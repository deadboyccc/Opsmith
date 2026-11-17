"""Scheduled repository audits (spec 5.3): deterministic analyzers + LLM triage, full or
incremental (changed files since the last audited commit)."""

from __future__ import annotations

import fcntl
import logging
import os
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsmith import audit_log, queue
from opsmith.audits.base import (
    AnalyzerError,
    AnalyzerFactory,
    AuditContext,
    RawFinding,
    analyzers_for,
    iter_repo_files,
)
from opsmith.db import utcnow
from opsmith.gitutil import GitError, changed_files, head_sha
from opsmith.llm import LLMError, LLMGateway
from opsmith.models import AuditRun, Finding, Project, Repository
from opsmith.observability import METRICS
from opsmith.schemas import ProjectSettings, TriageOutput
from opsmith.scm import SCMRegistry

log = logging.getLogger(__name__)
TRIAGE_BATCH = 40
_STRENGTH = {True: 1.0, False: 0.6}  # deterministic tool vs LLM-only evidence


@contextmanager
def checkout_lock(workdir: Path, repository_id: str) -> Iterator[None]:
    """Hold the repository's checkout for one audit or check. A second run that starts
    meanwhile (a manual trigger during a scheduled run) would fetch, reset and clean the tree
    the first is still reading; it is deferred instead. The lock is an advisory file lock, so
    it also holds between worker processes that share the data directory, and the kernel
    drops it if the worker dies."""
    workdir.mkdir(parents=True, exist_ok=True)
    fd = os.open(workdir / f"{repository_id}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise queue.RetryLater(60, "another audit of this repository is running") from None
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


class AuditRunner:
    def __init__(
        self,
        gateway: LLMGateway,
        scm: SCMRegistry,
        workdir: Path,
        analyzer_factory: AnalyzerFactory = analyzers_for,
    ) -> None:
        self.gateway = gateway
        self.scm = scm
        self.workdir = workdir
        self.analyzer_factory = analyzer_factory

    def run(
        self,
        session: Session,
        repository_id: str,
        *,
        mode: str = "auto",
        audit_types: list[str] | None = None,
    ) -> AuditRun:
        repo = session.get(Repository, repository_id)
        if repo is None:
            raise queue.PermanentFailure(f"repository {repository_id} not found")
        project = session.get(Project, repo.project_id)
        assert project is not None
        settings = ProjectSettings.model_validate(project.settings)
        types = audit_types or list(repo.audit_types)
        started = utcnow()
        # One checkout directory per repository, so one audit of it at a time on this host.
        with self._checkout_lock(repo.id):
            path = self.scm.for_provider(repo.scm_provider).checkout(repo, self.workdir / repo.id)
            head = head_sha(path)
            all_files = iter_repo_files(path)
            mode = self._resolve_mode(session, repo, settings, mode)
            files, base = all_files, None
            if mode == "incremental" and repo.last_audited_sha:
                try:
                    changed = changed_files(path, repo.last_audited_sha, head)
                    existing = set(all_files)
                    files = [f for f in changed if f in existing]
                    base = repo.last_audited_sha
                except GitError:
                    mode = "full"  # base commit unreachable (force-push/shallow clone)
            else:
                mode = "full"

            ctx = AuditContext(
                repo_path=path,
                repository_id=repo.id,
                files=files,
                all_files=all_files,
                settings=settings,
                commit_sha=head,
            )
            raws: list[RawFinding] = []
            tools: dict[str, str] = {}
            failed: list[str] = []
            with METRICS.timer("opsmith_stage_seconds", stage="audit"):
                for analyzer in self.analyzer_factory(types):
                    if not analyzer.available():
                        tools[analyzer.name] = "unavailable"
                        continue
                    try:
                        raws.extend(analyzer.analyze(ctx))
                        tools[analyzer.name] = analyzer.version()
                    except Exception as exc:  # one broken tool must not sink the run
                        if isinstance(exc, AnalyzerError):
                            raws.extend(exc.findings)  # what it found before it failed
                        tools[analyzer.name] = f"error: {exc}"[:200]
                        failed.append(analyzer.name)
                        log.exception("analyzer failed", extra={"analyzer": analyzer.name})

        # File names are bytes to git and need not be UTF-8; nothing below can store or send
        # the lone surrogates Python reads them as.
        raws = [raw.cleaned() for raw in raws]
        # The checkout and the analyzers (minutes on a large repository) ran before anything
        # was written, so the audit's write transaction only starts here.
        run = AuditRun(
            tenant_id=repo.tenant_id,
            project_id=project.id,
            repository_id=repo.id,
            audit_types=types,
            mode=mode,
            commit_sha=head,
            base_sha=base,
            files_analyzed=len(files),
            started_at=started,
        )
        session.add(run)
        session.flush()
        decisions = self._triage(session, project, run, raws)
        places: dict[str, list[tuple[Any, Any]]] = {}  # fingerprint -> (line, column) seen
        kept = 0
        for idx, raw in enumerate(raws):
            earlier = places.setdefault(raw.fingerprint(repo.id), [])
            place = (raw.line, raw.extra.get("column"))
            if place in earlier:
                continue  # same rule, file, text and position: one finding, whoever reports it
            # The fingerprint ignores line numbers, so the same rule on identical lines of one
            # file collides. Those are distinct findings and each is kept: the first under the
            # plain fingerprint, later ones numbered in report order.
            fp = raw.fingerprint(repo.id, occurrence=len(earlier))
            earlier.append(place)
            keep, confidence, rationale, severity = decisions.get(
                idx, (True, raw.confidence, "not triaged", None)
            )
            session.add(
                Finding(
                    tenant_id=repo.tenant_id,
                    project_id=project.id,
                    source=f"audit:{raw.analyzer}"[:80],
                    source_run_id=run.id,
                    category=raw.category,
                    # Paths and tool output come from the repository; clip them to the column
                    # sizes so one over-long value cannot fail the whole audit on PostgreSQL.
                    rule_id=raw.rule_id[:120],
                    title=raw.title[:300],
                    description=raw.description or "",
                    severity=severity or raw.severity,
                    confidence=round(confidence, 3),
                    evidence_refs=[f"audit:{run.id}#{fp[:12]}"],
                    repository_id=repo.id,
                    path=raw.path[:500] if raw.path else raw.path,
                    line=raw.line,
                    fingerprint=fp,
                    evidence_strength=_STRENGTH[True],
                    status="new" if keep else "suppressed",
                    status_reason=None if keep else f"triage: {rationale}"[:100],
                    extra={
                        "snippet": raw.snippet,
                        "triage": rationale,
                        "commit": head,
                        **raw.extra,
                    },
                )
            )
            kept += int(keep)
        run.tool_versions = tools
        run.findings_count = kept
        run.status = "succeeded"
        run.finished_at = utcnow()
        if failed:
            # Stays on the finished run next to the per-tool detail in ``tool_versions``. The
            # audited commit is not advanced: the next incremental run diffs from the same
            # base again, so the files this run's failed analyzers missed are not skipped.
            run.error = f"analyzers failed: {', '.join(failed)}"
        else:
            repo.last_audited_sha = head
        audit_log.record(
            session,
            tenant_id=repo.tenant_id,
            entity_type="audit_run",
            entity_id=run.id,
            action="audit.completed",
            details={
                "mode": mode,
                "files": len(files),
                "raw": len(raws),
                "kept": kept,
                "tools": tools,
            },
        )
        queue.enqueue(
            session,
            tenant_id=repo.tenant_id,
            kind="aggregate",
            payload={"project_id": project.id},
            idempotency_key=f"aggregate:audit:{run.id}",
        )
        return run

    def _checkout_lock(self, repository_id: str) -> AbstractContextManager[None]:
        return checkout_lock(self.workdir, repository_id)

    @staticmethod
    def _resolve_mode(
        session: Session, repo: Repository, settings: ProjectSettings, mode: str
    ) -> str:
        if mode in {"full", "incremental"}:
            return mode
        if not repo.last_audited_sha:
            return "full"
        last_full = session.scalar(
            select(AuditRun.started_at)
            .where(
                AuditRun.repository_id == repo.id,
                AuditRun.mode == "full",
                AuditRun.status == "succeeded",
            )
            .order_by(AuditRun.started_at.desc())
            .limit(1)
        )
        if last_full is None or utcnow() - last_full > timedelta(
            days=settings.audit_full_every_days
        ):
            return "full"
        return "incremental"

    def _triage(
        self, session: Session, project: Project, run: AuditRun, raws: list[RawFinding]
    ) -> dict[int, tuple[bool, float, str, str | None]]:
        decisions: dict[int, tuple[bool, float, str, str | None]] = {}
        for start in range(0, len(raws), TRIAGE_BATCH):
            batch = raws[start : start + TRIAGE_BATCH]
            items: list[dict[str, Any]] = [
                {
                    "index": start + i,
                    "analyzer": r.analyzer,
                    "rule_id": r.rule_id,
                    "title": r.title,
                    "path": r.path,
                    "line": r.line,
                    "snippet": r.snippet,
                    "severity": r.severity,
                    "confidence": r.confidence,
                    "placeholder_value": bool(r.extra.get("placeholder")),
                }
                for i, r in enumerate(batch)
            ]
            try:
                result = self.gateway.structured(
                    session,
                    project=project,
                    stage="audit_triage",
                    prompt_id="audit.triage",
                    variables={"findings": items},
                    schema=TriageOutput,
                    run_id=run.id,
                )
            except LLMError as exc:
                # Triage is an enhancement: keep deterministic findings untouched on failure.
                log.warning("audit triage unavailable", extra={"error": str(exc)})
                continue
            run.input_tokens += result.input_tokens
            run.output_tokens += result.output_tokens
            run.cost_usd = round(run.cost_usd + result.cost_usd, 6)
            valid = {item["index"] for item in items}
            for d in result.output.decisions:
                if d.index in valid:
                    decisions[d.index] = (d.keep, d.confidence, d.rationale, d.severity)
        return decisions
