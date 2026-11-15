"""Execution of approved tasks by sandboxed subagents (spec 5.7).

Hard gates, checked before any code is touched and again before anything is pushed:

1. the task is ``approved`` and its latest review is an approval by a human reviewer/admin;
2. ``excluded`` protected paths are never changed; ``elevated`` ones require an admin
   approval with ``elevated_ack``;
3. tasks touching the same files as a running execution are serialized;
4. the diff is secret-scanned and must pass the repository's configured checks.

The long-running part (clone, agent, checks) runs *outside* any DB transaction; state
changes are short transactions, so a crash leaves the task in a consistent state.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsmith import audit_log, notifications, queue
from opsmith.audit_log import Actor, transition_task
from opsmith.config import Settings
from opsmith.db import Database, utcnow
from opsmith.executor.agents import AgentBudget, AgentResult, AgentTask, CodingAgent
from opsmith.executor.pr import render_pr_body
from opsmith.executor.sandbox import Workspace, make_sandbox
from opsmith.gitutil import GitError, head_sha, run_git, staged_added_lines, worktree_changes
from opsmith.models import ExecutionRun, Finding, Project, Repository, Review, Task
from opsmith.observability import METRICS
from opsmith.protection import classify, protected_hits
from opsmith.redaction import SECRET_DETECTORS
from opsmith.schemas import ProjectSettings
from opsmith.scm import SCMError, SCMRegistry

log = logging.getLogger(__name__)
HUMAN_APPROVER_ROLES = frozenset({"reviewer", "admin"})
APPROVE_DECISIONS = frozenset({"approve", "approve_with_edits"})


class ApprovalError(RuntimeError):
    pass


def latest_review(session: Session, task_id: str) -> Review | None:
    return session.scalar(
        select(Review)
        .where(Review.task_id == task_id)
        .order_by(Review.created_at.desc(), Review.id.desc())
        .limit(1)
    )


def assert_approved(session: Session, task: Task, review_id: str | None = None) -> Review:
    """The single gate that every code change goes through."""
    if task.status not in {"approved", "executing"}:
        raise ApprovalError(f"task {task.id} is {task.status}, not approved")
    review = latest_review(session, task.id)
    if review is None or review.decision not in APPROVE_DECISIONS:
        raise ApprovalError(f"task {task.id} has no recorded human approval")
    if review_id is not None and review.id != review_id:
        raise ApprovalError("approval superseded by a newer review")
    if review.reviewer_role not in HUMAN_APPROVER_ROLES:
        raise ApprovalError("approval was not made by a human reviewer")
    if task.protection == "excluded":
        raise ApprovalError("task touches paths excluded from autonomous changes")
    if task.protection == "elevated" and not (
        review.elevated_ack and review.reviewer_role == "admin"
    ):
        raise ApprovalError("protected paths require an elevated admin approval")
    return review


@dataclass
class AttemptOutcome:
    success: bool
    retryable: bool
    error: str | None = None
    branch: str | None = None
    commit_sha: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    changed_files: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    agent: AgentResult | None = None


AgentFactory = Callable[[ProjectSettings], CodingAgent]


class ExecutionService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        scm: SCMRegistry,
        agent_factories: dict[str, AgentFactory],
        *,
        base_url: str = "",
        max_attempts: int = 2,
    ) -> None:
        self.db = db
        self.settings = settings
        self.scm = scm
        self.agent_factories = agent_factories
        self.base_url = base_url
        self.max_attempts = max_attempts

    # ------------------------------------------------------------------ entry point
    def execute(self, task_id: str, review_id: str) -> dict[str, Any]:
        with self.db.session() as s:
            task = s.get(Task, task_id)
            if task is None:
                raise queue.PermanentFailure(f"task {task_id} not found")
            if task.status in {"pr_open", "merged", "verified"}:
                return {"skipped": f"already {task.status}"}
            if task.status == "executing":
                running = s.scalar(
                    select(ExecutionRun).where(
                        ExecutionRun.task_id == task.id, ExecutionRun.status == "running"
                    )
                )
                if running is not None:
                    # A crashed worker left the run behind; close it and continue.
                    running.status = "abandoned"
                    running.finished_at = utcnow()
            try:
                review = assert_approved(s, task, review_id)
            except ApprovalError as exc:
                audit_log.record(
                    s,
                    tenant_id=task.tenant_id,
                    entity_type="task",
                    entity_id=task.id,
                    action="execution.refused",
                    details={"reason": str(exc)},
                )
                return {"refused": str(exc)}
            project = s.get(Project, task.project_id)
            assert project is not None
            settings = ProjectSettings.model_validate(project.settings)
            self._check_concurrency(s, task, settings)
            if task.status == "approved":
                transition_task(s, task, "executing", details={"review_id": review.id})
            reviewer = review.reviewer_name
        outcome: AttemptOutcome | None = None
        for attempt in range(1, self.max_attempts + 1):
            run_id = self._start_run(task_id, review_id, attempt)
            outcome = self._attempt(task_id, run_id, settings, reviewer)
            self._finish_run(run_id, outcome)
            if outcome.success or not outcome.retryable:
                break
        assert outcome is not None
        self._finalize(task_id, outcome)
        return {"success": outcome.success, "pr_url": outcome.pr_url, "error": outcome.error}

    # ------------------------------------------------------------------ gates
    def _check_concurrency(self, s: Session, task: Task, settings: ProjectSettings) -> None:
        running = s.scalars(
            select(Task).where(
                Task.project_id == task.project_id, Task.status == "executing", Task.id != task.id
            )
        ).all()
        if len(running) >= settings.budgets.max_concurrent_executions:
            raise queue.RetryLater(60, "execution concurrency limit reached")
        mine = set(task.paths)
        for other in running:
            overlap = mine & set(other.paths)
            if overlap and other.repository_id == task.repository_id:
                audit_log.record(
                    s,
                    tenant_id=task.tenant_id,
                    entity_type="task",
                    entity_id=task.id,
                    action="execution.serialized",
                    details={"conflicts_with": other.id, "paths": sorted(overlap)[:10]},
                )
                raise queue.RetryLater(120, f"waiting for {other.id} (same files)")

    # ------------------------------------------------------------------ runs
    def _start_run(self, task_id: str, review_id: str, attempt: int) -> str:
        with self.db.session() as s:
            task = s.get(Task, task_id)
            assert task is not None
            project = s.get(Project, task.project_id)
            assert project is not None
            agent_name = ProjectSettings.model_validate(project.settings).executor_agent
            run = ExecutionRun(
                tenant_id=task.tenant_id,
                project_id=task.project_id,
                task_id=task.id,
                review_id=review_id,
                attempt=attempt,
                agent=agent_name,
                branch=task.branch_name,
            )
            s.add(run)
            s.flush()
            return run.id

    def _finish_run(self, run_id: str, out: AttemptOutcome) -> None:
        with self.db.session() as s:
            run = s.get(ExecutionRun, run_id)
            assert run is not None
            run.status = "succeeded" if out.success else "failed"
            run.error = out.error
            run.branch = out.branch or run.branch
            run.commit_sha = out.commit_sha
            run.pr_url, run.pr_number = out.pr_url, out.pr_number
            run.changed_files = out.changed_files
            run.checks = out.checks
            agent_steps = out.agent.steps if out.agent else []
            run.steps = out.steps[:1] + agent_steps + out.steps[1:]  # checkout, agent, push, PR
            if out.agent:
                run.input_tokens = out.agent.input_tokens
                run.output_tokens = out.agent.output_tokens
                run.cost_usd = out.agent.cost_usd
            run.finished_at = utcnow()
            METRICS.inc("opsmith_executions_total", outcome=run.status)

    def _attempt(
        self, task_id: str, run_id: str, settings: ProjectSettings, reviewer: str
    ) -> AttemptOutcome:
        with self.db.session() as s:
            task = s.get(Task, task_id)
            assert task is not None
            repo = s.get(Repository, task.repository_id) if task.repository_id else None
            if repo is None:
                return AttemptOutcome(False, False, "task has no target repository")
            s.expunge(repo)
            agent_task = self._agent_task(s, task)
            branch = task.branch_name
            s.expunge(task)
        tmp = Path(tempfile.mkdtemp(prefix="opsmith-exec-"))
        steps: list[dict[str, Any]] = []
        started = time.monotonic()
        try:
            provider = self.scm.for_provider(repo.scm_provider)
            workdir = provider.checkout(repo, tmp / "repo")
            # The checkout's remote is removed so the sandboxed agent holds no SCM handle.
            run_git(["remote", "remove", "origin"], workdir)
            run_git(["checkout", "-B", branch], workdir)
            exclude = workdir / ".git" / "info" / "exclude"
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with exclude.open("a") as fh:  # sandbox HOME lives in the checkout; never commit it
                fh.write("\n.opsmith-home/\n")
            steps.append({"step": "checkout", "branch": branch, "base": head_sha(workdir)})
            sandbox = make_sandbox(
                self.settings.sandbox_backend, workdir, self.settings.sandbox_image
            )
            checks = {
                "build": repo.build_command or "",
                "lint": repo.lint_command or "",
                "test": repo.test_command or "",
            }
            ws = Workspace(
                sandbox, checks, timeout=min(900, settings.budgets.max_execution_seconds)
            )
            agent = self.agent_factories[settings.executor_agent](settings)
            budget = AgentBudget(
                max_iterations=settings.budgets.max_agent_iterations,
                max_tokens=settings.budgets.max_tokens_per_run,
            )
            result = agent.run(agent_task, ws, budget)
            if time.monotonic() - started > settings.budgets.max_execution_seconds:
                return AttemptOutcome(
                    False, False, "execution time budget exceeded", steps=steps, agent=result
                )
            changed = [f for f in worktree_changes(workdir) if not f.startswith(".opsmith-home")]
            if not result.success:
                return AttemptOutcome(
                    False,
                    result.retryable,
                    f"agent: {result.summary}",
                    changed_files=changed,
                    steps=steps,
                    agent=result,
                )
            if not changed:
                return AttemptOutcome(
                    False,
                    False,
                    "agent reported success but changed nothing",
                    steps=steps,
                    agent=result,
                )
            gate = self._diff_gates(task_id, changed, settings, workdir)
            if gate:
                return AttemptOutcome(
                    False, False, gate, changed_files=changed, steps=steps, agent=result
                )
            # Authoritative verification by the platform, independent of the agent's claims.
            check_rows: list[dict[str, Any]] = []
            for name in ("build", "lint", "test"):
                if checks[name]:
                    res = sandbox.run(checks[name], timeout=ws.timeout)
                    check_rows.append(
                        {
                            "name": name,
                            "command": checks[name],
                            "exit_code": res.exit_code,
                            "output": res.output[-3000:],
                        }
                    )
            if any(c["exit_code"] != 0 for c in check_rows):
                failed = [c["name"] for c in check_rows if c["exit_code"] != 0]
                return AttemptOutcome(
                    False,
                    True,
                    f"checks failed: {', '.join(failed)}",
                    changed_files=changed,
                    checks=check_rows,
                    steps=steps,
                    agent=result,
                )
            shutil.rmtree(workdir / ".opsmith-home", ignore_errors=True)
            run_git(["add", "-A"], workdir)
            message = f"{agent_task.title}\n\n{result.summary}\n\nOpsmith-Task: {task_id}\n"
            run_git(
                ["commit", "-m", message],
                workdir,
                author=(self.settings.bot_name, self.settings.bot_email),
            )
            sha = head_sha(workdir)
            provider.push_branch(repo, workdir, branch)
            steps.append({"step": "push", "branch": branch, "sha": sha})
            with self.db.session() as s:
                task_row = s.get(Task, task_id)
                run_row = s.get(ExecutionRun, run_id)
                assert task_row is not None
                assert run_row is not None
                # Re-check the approval right before the outward-facing action.
                assert_approved(s, task_row)
                body = render_pr_body(
                    task_row, run_row, check_rows, base_url=self.base_url, reviewer=reviewer
                )
                title = f"[opsmith] {task_row.title}"[:250]
            pr = provider.create_pull_request(repo, branch=branch, title=title, body=body)
            steps.append({"step": "pull_request", "url": pr.url})
            return AttemptOutcome(
                True,
                False,
                None,
                branch,
                sha,
                pr.url,
                pr.number,
                changed,
                check_rows,
                steps,
                result,
            )
        except ApprovalError as exc:
            return AttemptOutcome(False, False, f"approval: {exc}", steps=steps)
        except (GitError, SCMError, OSError) as exc:
            log.warning("execution attempt failed", extra={"task_id": task_id, "error": str(exc)})
            return AttemptOutcome(False, True, f"infrastructure: {exc}", steps=steps)
        except Exception as exc:  # agent/provider errors must still surface on the task
            log.exception("execution attempt crashed", extra={"task_id": task_id})
            return AttemptOutcome(False, False, f"error: {type(exc).__name__}: {exc}", steps=steps)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _diff_gates(
        self, task_id: str, changed: list[str], settings: ProjectSettings, workdir: Path
    ) -> str | None:
        hits = protected_hits(changed, settings.protected_paths)
        level = classify(changed, settings.protected_paths)
        with self.db.session() as s:
            task = s.get(Task, task_id)
            assert task is not None
            if level == "excluded":
                return f"diff touches excluded paths: {sorted({p for p, _ in hits})[:5]}"
            if level == "elevated":
                review = latest_review(s, task_id)
                if not (review and review.elevated_ack and review.reviewer_role == "admin"):
                    task.protection = "elevated"
                    return (
                        "diff touches protected paths that need elevated approval: "
                        f"{sorted({p for p, _ in hits})[:5]}"
                    )
        introduced = []
        for path, line_no, text in staged_added_lines(workdir):
            for det in SECRET_DETECTORS:
                if det.pattern.search(text):
                    introduced.append(f"{path}:{line_no}")
                    break
        if introduced:
            return f"diff introduces credential-like values: {sorted(introduced)[:5]}"
        return None

    def _agent_task(self, s: Session, task: Task) -> AgentTask:
        findings = s.scalars(select(Finding).where(Finding.task_id == task.id)).all()
        return AgentTask(
            id=task.id,
            title=task.title,
            category=task.category,
            problem_statement=task.problem_statement,
            proposed_approach=task.proposed_approach,
            acceptance_criteria=list(task.acceptance_criteria),
            paths=list(task.paths),
            findings=[
                {
                    "rule_id": f.rule_id,
                    "path": f.path,
                    "line": f.line,
                    "title": f.title,
                    "category": f.category,
                    "snippet": f.extra.get("snippet", ""),
                }
                for f in findings
            ],
        )

    # ------------------------------------------------------------------ finalize
    def _finalize(self, task_id: str, out: AttemptOutcome) -> None:
        with self.db.session() as s:
            task = s.get(Task, task_id)
            assert task is not None
            agent_actor = Actor("agent", "executor")
            if out.success:
                task.pr_url, task.pr_number = out.pr_url, out.pr_number
                task.ci_status = "pending"
                task.execution_report = {
                    "changed_files": out.changed_files,
                    "checks": [{k: v for k, v in c.items() if k != "output"} for c in out.checks],
                }
                transition_task(
                    s, task, "pr_open", actor=agent_actor, details={"pr_url": out.pr_url}
                )
                notifications.notify(
                    s,
                    tenant_id=task.tenant_id,
                    project_id=task.project_id,
                    event="task.pr_open",
                    subject=f"PR opened: {task.title}",
                    link=out.pr_url,
                )
                return
            task.execution_report = {
                "error": out.error,
                "changed_files": out.changed_files,
                "checks": out.checks,
                "agent_summary": out.agent.summary if out.agent else None,
                "at": utcnow().isoformat(),
            }
            transition_task(s, task, "failed", actor=agent_actor, details={"error": out.error})
            transition_task(
                s,
                task,
                "in_review",
                details={"reason": "execution failed; returned to review queue"},
            )
            notifications.notify(
                s,
                tenant_id=task.tenant_id,
                project_id=task.project_id,
                event="task.execution_failed",
                subject=f"Execution failed: {task.title}",
                body=out.error or "",
            )
