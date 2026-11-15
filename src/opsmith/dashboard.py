"""Dashboard aggregates: pipeline health, run history, cost, open leads, task funnel."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from opsmith.db import utcnow
from opsmith.models import (
    AuditRun,
    ExecutionRun,
    InferenceRun,
    Job,
    Lead,
    Project,
    Task,
    UsageRecord,
)

FUNNEL = (
    "proposed",
    "in_review",
    "approved",
    "executing",
    "pr_open",
    "merged",
    "verified",
    "rejected",
    "deferred",
    "investigating",
    "failed",
    "regressed",
    "closed",
)


def project_summary(session: Session, project: Project) -> dict[str, Any]:
    now = utcnow()
    day, week = now - timedelta(days=1), now - timedelta(days=7)
    pid = project.id

    def grouped(model: Any, column: Any, *conds: Any) -> dict[str, int]:
        rows = session.execute(
            select(column, func.count()).select_from(model).where(*conds).group_by(column)
        ).all()
        return {str(k): int(v) for k, v in rows}

    def cost(since: Any) -> dict[str, float]:
        rows = session.execute(
            select(
                UsageRecord.stage,
                func.sum(UsageRecord.cost_usd),
                func.avg(UsageRecord.latency_ms),
                func.count(),
            )
            .where(UsageRecord.project_id == pid, UsageRecord.created_at >= since)
            .group_by(UsageRecord.stage)
        ).all()
        return {str(stage): round(float(total or 0), 4) for stage, total, _lat, _n in rows}

    latency_rows = session.execute(
        select(UsageRecord.stage, func.avg(UsageRecord.latency_ms))
        .where(UsageRecord.project_id == pid, UsageRecord.created_at >= week)
        .group_by(UsageRecord.stage)
    ).all()
    funnel = grouped(Task, Task.status, Task.project_id == pid)
    return {
        "project": {"id": project.id, "name": project.name},
        "jobs": grouped(Job, Job.status, Job.tenant_id == project.tenant_id, Job.created_at >= day),
        "dead_jobs": int(
            session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.tenant_id == project.tenant_id, Job.status == "dead")
            )
            or 0
        ),
        "tasks": {s: funnel.get(s, 0) for s in FUNNEL},
        "leads": grouped(Lead, Lead.state, Lead.project_id == pid),
        "escalated_leads": int(
            session.scalar(
                select(func.count())
                .select_from(Lead)
                .where(Lead.project_id == pid, Lead.escalated.is_(True))
            )
            or 0
        ),
        "cost_24h": cost(day),
        "cost_7d": cost(week),
        "latency_ms_7d": {str(s): int(v or 0) for s, v in latency_rows},
        "inference_runs": grouped(
            InferenceRun, InferenceRun.status, InferenceRun.project_id == pid
        ),
        "audit_runs": grouped(AuditRun, AuditRun.status, AuditRun.project_id == pid),
        "executions": grouped(ExecutionRun, ExecutionRun.status, ExecutionRun.project_id == pid),
    }
