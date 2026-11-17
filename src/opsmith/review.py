"""Human review gate (spec 5.6). Nothing executes without a recorded approval here."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from opsmith import audit_log, queue
from opsmith.audit_log import Actor, transition_task
from opsmith.models import Finding, Lead, Project, Review, Service, SuppressionRule, Task, User
from opsmith.protection import classify
from opsmith.schemas import CATEGORIES, ProjectSettings

REASON_CODES = (
    "false_positive",
    "wont_fix",
    "duplicate",
    "insufficient_evidence",
    "out_of_scope",
    "too_risky",
    "other",
)
EDITABLE = {
    "title",
    "proposed_approach",
    "acceptance_criteria",
    "priority",
    "paths",
    "branch_name",
    "category",
    "risk_assessment",
    "effort_estimate",
}
REVIEWER_ROLES = frozenset({"reviewer", "admin"})
# Opsmith's own branch namespace: plain path segments, nothing git treats specially.
BRANCH_NAME = re.compile(r"opsmith/[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*")
_TEXT_LIMITS = {
    "title": 300,
    "proposed_approach": 20_000,
    "risk_assessment": 20_000,
    "branch_name": 200,
}
_LIST_LIMIT = 200
_ITEM_LIMIT = 5_000  # one path or acceptance criterion
_LONG_TEXT = 20_000  # a comment, an approach, an investigation question


def valid_branch_name(name: str) -> bool:
    return (
        len(name) <= _TEXT_LIMITS["branch_name"]
        and BRANCH_NAME.fullmatch(name) is not None
        and ".." not in name
        and not name.endswith((".lock", "."))
    )


def approval_scope_digest(session: Session, task: Task) -> str:
    """Hash of everything an approval authorizes: where the change goes and what the agent is
    told to do. It is stored on the approving review and re-checked by the executor before it
    starts and again before it pushes, so an approval never covers work the reviewer did not
    see (edited scope, a different target, findings attached later).
    """
    finding_ids = session.scalars(
        select(Finding.id).where(Finding.task_id == task.id).order_by(Finding.id)
    ).all()
    scope = {
        "task_id": task.id,
        "repository_id": task.repository_id,
        "branch_name": task.branch_name,
        "title": task.title,
        "category": task.category,
        "problem_statement": task.problem_statement,
        "proposed_approach": task.proposed_approach,
        "acceptance_criteria": list(task.acceptance_criteria),
        "paths": list(task.paths),
        "protection": task.protection,
        "finding_ids": list(finding_ids),
    }
    encoded = json.dumps(scope, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "approve_with_edits", "reject", "defer", "request_investigation"]
    comment: str = Field(default="", max_length=_LONG_TEXT)
    reason_code: str | None = Field(default=None, max_length=50)  # the column's size
    edits: dict[str, Any] = Field(default_factory=dict, max_length=len(EDITABLE))
    elevated_ack: bool = False
    # hypothesis/question for the next inference run
    investigation: str | None = Field(default=None, max_length=_LONG_TEXT)
    expected_version: int | None = None


class ReviewError(ValueError):
    pass


class ReviewService:
    def review(self, session: Session, task: Task, reviewer: User, req: ReviewRequest) -> Review:
        if reviewer.role not in REVIEWER_ROLES:
            raise ReviewError("only reviewers or admins can review tasks")
        if reviewer.tenant_id != task.tenant_id:
            raise ReviewError("task not found")
        # Lock the row and re-read it: two simultaneous decisions (a double click, or an
        # approve racing a reject) are serialized, and the loser sees the new status below.
        session.refresh(task, with_for_update=True)
        if task.status != "in_review":
            raise ReviewError(f"task is {task.status}; only in_review tasks can be reviewed")
        if req.expected_version is not None and req.expected_version != task.version:
            raise ReviewError("task changed since you loaded it; reload and review again")
        project = session.get(Project, task.project_id)
        assert project is not None
        settings = ProjectSettings.model_validate(project.settings)
        actor = Actor("user", reviewer.id)

        review = Review(
            tenant_id=task.tenant_id,
            task_id=task.id,
            reviewer_id=reviewer.id,
            reviewer_name=reviewer.name,
            reviewer_role=reviewer.role,
            decision=req.decision,
            comment=req.comment,
            reason_code=req.reason_code,
            elevated_ack=req.elevated_ack,
            task_version=task.version,
        )
        if req.decision in {"approve", "approve_with_edits"}:
            if req.decision == "approve_with_edits":
                review.edits = self._apply_edits(task, req.edits, settings)
            elif req.edits:
                raise ReviewError("use approve_with_edits to change the task")
            if task.protection == "excluded":
                raise ReviewError(
                    "task touches excluded paths; it cannot be executed "
                    "autonomously - handle it manually and close it"
                )
            if task.protection == "elevated" and (reviewer.role != "admin" or not req.elevated_ack):
                raise ReviewError("protected paths: an admin must approve with elevated_ack")
            review.scope_digest = approval_scope_digest(session, task)
            session.add(review)
            session.flush()
            transition_task(
                session,
                task,
                "approved",
                actor=actor,
                details={"review_id": review.id, "edited": bool(review.edits)},
            )
            queue.enqueue(
                session,
                tenant_id=task.tenant_id,
                kind="execute",
                payload={"task_id": task.id, "review_id": review.id},
                idempotency_key=f"execute:{task.id}:{review.id}",
                max_attempts=3,
            )
        elif req.decision == "reject":
            if req.reason_code not in REASON_CODES:
                raise ReviewError(f"reject requires reason_code in {REASON_CODES}")
            session.add(review)
            session.flush()
            task.rejection_reason = req.reason_code
            kinds = sorted({e.get("kind") for e in task.evidence if e.get("kind")})
            session.add(
                SuppressionRule(
                    tenant_id=task.tenant_id,
                    project_id=task.project_id,
                    fingerprint=task.id,
                    reason_code=req.reason_code,
                    severity_at_rejection=task.severity,
                    evidence_strength_at_rejection=task.evidence_strength,
                    sources_at_rejection=kinds,
                    task_id=task.id,
                )
            )
            transition_task(
                session,
                task,
                "rejected",
                actor=actor,
                details={"review_id": review.id, "reason": req.reason_code},
            )
        elif req.decision == "defer":
            session.add(review)
            session.flush()
            transition_task(
                session, task, "deferred", actor=actor, details={"review_id": review.id}
            )
        else:  # request_investigation
            question = (req.investigation or req.comment).strip()
            if not question:
                raise ReviewError("request_investigation needs an investigation question")
            session.add(review)
            session.flush()
            self._open_investigation(session, task, question, review)
            transition_task(
                session, task, "investigating", actor=actor, details={"review_id": review.id}
            )
        return review

    def resubmit(self, session: Session, task: Task, user: User) -> None:
        """Move a deferred task back into the queue."""
        if user.role not in REVIEWER_ROLES:
            raise ReviewError("only reviewers or admins can resubmit tasks")
        if task.status != "deferred":
            raise ReviewError("only deferred tasks can be resubmitted")
        transition_task(session, task, "in_review", actor=Actor("user", user.id))

    @staticmethod
    def _apply_edits(
        task: Task, edits: dict[str, Any], settings: ProjectSettings
    ) -> dict[str, Any]:
        unknown = set(edits) - EDITABLE
        if unknown:
            raise ReviewError(f"fields not editable: {sorted(unknown)}")
        if not edits:
            raise ReviewError("approve_with_edits requires at least one edit")
        # Tuples, not sets: a JSON list or object is unhashable, and must be refused here
        # rather than raise TypeError out of a membership test.
        if "priority" in edits and edits["priority"] not in ("P0", "P1", "P2", "P3"):
            raise ReviewError("priority must be P0-P3")
        if "category" in edits and edits["category"] not in CATEGORIES:
            raise ReviewError(f"category must be one of {CATEGORIES}")
        if "effort_estimate" in edits and edits["effort_estimate"] not in ("S", "M", "L"):
            raise ReviewError("effort_estimate must be S, M or L")
        for key, limit in _TEXT_LIMITS.items():
            if key in edits and not (
                isinstance(edits[key], str) and 0 < len(edits[key].strip()) <= limit
            ):
                raise ReviewError(f"{key} must be a non-empty string of at most {limit} chars")
        for key in ("paths", "acceptance_criteria"):
            if key in edits and not (
                isinstance(edits[key], list)
                and len(edits[key]) <= _LIST_LIMIT
                and all(
                    isinstance(x, str) and 0 < len(x.strip()) <= _ITEM_LIMIT for x in edits[key]
                )
            ):
                raise ReviewError(
                    f"{key} must be a list of at most {_LIST_LIMIT} strings of at most "
                    f"{_ITEM_LIMIT} characters"
                )
        if "acceptance_criteria" in edits and not edits["acceptance_criteria"]:
            raise ReviewError("acceptance_criteria cannot be empty")
        if "paths" in edits and any(
            p.startswith("/") or ".." in p.split("/") or "\x00" in p for p in edits["paths"]
        ):
            raise ReviewError("paths must be relative to the repository root")
        if "branch_name" in edits and not valid_branch_name(edits["branch_name"]):
            raise ReviewError(
                "branch_name must look like 'opsmith/<segment>[/<segment>...]' using letters, "
                "digits, '.', '_' and '-'"
            )
        before = {k: getattr(task, k) for k in edits}
        for key, value in edits.items():
            setattr(task, key, value)
        if "paths" in edits:  # scope changes re-evaluate protection
            task.protection = classify(task.paths, settings.protected_paths)
        return {"before": before, "after": edits}

    @staticmethod
    def _open_investigation(session: Session, task: Task, question: str, review: Review) -> Lead:
        signal = next((e.get("signal") for e in task.evidence if e.get("signal")), None)
        service_id = None
        for fid in [e.get("finding_id") for e in task.evidence if e.get("finding_id")]:
            finding = session.get(Finding, fid)
            if finding is not None and finding.service_id:
                service_id = finding.service_id
                break
        if service_id is None:
            service_id = session.scalar(
                select(Service.id)
                .where(Service.project_id == task.project_id)
                .order_by(Service.created_at)
                .limit(1)
            )
        if service_id is None:
            raise ReviewError("project has no service to investigate; add a connector first")
        project = session.get(Project, task.project_id)
        assert project is not None
        settings = ProjectSettings.model_validate(project.settings)
        lead = Lead(
            tenant_id=task.tenant_id,
            project_id=task.project_id,
            service_id=service_id,
            title=f"Investigation requested: {task.title}"[:300],
            hypothesis=question,
            signal=signal,
            suspected_path=task.paths[0] if task.paths else None,
            source="review_request",
            task_id=task.id,
            max_carry=settings.lead_max_carry,
            evidence_refs=[r for e in task.evidence for r in (e.get("refs") or [])][:5],
            context=f"Reviewer {review.reviewer_name}: {question}",
        )
        session.add(lead)
        session.flush()
        audit_log.record(
            session,
            tenant_id=task.tenant_id,
            entity_type="lead",
            entity_id=lead.id,
            action="lead.open",
            to_state="open",
            actor=Actor("user", review.reviewer_id),
            details={"task_id": task.id, "source": "review_request"},
        )
        return lead
