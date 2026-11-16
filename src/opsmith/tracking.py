"""PR status tracking and post-merge verification (spec 5.7)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsmith import audit_log, notifications, queue
from opsmith.audit_log import transition_task
from opsmith.connectors.files import parse_ts
from opsmith.db import utcnow
from opsmith.models import AnalysisBundle, AuditRun, Finding, Project, Repository, Task
from opsmith.schemas import ProjectSettings
from opsmith.scm import SCMError, SCMRegistry

log = logging.getLogger(__name__)
# Hourly retries while there is no post-merge data: 48 covers a daily audit and any normal
# analysis interval. After that the verify job fails (dead, audited, notified).
MAX_VERIFY_DEFERRALS = 48


class PRTracker:
    def __init__(self, scm: SCMRegistry) -> None:
        self.scm = scm

    def run(self, session: Session, project_id: str) -> dict[str, Any]:
        project = session.get(Project, project_id)
        assert project is not None
        settings = ProjectSettings.model_validate(project.settings)
        tasks = session.scalars(
            select(Task).where(Task.project_id == project_id, Task.status == "pr_open")
        ).all()
        updated: dict[str, str] = {}
        for task in tasks:
            repo = session.get(Repository, task.repository_id) if task.repository_id else None
            if repo is None or task.pr_number is None:
                continue
            try:
                status = self.scm.for_provider(repo.scm_provider).get_pull_request(
                    repo, task.pr_number
                )
            except SCMError as exc:
                log.warning("PR status unavailable", extra={"task_id": task.id, "error": str(exc)})
                continue
            if status.ci != task.ci_status:
                audit_log.record(
                    session,
                    tenant_id=task.tenant_id,
                    entity_type="task",
                    entity_id=task.id,
                    action="task.ci_status",
                    details={"from": task.ci_status, "to": status.ci},
                )
                task.ci_status = status.ci
                if status.ci == "failure":
                    notifications.notify(
                        session,
                        tenant_id=task.tenant_id,
                        project_id=project_id,
                        event="task.ci_failed",
                        subject=f"CI failed on PR for {task.title}",
                        link=task.pr_url,
                    )
            task.execution_report = {
                **task.execution_report,
                "review_comments": status.review_comments,
            }
            if status.state == "merged":
                task.merged_at = utcnow()
                transition_task(session, task, "merged", details={"pr": task.pr_url})
                if settings.post_merge_verification:
                    queue.enqueue(
                        session,
                        tenant_id=task.tenant_id,
                        kind="verify",
                        payload={"task_id": task.id},
                        # One verification per merge: a task merged again after a
                        # regression is verified again.
                        idempotency_key=f"verify:{task.id}:{task.merged_at.isoformat()}",
                        run_after=utcnow() + timedelta(hours=settings.verification_delay_hours),
                    )
                updated[task.id] = "merged"
            elif status.state == "closed":
                transition_task(
                    session, task, "closed", details={"pr": task.pr_url, "reason": "PR closed"}
                )
                updated[task.id] = "closed"
        return {"checked": len(tasks), "updated": updated}


class Verifier:
    """After merge, watch the task's signals: recurrence -> ``regressed``, else ``verified``."""

    def run(self, session: Session, task_id: str, attempt: int = 0) -> dict[str, Any]:
        task = session.get(Task, task_id)
        if task is None or task.status != "merged" or task.merged_at is None:
            return {"skipped": True}
        findings = session.scalars(select(Finding).where(Finding.task_id == task.id)).all()
        signals = {f.signal for f in findings if f.signal}
        services = {f.service_id for f in findings if f.service_id}
        static_fps = {f.fingerprint for f in findings if f.source.startswith("audit:")}
        evidence: list[str] = []
        observed = False
        if signals and services:
            bundles = session.scalars(
                select(AnalysisBundle).where(
                    AnalysisBundle.service_id.in_(services),
                    AnalysisBundle.window_end > task.merged_at,
                )
            ).all()
            for b in bundles:
                observed = True
                for t in b.log_templates:
                    errors = t["severities"].get("error", 0) + t["severities"].get("critical", 0)
                    # only occurrences *after* the merge count as recurrence
                    if (
                        t["signal"] in signals
                        and errors
                        and parse_ts(t["last_seen"]) > task.merged_at
                    ):
                        evidence.append(t["ref"])
                if b.window_start >= task.merged_at:
                    for feat in b.metric_features:
                        if feat["signal"] in signals and feat.get("anomalous"):
                            evidence.append(feat["ref"])
        if static_fps and task.repository_id:
            later_audit = session.scalar(
                select(AuditRun)
                .where(
                    AuditRun.repository_id == task.repository_id,
                    AuditRun.status == "succeeded",
                    AuditRun.started_at >= task.merged_at,
                )
                .limit(1)
            )
            if later_audit is not None:
                observed = True
                again = session.scalars(
                    select(Finding).where(
                        Finding.source_run_id.in_(
                            select(AuditRun.id).where(
                                AuditRun.repository_id == task.repository_id,
                                AuditRun.started_at >= task.merged_at,
                            )
                        ),
                        Finding.fingerprint.in_(static_fps),
                    )
                ).all()
                evidence.extend(f"finding:{f.id}" for f in again)
        if not observed:
            if attempt < MAX_VERIFY_DEFERRALS:
                raise queue.RetryLater(3600, "waiting for post-merge signal data")
            raise queue.PermanentFailure(
                f"no post-merge signals or audit for task {task.id} within "
                f"{MAX_VERIFY_DEFERRALS} hours; check it by hand, or retry this job once data "
                "has arrived"
            )
        outcome = "regressed" if evidence else "verified"
        transition_task(session, task, outcome, details={"evidence": evidence[:10]})
        if outcome == "regressed":
            notifications.notify(
                session,
                tenant_id=task.tenant_id,
                project_id=task.project_id,
                event="task.regressed",
                subject=f"Regression after merge: {task.title}",
                body="\n".join(evidence[:10]),
            )
        return {"outcome": outcome, "evidence": evidence[:10]}
