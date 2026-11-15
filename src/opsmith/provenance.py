"""Provenance chain for a task: PR <- execution <- approval <- task <- findings <- evidence
(bundles/templates/metric features/audit findings) <- pipeline runs <- leads."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsmith.models import (
    AnalysisBundle,
    AuditLog,
    AuditRun,
    ExecutionRun,
    Finding,
    InferenceRun,
    Lead,
    Review,
    Task,
)


def _row(obj: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in fields:
        value = getattr(obj, f)
        out[f] = value.isoformat() if hasattr(value, "isoformat") else value
    return out


def task_provenance(session: Session, task: Task) -> dict[str, Any]:
    findings = session.scalars(
        select(Finding).where(Finding.task_id == task.id).order_by(Finding.created_at)
    ).all()
    inference_ids = {f.source_run_id for f in findings if f.source == "inference"}
    audit_ids = {f.source_run_id for f in findings if f.source.startswith("audit:")}
    lead_ids = {f.lead_id for f in findings if f.lead_id}
    bundle_ids: set[str] = set()
    for f in findings:
        for ref in f.evidence_refs:
            if ref.startswith("bundle:"):
                bundle_ids.add(ref.split(":", 1)[1].split("#", 1)[0])
    inference_runs = (
        session.scalars(select(InferenceRun).where(InferenceRun.id.in_(inference_ids))).all()
        if inference_ids
        else []
    )
    for run in inference_runs:
        bundle_ids.update(run.input_bundle_ids)
    leads = session.scalars(select(Lead).where(Lead.id.in_(lead_ids))).all() if lead_ids else []
    investigation_leads = session.scalars(select(Lead).where(Lead.task_id == task.id)).all()
    return {
        "task": _row(
            task,
            (
                "id",
                "title",
                "status",
                "priority",
                "score",
                "category",
                "confidence",
                "pr_url",
                "ci_status",
                "created_at",
            ),
        ),
        "executions": [
            _row(
                e,
                (
                    "id",
                    "attempt",
                    "status",
                    "agent",
                    "branch",
                    "commit_sha",
                    "pr_url",
                    "error",
                    "started_at",
                    "finished_at",
                ),
            )
            for e in session.scalars(
                select(ExecutionRun)
                .where(ExecutionRun.task_id == task.id)
                .order_by(ExecutionRun.started_at)
            ).all()
        ],
        "reviews": [
            _row(
                r,
                (
                    "id",
                    "reviewer_name",
                    "reviewer_role",
                    "decision",
                    "reason_code",
                    "comment",
                    "elevated_ack",
                    "created_at",
                ),
            )
            for r in session.scalars(
                select(Review).where(Review.task_id == task.id).order_by(Review.created_at)
            ).all()
        ],
        "findings": [
            _row(
                f,
                (
                    "id",
                    "source",
                    "source_run_id",
                    "category",
                    "rule_id",
                    "title",
                    "severity",
                    "confidence",
                    "path",
                    "line",
                    "signal",
                    "evidence_refs",
                    "lead_id",
                    "created_at",
                ),
            )
            for f in findings
        ],
        "inference_runs": [
            _row(
                r,
                (
                    "id",
                    "slot",
                    "previous_run_id",
                    "model",
                    "prompt_id",
                    "prompt_version",
                    "input_bundle_ids",
                    "cost_usd",
                    "status",
                    "started_at",
                ),
            )
            for r in inference_runs
        ],
        "audit_runs": [
            _row(
                r, ("id", "mode", "commit_sha", "base_sha", "tool_versions", "status", "started_at")
            )
            for r in (
                session.scalars(select(AuditRun).where(AuditRun.id.in_(audit_ids))).all()
                if audit_ids
                else []
            )
        ],
        "bundles": [
            _row(b, ("id", "service_id", "window_start", "window_end", "stats", "raw_refs"))
            for b in (
                session.scalars(
                    select(AnalysisBundle).where(AnalysisBundle.id.in_(bundle_ids))
                ).all()
                if bundle_ids
                else []
            )
        ],
        "leads": [
            _row(
                lead,
                (
                    "id",
                    "title",
                    "state",
                    "age",
                    "signal",
                    "originating_run_id",
                    "last_run_id",
                    "source",
                ),
            )
            for lead in [*leads, *investigation_leads]
        ],
        "audit_log": [
            _row(
                a,
                (
                    "action",
                    "from_state",
                    "to_state",
                    "actor_type",
                    "actor_id",
                    "details",
                    "created_at",
                ),
            )
            for a in session.scalars(
                select(AuditLog)
                .where(AuditLog.entity_type == "task", AuditLog.entity_id == task.id)
                .order_by(AuditLog.id)
            ).all()
        ],
    }
