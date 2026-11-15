from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from opsmith import queue
from opsmith.db import utcnow
from opsmith.models import AuditLog, Job, Notification, Schedule
from opsmith.queue import PermanentFailure, RetryLater, Worker
from opsmith.scheduler import CronExpr, Scheduler, upsert_schedule


def test_cron_parsing_and_next() -> None:
    expr = CronExpr.parse("*/15 2-3 * * 1-5")
    assert expr.minutes == frozenset({0, 15, 30, 45})
    monday = datetime(2026, 11, 16, 1, 59, tzinfo=UTC)
    assert expr.next_after(monday) == datetime(2026, 11, 16, 2, 0, tzinfo=UTC)
    saturday = datetime(2026, 11, 21, 3, 50, tzinfo=UTC)
    assert expr.next_after(saturday).weekday() == 0
    assert CronExpr.parse("0 0 * * 7").weekdays == frozenset({0})
    for bad in ("* * *", "61 * * * *", "*/0 * * * *"):
        with pytest.raises(ValueError, match="cron"):
            CronExpr.parse(bad)


def test_enqueue_is_idempotent(platform, seed) -> None:
    with platform.db.session() as s:
        a = queue.enqueue(s, tenant_id=seed.tenant.id, kind="x", payload={}, idempotency_key="k")
        b = queue.enqueue(s, tenant_id=seed.tenant.id, kind="x", payload={}, idempotency_key="k")
        assert a.id == b.id


def test_scheduler_tick_enqueues_once_and_coalesces(platform, seed) -> None:
    with platform.db.session() as s:
        sch = upsert_schedule(
            s,
            tenant_id=seed.tenant.id,
            project_id=seed.project.id,
            job_kind="track_prs",
            target_id="custom",
            interval_seconds=600,
            payload={"project_id": seed.project.id},
            first_run_at=utcnow() - timedelta(hours=5),
        )
        sch_id = sch.id
    first = platform.scheduler.tick()
    again = Scheduler(platform.db).tick()
    with platform.db.session() as s:
        sch = s.get(Schedule, sch_id)
        assert sch.next_run_at > utcnow()  # five hours of missed slots -> one run
        jobs = s.scalars(select(Job).where(Job.idempotency_key.like(f"sched:{sch_id}:%"))).all()
    assert len(jobs) == 1
    assert jobs[0].id in first
    assert jobs[0].id not in again


def _worker(platform, handler, **kw):
    return Worker(platform.db, {"t": handler}, on_dead=platform._on_dead, **kw)


def test_worker_success_retry_and_dead_letter(platform, seed) -> None:
    calls = {"n": 0}

    def flaky(_s, _job):
        calls["n"] += 1
        raise RuntimeError("boom")

    with platform.db.session() as s:
        job = queue.enqueue(
            s,
            tenant_id=seed.tenant.id,
            kind="t",
            payload={},
            idempotency_key="flaky",
            max_attempts=2,
        )
        job_id = job.id
    worker = _worker(platform, flaky)
    assert worker.run_once().status == "retrying"
    with platform.db.session() as s:
        s.get(Job, job_id).run_after = utcnow()  # skip backoff
    assert worker.run_once().status == "dead"
    with platform.db.session() as s:
        job = s.get(Job, job_id)
        assert job.status == "dead"
        assert "boom" in job.last_error
        assert s.scalar(select(AuditLog).where(AuditLog.action == "job.dead"))
        assert s.scalar(select(Notification).where(Notification.event == "job.dead"))
        queue.retry_dead_job(s, job)
        assert job.status == "queued"
        assert job.attempts == 0


def test_retry_later_does_not_consume_attempts(platform, seed) -> None:
    def wait(_s, _job):
        raise RetryLater(120, "busy")

    with platform.db.session() as s:
        job_id = queue.enqueue(
            s, tenant_id=seed.tenant.id, kind="t", payload={}, idempotency_key="w"
        ).id
    res = _worker(platform, wait).run_once()
    assert res.status == "deferred"
    with platform.db.session() as s:
        job = s.get(Job, job_id)
        assert job.attempts == 0
        assert job.deferrals == 1
        assert job.run_after > utcnow() + timedelta(seconds=60)


def test_permanent_failure_and_unknown_kind_go_dead(platform, seed) -> None:
    def perm(_s, _job):
        raise PermanentFailure("bad input")

    with platform.db.session() as s:
        queue.enqueue(s, tenant_id=seed.tenant.id, kind="t", payload={}, idempotency_key="p")
        queue.enqueue(s, tenant_id=seed.tenant.id, kind="unknown", payload={}, idempotency_key="u")
    results = _worker(platform, perm).drain()
    assert {r.status for r in results} == {"dead"}


def test_expired_leases_are_recovered(platform, seed) -> None:
    with platform.db.session() as s:
        job = queue.enqueue(
            s, tenant_id=seed.tenant.id, kind="t", payload={}, idempotency_key="lease"
        )
        job.status = "running"
        job.lease_expires_at = utcnow() - timedelta(minutes=1)
    worker = _worker(platform, lambda _s, _j: {"ok": True})
    assert worker.recover_expired_leases() == 1
    assert worker.run_once().status == "succeeded"
