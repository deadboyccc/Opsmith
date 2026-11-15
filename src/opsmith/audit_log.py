"""Append-only audit trail helpers and the task/lead state machines."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import event, inspect
from sqlalchemy.orm import InstanceState, Session

from opsmith.db import utcnow
from opsmith.models import AuditLog, AuditRun, InferenceRun, Lead, Task


@dataclass(frozen=True)
class Actor:
    type: str  # user | system | agent
    id: str

    @classmethod
    def system(cls, component: str = "system") -> Actor:
        return cls("system", component)


SYSTEM = Actor.system()


def record(
    session: Session,
    *,
    tenant_id: str,
    entity_type: str,
    entity_id: str,
    action: str,
    actor: Actor = SYSTEM,
    from_state: str | None = None,
    to_state: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    entry = AuditLog(
        tenant_id=tenant_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        actor_type=actor.type,
        actor_id=actor.id,
        from_state=from_state,
        to_state=to_state,
        details=details or {},
        created_at=utcnow(),
    )
    session.add(entry)
    return entry


# --------------------------------------------------------------------------- task lifecycle

# proposed -> in_review -> approved | rejected | deferred -> executing -> pr_open
#   -> merged | closed | failed ; merged -> verified | regressed (post-merge verification)
TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"in_review", "closed"}),
    "in_review": frozenset({"approved", "rejected", "deferred", "investigating", "closed"}),
    "deferred": frozenset({"in_review", "closed"}),
    "investigating": frozenset({"in_review", "closed"}),
    "approved": frozenset({"executing", "in_review", "closed"}),
    "executing": frozenset({"pr_open", "failed"}),
    "failed": frozenset({"in_review", "closed"}),
    "pr_open": frozenset({"merged", "closed"}),
    "merged": frozenset({"verified", "regressed"}),
    "regressed": frozenset({"in_review", "closed"}),
    "rejected": frozenset({"in_review"}),  # only via material evidence change
    "verified": frozenset(),
    "closed": frozenset(),
}
OPEN_TASK_STATES = frozenset(
    {
        "proposed",
        "in_review",
        "deferred",
        "investigating",
        "approved",
        "executing",
        "failed",
        "pr_open",
    }
)
DONE_TASK_STATES = frozenset({"merged", "verified", "closed"})


class InvalidTransition(ValueError):
    pass


def transition_task(
    session: Session,
    task: Task,
    to_state: str,
    *,
    actor: Actor = SYSTEM,
    details: dict[str, Any] | None = None,
) -> None:
    allowed = TASK_TRANSITIONS.get(task.status, frozenset())
    if to_state not in allowed:
        raise InvalidTransition(f"task {task.id}: {task.status} -> {to_state} is not allowed")
    previous = task.status
    task.status = to_state
    task.version += 1
    task.updated_at = utcnow()
    record(
        session,
        tenant_id=task.tenant_id,
        entity_type="task",
        entity_id=task.id,
        action=f"task.{to_state}",
        actor=actor,
        from_state=previous,
        to_state=to_state,
        details=details,
    )


# --------------------------------------------------------------------------- lead lifecycle

# open -> carried -> confirmed | refuted | expired
LEAD_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"carried", "confirmed", "refuted", "expired"}),
    "carried": frozenset({"carried", "confirmed", "refuted", "expired"}),
    "confirmed": frozenset(),
    "refuted": frozenset(),
    "expired": frozenset(),
}
ACTIVE_LEAD_STATES = frozenset({"open", "carried"})


def transition_lead(
    session: Session,
    lead: Lead,
    to_state: str,
    *,
    run_id: str | None,
    details: dict[str, Any] | None = None,
) -> None:
    if to_state not in LEAD_TRANSITIONS.get(lead.state, frozenset()):
        raise InvalidTransition(f"lead {lead.id}: {lead.state} -> {to_state} is not allowed")
    previous = lead.state
    lead.state = to_state
    lead.last_run_id = run_id
    record(
        session,
        tenant_id=lead.tenant_id,
        entity_type="lead",
        entity_id=lead.id,
        action=f"lead.{to_state}",
        from_state=previous,
        to_state=to_state,
        details={"run_id": run_id, **(details or {})},
    )


# --------------------------------------------------------------------------- immutability

_TERMINAL_RUN_STATES = frozenset({"succeeded", "failed", "budget_exceeded"})


class ImmutableRecordError(RuntimeError):
    pass


@event.listens_for(Session, "before_flush")
def _guard_immutable_runs(session: Session, _ctx: Any, _instances: Any) -> None:
    """Finished inference/audit runs are immutable records (spec 5.2)."""
    for obj in session.dirty:
        if not isinstance(obj, (InferenceRun, AuditRun)):
            continue
        state: InstanceState[Any] = inspect(obj)
        status_hist = state.attrs.status.history
        committed_status = (
            status_hist.deleted[0] if status_hist.deleted else state.attrs.status.value
        )
        if committed_status in _TERMINAL_RUN_STATES and session.is_modified(obj):
            raise ImmutableRecordError(f"{type(obj).__name__} {obj.id} is finished and immutable")
