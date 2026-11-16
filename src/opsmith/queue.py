"""Durable, idempotent, DB-backed job queue.

* ``enqueue`` is idempotent on ``idempotency_key`` - repeating it returns the existing job.
* Claims are atomic compare-and-set updates (portable across SQLite and PostgreSQL).
* Failed jobs retry with exponential backoff up to ``max_attempts`` and then move to
  ``dead`` - which is surfaced on the dashboard and notified. Nothing is dropped silently.
* A running job's lease is renewed by a heartbeat for as long as its worker is alive, so a
  slow job is never handed to a second worker. When a worker dies the lease expires and the
  job is picked up again - or dead-lettered once its attempts are used up.
"""

from __future__ import annotations

import logging
import threading
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from opsmith import audit_log
from opsmith.db import Database, utcnow
from opsmith.models import Job

log = logging.getLogger(__name__)

MAX_DEFERRALS = 200


class RetryLater(Exception):
    """Raise from a handler to reschedule without consuming a failure attempt."""

    def __init__(self, delay_seconds: float, reason: str) -> None:
        super().__init__(reason)
        self.delay_seconds = delay_seconds
        self.reason = reason


class PermanentFailure(Exception):
    """Raise from a handler when retrying cannot help (job goes straight to ``dead``)."""


def enqueue(
    session: Session,
    *,
    tenant_id: str,
    kind: str,
    payload: dict[str, Any],
    idempotency_key: str,
    run_after: datetime | None = None,
    max_attempts: int = 5,
    trace_id: str | None = None,
) -> Job:
    existing = session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
    if existing is not None:
        return existing
    job = Job(
        tenant_id=tenant_id,
        kind=kind,
        payload=payload,
        idempotency_key=idempotency_key,
        run_after=run_after or utcnow(),
        max_attempts=max_attempts,
        trace_id=trace_id or uuid.uuid4().hex[:16],
    )
    # A concurrent enqueue of the same key fails this transaction on the unique constraint;
    # the caller (a job or scheduler tick) is itself retried, so the race is benign.
    session.add(job)
    session.flush()
    return job


Handler = Callable[[Session, Job], dict[str, Any] | None]


@dataclass
class WorkResult:
    job_id: str
    kind: str
    status: str
    error: str | None = None


