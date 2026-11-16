from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from opsmith import queue
from opsmith.db import utcnow
from opsmith.models import AuditLog, Job, Notification, Schedule, Task
from opsmith.queue import PermanentFailure, RetryLater, Worker
from opsmith.scheduler import CronExpr, Scheduler, upsert_schedule
from opsmith.security import create_tenant


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


@pytest.mark.parametrize("outcome", [RuntimeError("boom"), RetryLater(60, "later")])
def test_a_worker_that_lost_its_lease_leaves_the_job_to_its_new_owner(
    platform, seed, outcome: Exception
) -> None:
    """A's lease lapses while its handler runs and B takes the job over. When A's handler then
    fails or defers, A must not requeue the job under B (a third worker would run it too)."""
    db = platform.db
    b = Worker(db, {}, worker_id="B", heartbeat_seconds=999)

    def handler(s, job):
        with db.session() as other:
            other.get(Job, job.id).lease_expires_at = utcnow() - timedelta(seconds=1)
        assert b.recover_expired_leases() == 1
        assert b._claim() == job.id
        raise outcome

    a = Worker(db, {"x": handler}, worker_id="A", heartbeat_seconds=999)
    with db.session() as s:
        job_id = queue.enqueue(
            s, tenant_id=seed.tenant.id, kind="x", payload={}, idempotency_key="lease"
        ).id
    result = a.run_once()
    assert result is not None
    assert result.status == "lost_lease"
    with db.session() as s:
        job = s.get(Job, job_id)
        assert (job.status, job.locked_by, job.deferrals) == ("running", "B", 0)
    assert Worker(db, {"x": handler}, worker_id="C")._claim() is None


@pytest.mark.parametrize("kind", ["ingest", "inference", "audit", "execute", "verify"])
def test_a_dead_job_notifies_its_project(platform, seed, kind) -> None:
    """Only three job kinds carry ``project_id``. For every other kind the ``job.dead``
    notification had no project, so it was logged and never reached the project's channels."""
    with platform.db.session() as s:
        task = Task(tenant_id=seed.tenant.id, project_id=seed.project.id, title="t", category="fix")
        s.add(task)
        s.flush()
        payload = {
            "ingest": {"connector_id": seed.log_connector.id},
            "inference": {"service_id": seed.service.id},
            "audit": {"repository_id": seed.repo.id},
            "execute": {"task_id": task.id, "review_id": "rev_x"},
            "verify": {"task_id": task.id},
        }[kind]
        queue.enqueue(
            s, tenant_id=seed.tenant.id, kind=kind, payload=payload, idempotency_key=f"dead-{kind}"
        )

    def fail(_s, _job):
        raise PermanentFailure("no")

    worker = Worker(platform.db, {kind: fail}, kinds={kind}, on_dead=platform._on_dead)
    assert worker.run_once().status == "dead"
    with platform.db.session() as s:
        note = s.scalars(select(Notification).where(Notification.event == "job.dead")).one()
        assert note.project_id == seed.project.id


def test_a_dead_job_of_another_tenant_does_not_borrow_a_project(platform, seed) -> None:
    """The project is looked up within the job's own tenant only."""
    with platform.db.session() as s:
        other = create_tenant(s, "other")
        queue.enqueue(
            s,
            tenant_id=other.id,
            kind="ingest",
            payload={"connector_id": seed.log_connector.id},
            idempotency_key="foreign",
        )

    def fail(_s, _job):
        raise PermanentFailure("no")

    worker = Worker(platform.db, {"ingest": fail}, kinds={"ingest"}, on_dead=platform._on_dead)
    assert worker.run_once().status == "dead"
    with platform.db.session() as s:
        note = s.scalars(select(Notification).where(Notification.event == "job.dead")).one()
        assert note.project_id is None


def test_a_job_deferred_too_often_releases_its_lease(platform, seed, monkeypatch) -> None:
    monkeypatch.setattr(queue, "MAX_DEFERRALS", 0)

    def wait(_s, _job):
        raise RetryLater(1, "busy")

    with platform.db.session() as s:
        job_id = queue.enqueue(
            s, tenant_id=seed.tenant.id, kind="t", payload={}, idempotency_key="d"
        ).id
    assert _worker(platform, wait).run_once().status == "dead"
    with platform.db.session() as s:
        job = s.get(Job, job_id)
        assert (job.status, job.locked_by, job.lease_expires_at) == ("dead", None, None)
