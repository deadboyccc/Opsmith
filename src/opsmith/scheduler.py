"""Per-project cron/interval scheduler.

Schedules live in the database. ``Scheduler.tick`` enqueues every due schedule with an
idempotency key derived from the schedule slot, so running several scheduler replicas (or
ticking twice) never double-enqueues. Missed slots are coalesced into one run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsmith import audit_log, queue
from opsmith.db import Database, utcnow
from opsmith.models import Connector, Project, Repository, Schedule, Service
from opsmith.schemas import ProjectSettings

log = logging.getLogger(__name__)

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
        parsed = cls(
            minutes=_field(m, 0, 59),
            hours=_field(h, 0, 23),
            days=_field(dom, 1, 31),
            months=_field(mon, 1, 12),
            weekdays=weekdays,
            dom_restricted=dom != "*",
            dow_restricted=dow != "*",
        )
        parsed.next_after(datetime(2024, 1, 1))  # rejects dates that never occur, e.g. Feb 31
        return parsed

    def matches(self, dt: datetime) -> bool:
        if dt.minute not in self.minutes or dt.hour not in self.hours:
            return False
        return dt.month in self.months and self._day_matches(dt)

    def next_after(self, dt: datetime) -> datetime:
        candidate = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
        # Eight years covers the rarest satisfiable expression (Feb 29 on a given weekday is
        # not expressible: day-of-month and day-of-week are OR-ed), so this only runs out
        # for impossible dates.
        for _ in range(8 * 366):
            if candidate.month in self.months and self._day_matches(candidate):
                day_end = candidate.replace(hour=23, minute=59)
                while candidate <= day_end:
                    if candidate.hour in self.hours and candidate.minute in self.minutes:
                        return candidate
                    candidate += timedelta(minutes=1)
            else:
                candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
        raise ValueError("cron expression never matches a real date")

    def _day_matches(self, dt: datetime) -> bool:
        dom_ok = dt.day in self.days
        dow_ok = ((dt.weekday() + 1) % 7) in self.weekdays
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok  # POSIX cron semantics
        return dom_ok and dow_ok


def _field(spec: str, lo: int, hi: int) -> frozenset[int]:
    values: set[int] = set()
    try:
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
                if "/" in item:
                    end = hi
            if start < lo or end > hi or start > end:
                raise ValueError("value out of range")
            values.update(range(start, end + 1, step))
    except ValueError as exc:
        raise ValueError(f"invalid cron field {spec!r}: {exc}") from None
    return frozenset(values)


# --------------------------------------------------------------------------- schedules


def compute_next(schedule: Schedule, after: datetime) -> datetime:
    if schedule.cron:
        return CronExpr.parse(schedule.cron).next_after(after)
    if not schedule.interval_seconds:
        raise ValueError(f"schedule {schedule.id} has neither cron nor interval")
    interval = timedelta(seconds=schedule.interval_seconds)
    nxt = schedule.next_run_at + interval
    if nxt <= after:  # coalesce missed slots, staying on the schedule's own grid
        nxt += interval * ((after - nxt) // interval + 1)
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
    """Create the schedule or bring it in line with the given timing.

    A schedule whose timing an admin set directly (``custom``) keeps that timing; only its
    payload is refreshed. When the timing does change, the next run is recomputed so the new
    cadence applies immediately instead of after one more run on the old one.
    """
    if cron:
        CronExpr.parse(cron)  # validate early
    elif not interval_seconds or interval_seconds <= 0:
        raise ValueError("a schedule needs a cron expression or a positive interval")
    schedule = session.scalar(
        select(Schedule).where(Schedule.job_kind == job_kind, Schedule.target_id == target_id)
    )
    created = schedule is None
    if schedule is None:
        schedule = Schedule(
            tenant_id=tenant_id,
            project_id=project_id,
            job_kind=job_kind,
            target_id=target_id,
            next_run_at=first_run_at or utcnow(),
        )
        session.add(schedule)
    schedule.payload = dict(payload or {})
    if created or not schedule.custom:
        retimed = not created and (
            schedule.cron != cron or schedule.interval_seconds != interval_seconds
        )
        schedule.interval_seconds = interval_seconds
        schedule.cron = cron
        if (created and cron and first_run_at is None) or retimed:
            reschedule(schedule)
    session.flush()
    return schedule


def reschedule(schedule: Schedule, now: datetime | None = None) -> None:
    """Set the next run from the schedule's current timing, counted from ``now``."""
    now = now or utcnow()
    if schedule.cron:
        schedule.next_run_at = CronExpr.parse(schedule.cron).next_after(now)
    elif schedule.interval_seconds:
        schedule.next_run_at = now + timedelta(seconds=schedule.interval_seconds)


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
                interval_seconds=conn.interval_seconds,
                payload={"connector_id": conn.id},
            )
        )
    for svc in session.scalars(select(Service).where(Service.project_id == p)).all():
        out.append(ensure_inference_schedule(session, project, svc, settings))
    repos = session.scalars(select(Repository).where(Repository.project_id == p)).all()
    for repo in repos:
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
    out += ensure_check_schedules(session, project, settings, list(repos))
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


def ensure_check_schedules(
    session: Session, project: Project, settings: ProjectSettings, repos: list[Repository]
) -> list[Schedule]:
    """One schedule per enabled check and repository it covers, on the check's cron. The
    schedules of checks that were removed, disabled or narrowed are deleted, so a check that
    is gone stops running."""
    from opsmith.audits.checks import applies_to, check_target_id  # noqa: PLC0415 - cycle

    out: list[Schedule] = []
    for check in settings.checks:
        if not check.enabled:
            continue
        for repo in repos:
            if applies_to(check, repo):
                out.append(
                    upsert_schedule(
                        session,
                        tenant_id=project.tenant_id,
                        project_id=project.id,
                        job_kind="check",
                        target_id=check_target_id(repo.id, check.name),
                        cron=check.cron,
                        payload={"repository_id": repo.id, "check": check.name},
                    )
                )
    wanted = {schedule.id for schedule in out}
    stale = session.scalars(
        select(Schedule).where(Schedule.project_id == project.id, Schedule.job_kind == "check")
    ).all()
    for schedule in stale:
        if schedule.id not in wanted:
            session.delete(schedule)
    session.flush()
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
                try:
                    next_run = compute_next(sch, now)
                except ValueError as exc:
                    # One unusable schedule must not stop every other tenant's schedules.
                    # Disable it visibly instead of failing the whole tick forever.
                    sch.enabled = False
                    log.error("schedule disabled", extra={"schedule_id": sch.id, "error": str(exc)})
                    audit_log.record(
                        s,
                        tenant_id=sch.tenant_id,
                        entity_type="schedule",
                        entity_id=sch.id,
                        action="schedule.disabled",
                        details={"reason": str(exc), "job_kind": sch.job_kind},
                    )
                    continue
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
                sch.next_run_at = next_run
                enqueued.append(job.id)
        return enqueued
