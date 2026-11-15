"""Per-project cron/interval scheduler.

Schedules live in the database. ``Scheduler.tick`` enqueues every due schedule with an
idempotency key derived from the schedule slot, so running several scheduler replicas (or
ticking twice) never double-enqueues. Missed slots are coalesced into one run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsmith import queue
from opsmith.db import Database, utcnow
from opsmith.models import Connector, Project, Repository, Schedule, Service
from opsmith.schemas import ProjectSettings

# --------------------------------------------------------------------------- cron


@dataclass(frozen=True)
class CronExpr:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    dom_restricted: bool
    dow_restricted: bool

    @classmethod
    def parse(cls, expr: str) -> CronExpr:
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError(f"cron expression needs 5 fields: {expr!r}")
        m, h, dom, mon, dow = parts
        weekdays = frozenset(0 if d == 7 else d for d in _field(dow, 0, 7))
        return cls(
            minutes=_field(m, 0, 59),
            hours=_field(h, 0, 23),
            days=_field(dom, 1, 31),
            months=_field(mon, 1, 12),
            weekdays=weekdays,
            dom_restricted=dom != "*",
            dow_restricted=dow != "*",
        )

    def matches(self, dt: datetime) -> bool:
        if dt.minute not in self.minutes or dt.hour not in self.hours:
            return False
        if dt.month not in self.months:
            return False
        dom_ok = dt.day in self.days
        dow_ok = ((dt.weekday() + 1) % 7) in self.weekdays
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok  # POSIX cron semantics
        return dom_ok and dow_ok

    def next_after(self, dt: datetime) -> datetime:
        candidate = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(366 * 24 * 60):
            if self.matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)
        raise ValueError("cron expression never matches")


def _field(spec: str, lo: int, hi: int) -> frozenset[int]:
    values: set[int] = set()
    for item in spec.split(","):
        step = 1
        part = item
        if "/" in item:
            part, step_s = item.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError("cron step must be positive")
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
            if step != 1:
                end = hi
        if start < lo or end > hi or start > end:
            raise ValueError(f"cron value out of range in {spec!r}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


# --------------------------------------------------------------------------- schedules


def compute_next(schedule: Schedule, after: datetime) -> datetime:
    if schedule.cron:
        return CronExpr.parse(schedule.cron).next_after(after)
    if not schedule.interval_seconds:
        raise ValueError(f"schedule {schedule.id} has neither cron nor interval")
    nxt = schedule.next_run_at + timedelta(seconds=schedule.interval_seconds)
    while nxt <= after:  # coalesce missed slots
        nxt += timedelta(seconds=schedule.interval_seconds)
    return nxt


def upsert_schedule(
    session: Session,
    *,
    tenant_id: str,
    project_id: str,
    job_kind: str,
    target_id: str,
    interval_seconds: int | None = None,
    cron: str | None = None,
    payload: dict[str, object] | None = None,
    first_run_at: datetime | None = None,
) -> Schedule:
    if cron:
        CronExpr.parse(cron)  # validate early
    schedule = session.scalar(
        select(Schedule).where(Schedule.job_kind == job_kind, Schedule.target_id == target_id)
    )
    if schedule is None:
        schedule = Schedule(
            tenant_id=tenant_id,
            project_id=project_id,
            job_kind=job_kind,
            target_id=target_id,
            next_run_at=first_run_at or utcnow(),
        )
        session.add(schedule)
    schedule.interval_seconds = interval_seconds
    schedule.cron = cron
    schedule.payload = dict(payload or {})
    if cron and first_run_at is None and schedule.last_enqueued_at is None:
        schedule.next_run_at = CronExpr.parse(cron).next_after(utcnow())
    session.flush()
    return schedule


def ensure_project_schedules(session: Session, project: Project) -> list[Schedule]:
    """Create/refresh the default schedules for every component of a project."""
    settings = ProjectSettings.model_validate(project.settings)
    out: list[Schedule] = []
    t, p = project.tenant_id, project.id
    for conn in session.scalars(select(Connector).where(Connector.project_id == p)).all():
        out.append(
            upsert_schedule(
                session,
                tenant_id=t,
                project_id=p,
                job_kind="ingest",
                target_id=conn.id,
                interval_seconds=conn.interval_seconds or settings.ingest_interval_seconds,
                payload={"connector_id": conn.id},
            )
        )
    for svc in session.scalars(select(Service).where(Service.project_id == p)).all():
        out.append(ensure_inference_schedule(session, project, svc, settings))
    for repo in session.scalars(select(Repository).where(Repository.project_id == p)).all():
        out.append(
            upsert_schedule(
                session,
                tenant_id=t,
                project_id=p,
                job_kind="audit",
                target_id=repo.id,
                cron=settings.audit_cron,
                payload={"repository_id": repo.id, "mode": "auto"},
            )
        )
    out.append(
        upsert_schedule(
            session,
            tenant_id=t,
            project_id=p,
            job_kind="track_prs",
            target_id=p,
            interval_seconds=settings.pr_poll_seconds,
            payload={"project_id": p},
        )
    )
    return out


def ensure_inference_schedule(
    session: Session, project: Project, service: Service, settings: ProjectSettings | None = None
) -> Schedule:
    settings = settings or ProjectSettings.model_validate(project.settings)
    hours = service.inference_interval_hours or settings.inference_interval_hours
    return upsert_schedule(
        session,
        tenant_id=project.tenant_id,
        project_id=project.id,
        job_kind="inference",
        target_id=service.id,
        interval_seconds=max(int(hours * 3600), 60),
        payload={"service_id": service.id},
        first_run_at=utcnow() + timedelta(hours=hours),
    )


class Scheduler:
    def __init__(self, db: Database) -> None:
        self.db = db

    def tick(self, now: datetime | None = None) -> list[str]:
        now = now or utcnow()
        enqueued: list[str] = []
        with self.db.session() as s:
            due = s.scalars(
                select(Schedule).where(Schedule.enabled.is_(True), Schedule.next_run_at <= now)
            ).all()
            for sch in due:
                slot = sch.next_run_at.strftime("%Y%m%dT%H%M%S")
                payload = {**sch.payload, "slot": slot}
                job = queue.enqueue(
                    s,
                    tenant_id=sch.tenant_id,
                    kind=sch.job_kind,
                    payload=payload,
                    idempotency_key=f"sched:{sch.id}:{slot}",
                )
                sch.last_enqueued_at = now
                sch.next_run_at = compute_next(sch, now)
                enqueued.append(job.id)
        return enqueued