class Worker:
    def __init__(
        self,
        db: Database,
        handlers: dict[str, Handler],
        *,
        worker_id: str | None = None,
        lease_seconds: int = 900,
        kinds: set[str] | None = None,
        on_dead: Callable[[Session, Job], None] | None = None,
        heartbeat_seconds: float | None = None,
    ) -> None:
        self.db = db
        self.handlers = handlers
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.lease_seconds = lease_seconds
        # Renew three times per lease, so two renewals may fail before the lease lapses.
        self.heartbeat_seconds = heartbeat_seconds or max(lease_seconds / 3, 1.0)
        self.kinds = kinds
        self.on_dead = on_dead

    # ------------------------------------------------------------------ claiming
    def recover_expired_leases(self) -> int:
        """Requeue jobs whose worker stopped renewing the lease (it crashed or was killed).

        A job that keeps taking its worker down would otherwise be retried forever, because
        the failure path never runs for it; once its attempts are used up it is dead-lettered
        here instead, which audits and notifies like any other dead job.
        """
        recovered = 0
        with self.db.session() as s:
            # Locked, and rows a renewing heartbeat holds are skipped: a lease renewed between
            # this query and the update must not be taken away.
            expired = s.scalars(
                select(Job)
                .where(Job.status == "running", Job.lease_expires_at < utcnow())
                .with_for_update(skip_locked=True)
            ).all()
            for job in expired:
                job.locked_by = None
                job.lease_expires_at = None
                if job.attempts >= job.max_attempts:
                    self._mark_dead(s, job, "worker lost the job repeatedly (lease expired)")
                else:
                    job.status = "queued"
                    job.last_error = "lease expired; requeued"
                recovered += 1
        return recovered

    def _claim(self) -> str | None:
        now = utcnow()
        with self.db.session() as s:
            query = (
                select(Job.id)
                .where(Job.status == "queued", Job.run_after <= now)
                .order_by(Job.run_after, Job.created_at)
                .limit(10)
            )
            if self.kinds:
                query = query.where(Job.kind.in_(self.kinds))
            for job_id in s.scalars(query).all():
                result = s.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.status == "queued")
                    .values(
                        status="running",
                        locked_by=self.worker_id,
                        lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                        started_at=now,
                        attempts=Job.attempts + 1,
                    )
                )
                if getattr(result, "rowcount", 0) == 1:
                    return job_id
        return None

    # ------------------------------------------------------------------ execution
    def run_once(self) -> WorkResult | None:
        job_id = self._claim()
        if job_id is None:
            return None
        return self._execute(job_id)

    def drain(self, max_jobs: int = 1000) -> list[WorkResult]:
        """Run until no job is currently due (used by tests, the demo and ``opsmith run``)."""
        results: list[WorkResult] = []
        for _ in range(max_jobs):
            result = self.run_once()
            if result is None:
                break
            results.append(result)
        return results

    def _owns(self, session: Session, job: Job) -> bool:
        """Whether this worker still holds the job. The row stays locked until the caller
        commits, so ownership cannot change between this check and the write."""
        session.refresh(job, with_for_update=True)
        return job.status == "running" and job.locked_by == self.worker_id

    def _heartbeat(self, job_id: str, stop: threading.Event) -> None:
        """Renew the lease while the handler runs. Stops by itself once the job is no longer
        ours, so a job taken over after a real outage is not held by two workers."""
        while not stop.wait(self.heartbeat_seconds):
            try:
                with self.db.session() as s:
                    result = s.execute(
                        update(Job)
                        .where(
                            Job.id == job_id,
                            Job.status == "running",
                            Job.locked_by == self.worker_id,
                        )
                        .values(lease_expires_at=utcnow() + timedelta(seconds=self.lease_seconds))
                    )
                    if getattr(result, "rowcount", 0) != 1:
                        return
            except Exception as exc:  # a transient DB error must not kill the heartbeat
                log.warning("lease heartbeat failed", extra={"job_id": job_id, "error": str(exc)})

    def _execute(self, job_id: str) -> WorkResult:
        stop = threading.Event()
        beat = threading.Thread(
            target=self._heartbeat, args=(job_id, stop), name=f"lease-{job_id}", daemon=True
        )
        beat.start()
        try:
            return self._run_handler(job_id)
        finally:
            stop.set()
            beat.join(timeout=5)

    def _run_handler(self, job_id: str) -> WorkResult:
        session = self.db.new_session()
        try:
            job = session.get(Job, job_id)
            assert job is not None
            kind = job.kind
            handler = self.handlers.get(kind)
            if handler is None:
                raise PermanentFailure(f"no handler registered for job kind {kind!r}")
            result = handler(session, job) or {}
            if not self._owns(session, job):
                # The lease was lost (long outage) and another worker has the job now. Keep
                # this handler's committed work out of the way of the new owner's bookkeeping.
                session.rollback()
                log.warning("job finished after losing its lease", extra={"job_id": job_id})
                return WorkResult(job_id, kind, "lost_lease")
            job.status = "succeeded"
            job.result = result
            job.finished_at = utcnow()
            job.locked_by = None
            job.lease_expires_at = None
            session.commit()
            log.info("job succeeded", extra={"job_id": job_id, "kind": kind})
            return WorkResult(job_id, kind, "succeeded")
        except RetryLater as exc:
            session.rollback()
            return self._reschedule(job_id, exc)
        except Exception as exc:
            session.rollback()
            return self._fail(job_id, exc)
        finally:
            session.close()

    def _lost(self, job: Job, outcome: str) -> WorkResult:
        """The lease expired and the job was requeued or taken by another worker: leave its
        bookkeeping to the new owner instead of requeueing it under them."""
        log.warning(f"job {outcome} after losing its lease", extra={"job_id": job.id})
        return WorkResult(job.id, job.kind, "lost_lease")

    def _reschedule(self, job_id: str, exc: RetryLater) -> WorkResult:
        with self.db.session() as s:
            job = s.get(Job, job_id)
            assert job is not None
            if not self._owns(s, job):
                return self._lost(job, "deferred")
            job.deferrals += 1
            job.attempts = max(job.attempts - 1, 0)  # a deferral is not a failed attempt
            if job.deferrals > MAX_DEFERRALS:
                return self._mark_dead(s, job, f"deferred too many times: {exc.reason}")
            job.status = "queued"
            job.locked_by = None
            job.lease_expires_at = None
            job.last_error = f"deferred: {exc.reason}"
            job.run_after = utcnow() + timedelta(seconds=exc.delay_seconds)
            return WorkResult(job_id, job.kind, "deferred", exc.reason)

    def _fail(self, job_id: str, exc: Exception) -> WorkResult:
        error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        log.warning("job failed", extra={"job_id": job_id, "error": error})
        with self.db.session() as s:
            job = s.get(Job, job_id)
            assert job is not None
            if not self._owns(s, job):
                return self._lost(job, "failed")
            job.last_error = error[:4000]
            job.locked_by = None
            job.lease_expires_at = None
            if isinstance(exc, PermanentFailure) or job.attempts >= job.max_attempts:
                return self._mark_dead(s, job, error)
            backoff = min(30 * 2 ** (job.attempts - 1), 3600)
            job.status = "queued"
            job.run_after = utcnow() + timedelta(seconds=backoff)
            return WorkResult(job_id, job.kind, "retrying", error)

    def _mark_dead(self, s: Session, job: Job, error: str) -> WorkResult:
        job.status = "dead"
        job.finished_at = utcnow()
        job.last_error = error[:4000]
        audit_log.record(
            s,
            tenant_id=job.tenant_id,
            entity_type="job",
            entity_id=job.id,
            action="job.dead",
            to_state="dead",
            details={"kind": job.kind, "error": error[:1000], "attempts": job.attempts},
        )
        if self.on_dead is not None:
            self.on_dead(s, job)
        return WorkResult(job.id, job.kind, "dead", error)


def retry_dead_job(session: Session, job: Job) -> None:
    """Operator action: put a dead job back on the queue with a fresh attempt budget."""
    if job.status != "dead":
        raise ValueError("only dead jobs can be retried")
    job.status = "queued"
    job.attempts = 0
    job.deferrals = 0
    job.run_after = utcnow()
    job.finished_at = None
    job.locked_by = None
    job.lease_expires_at = None
