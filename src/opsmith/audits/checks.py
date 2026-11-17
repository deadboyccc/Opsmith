"""Checks: standing instructions a model carries out against a repository's code on a schedule
(ADR-019).

A project administrator writes a check (``ProjectSettings.checks``): what to look for, in which
files, on which cron. A run checks the repository out as an audit does, sends the files in
scope to the model of the ``check`` stage together with the instruction, and keeps each problem
the model reports only if it quotes the file it names. What is kept is stored like audit
findings, under an ``AuditRun`` of mode ``check``, and the aggregator turns it into tasks that a
human reviews like any other. A check never runs repository code and never changes anything.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from opsmith import audit_log, notifications, queue
from opsmith.audits.base import MAX_FILE_BYTES, iter_repo_files, printable, source_lines
from opsmith.audits.runner import checkout_lock
from opsmith.db import utcnow
from opsmith.gitutil import head_sha
from opsmith.llm import BudgetExceeded, LLMError, LLMGateway
from opsmith.models import AuditRun, Finding, Project, Repository
from opsmith.observability import METRICS
from opsmith.protection import path_matches
from opsmith.redaction import Redactor
from opsmith.schemas import (
    SEVERITY_RANK,
    Check,
    CheckFinding,
    CheckOutput,
    ProjectSettings,
    clean_text,
)
from opsmith.scm import SCMRegistry

log = logging.getLogger(__name__)
BATCH_CHARS = 120_000  # numbered file text per model call; a larger scope takes several calls
MAX_EVIDENCE_LINES = 20  # a longer "quote" does not point at a problem
MIN_EVIDENCE_CHARS = 6  # a shorter one ("}", "x") matches almost anywhere and proves nothing
MAX_SNIPPET_CHARS = 1_000
MAX_NOTES = 20
MAX_NOTE_CHARS = 500
STRENGTH = 0.6  # a model's reading of the code, as for model-only evidence elsewhere
SUBJECT_CHARS = 300  # models.Notification.subject
_LINE_NUMBER = re.compile(r"^(\d+): ")  # the numbering this module adds to every line it sends
_COPIED_NUMBER = re.compile(r"^\s*\d+\s*[:|]\s?")  # the same, copied along into a quote


def check_target_id(repository_id: str, check_name: str) -> str:
    """The schedule target of one check on one repository (``schedules.target_id``, 40)."""
    digest = hashlib.sha256(f"{repository_id}|{check_name}".encode()).hexdigest()
    return f"chk_{digest[:32]}"


def find_check(settings: ProjectSettings, name: str) -> Check | None:
    return next((check for check in settings.checks if check.name == name), None)


def applies_to(check: Check, repository: Repository) -> bool:
    return not check.repositories or repository.name in check.repositories


@dataclass
class ScopedFile:
    ref: str
    path: str
    lines: list[str]

    def numbered(self) -> str:
        return "\n".join(f"{n}: {text}" for n, text in enumerate(self.lines, start=1))


@dataclass
class Scope:
    files: list[ScopedFile] = field(default_factory=list)
    over_limit: int = 0  # in scope, but past ``max_files`` or ``max_chars``
    unreadable: int = 0  # binary, over MAX_FILE_BYTES, or not a regular file in the checkout


def _read(path: Path, real_root: Path) -> str | None:
    """A file's text, if it is a regular file physically inside the checkout. A symlink, or a
    path that resolves elsewhere, is not read: a check sends what it reads to a model, and a
    repository must not be able to make it send a file of the worker's."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        real = path.resolve()
        real.relative_to(real_root)
        if real.stat().st_size > MAX_FILE_BYTES:
            return None
        data = real.read_bytes()
    except (OSError, ValueError):
        return None
    if b"\x00" in data[:4096]:
        return None  # binary
    return clean_text(data.decode("utf-8", errors="replace"))


def select_files(root: Path, check: Check, settings: ProjectSettings) -> Scope:
    """The files one run of ``check`` reads, in path order: those its ``paths`` cover, minus
    its ``exclude`` and the protected paths marked ``excluded``, up to its limits."""
    scope = Scope()
    real_root = root.resolve()
    excluded = [rule.pattern for rule in settings.protected_paths if rule.mode == "excluded"]
    chars = 0
    for rel in iter_repo_files(root):
        if not any(path_matches(rel, pattern) for pattern in check.paths):
            continue
        if any(path_matches(rel, pattern) for pattern in (*check.exclude, *excluded)):
            continue
        text = _read(root / rel, real_root)
        if text is None:
            scope.unreadable += 1
            continue
        lines = source_lines(text)
        size = sum(len(line) for line in lines) + 8 * len(lines)  # with the line numbers
        if len(scope.files) >= check.max_files or chars + size > check.max_chars:
            scope.over_limit += 1
            continue
        chars += size
        ref = f"f{len(scope.files) + 1}"
        scope.files.append(ScopedFile(ref=ref, path=printable(rel), lines=lines))
    return scope


def batches(files: list[ScopedFile]) -> list[list[ScopedFile]]:
    """Files grouped into model calls of about ``BATCH_CHARS`` each; a larger file goes alone."""
    out: list[list[ScopedFile]] = []
    current: list[ScopedFile] = []
    size = 0
    for scoped in files:
        n = len(scoped.numbered())
        if current and size + n > BATCH_CHARS:
            out.append(current)
            current, size = [], 0
        current.append(scoped)
        size += n
    if current:
        out.append(current)
    return out


def _normalize(line: str) -> str:
    return " ".join(line.split())


def seen_lines(numbered_and_redacted: str) -> list[tuple[int, str]]:
    """The file's non-blank lines as the model saw them: numbered, then redacted. A redaction
    that spans lines (a private key) leaves one token where several lines were; its text joins
    the line it started on."""
    out: list[tuple[int, str]] = []
    for raw in numbered_and_redacted.split("\n"):
        match = _LINE_NUMBER.match(raw)
        if match:
            out.append((int(match.group(1)), raw[match.end() :]))
        elif out:
            out[-1] = (out[-1][0], f"{out[-1][1]} {raw}")
    return [(n, _normalize(text)) for n, text in out if _normalize(text)]


@dataclass(frozen=True)
class Located:
    line: int  # where the quote starts in the file
    text: str  # the quoted lines as the file has them, whitespace collapsed: its identity


def locate(evidence: str, lines: list[tuple[int, str]], hint: int | None) -> Located | None:
    """Where ``evidence`` starts in the file, or None if it is not a quote of it.

    Lines are compared with their whitespace collapsed and blank lines left out. A quote that
    occurs more than once is placed at the occurrence nearest to ``hint``, the line the model
    gave. Line numbers copied into the quote are tolerated, but only when the quote does not
    match as written: code can itself start with ``12:``.
    """
    texts = [text for _, text in lines]
    for strip in (False, True):
        quote = [
            _normalize(_COPIED_NUMBER.sub("", line, count=1) if strip else line)
            for line in source_lines(evidence)
        ]
        quote = [line for line in quote if line]
        if not quote or len(quote) > MAX_EVIDENCE_LINES:
            return None
        if sum(len(line) for line in quote) < MIN_EVIDENCE_CHARS:
            return None
        starts = [
            i for i in range(len(texts) - len(quote) + 1) if texts[i : i + len(quote)] == quote
        ]
        if starts:
            hinted = hint  # narrowed for the key function
            first = min(starts, key=lambda i: abs(lines[i][0] - hinted)) if hinted else starts[0]
            return Located(lines[first][0], " ".join(quote))
    return None


@dataclass
class _Candidate:
    finding: CheckFinding
    path: str
    line: int
    evidence: str
    fingerprint: str


@dataclass
class _Tally:
    unknown_file: int = 0
    unverified_evidence: int = 0
    duplicate: int = 0
    over_limit: int = 0

    def as_dict(self) -> dict[str, int]:
        return {k: v for k, v in vars(self).items() if v}


class CheckRunner:
    def __init__(self, gateway: LLMGateway, scm: SCMRegistry, workdir: Path) -> None:
        self.gateway = gateway
        self.scm = scm
        self.workdir = workdir  # shared with audits: one checkout per repository

    def run(self, session: Session, repository_id: str, check_name: str) -> AuditRun | None:
        """Run one check on one repository. Returns None, with the reason in the audit log,
        when the check is no longer configured for it (the settings changed after the job
        was queued)."""
        repo = session.get(Repository, repository_id)
        if repo is None:
            raise queue.PermanentFailure(f"repository {repository_id} not found")
        project = session.get(Project, repo.project_id)
        assert project is not None
        settings = ProjectSettings.model_validate(project.settings)
        check = find_check(settings, check_name)
        if check is None or not applies_to(check, repo):
            audit_log.record(
                session,
                tenant_id=repo.tenant_id,
                entity_type="repository",
                entity_id=repo.id,
                action="check.skipped",
                details={
                    "check": check_name,
                    "reason": "no longer configured"
                    if check is None
                    else "repository not in the check's scope",
                },
            )
            return None
        started = utcnow()
        # The checkout and the reading happen before anything is written, as for audits.
        with checkout_lock(self.workdir, repo.id):
            path = self.scm.for_provider(repo.scm_provider).checkout(repo, self.workdir / repo.id)
            head = head_sha(path)
            scope = select_files(path, check, settings)
        run = AuditRun(
            tenant_id=repo.tenant_id,
            project_id=project.id,
            repository_id=repo.id,
            audit_types=[f"check:{check.name}"],
            mode="check",
            commit_sha=head,
            files_analyzed=len(scope.files),
            started_at=started,
        )
        session.add(run)
        session.flush()
        with METRICS.timer("opsmith_stage_seconds", stage="check"):
            return self._analyze(session, project, repo, run, check, scope, settings)

    def _analyze(
        self,
        session: Session,
        project: Project,
        repo: Repository,
        run: AuditRun,
        check: Check,
        scope: Scope,
        settings: ProjectSettings,
    ) -> AuditRun:
        redactor = self.gateway.redactor(settings)
        groups = batches(scope.files)
        tally = _Tally()
        candidates: dict[str, _Candidate] = {}
        notes: list[str] = []
        details: dict[str, Any] = {"check": check.name, "prompt": "check.run"}
        failure: LLMError | None = None
        not_analyzed = not groups
        if not groups:
            notes.append("No readable file in the repository matches the check's paths.")
        for number, group in enumerate(groups, start=1):
            variables = {
                "check": check.name,
                "repository": repo.name,
                "commit": run.commit_sha,
                "part": f"{number} of {len(groups)}",
                "files_not_included": scope.over_limit,
                "files": [{"ref": f.ref, "path": f.path, "content": f.numbered()} for f in group],
            }
            try:
                result = self.gateway.structured(
                    session,
                    project=project,
                    stage="check",
                    prompt_id="check.run",
                    variables=variables,
                    schema=CheckOutput,
                    run_id=run.id,
                    instruction=check.instructions,
                )
            except LLMError as exc:
                failure = exc
                details["failed_part"] = f"{number} of {len(groups)}"
                break
            run.input_tokens += result.input_tokens
            run.output_tokens += result.output_tokens
            run.cost_usd = round(run.cost_usd + result.cost_usd, 6)
            details.update(
                prompt=f"{result.prompt_id}@{result.prompt_version}",
                provider=result.provider,
                model=result.model,
            )
            notes += [note[:MAX_NOTE_CHARS] for note in result.output.notes]
            if not result.output.analyzed:
                not_analyzed = True
                break
            self._collect(repo, check, group, result.output, redactor, candidates, tally)

        ranked = sorted(
            candidates.values(),
            key=lambda c: (SEVERITY_RANK[c.finding.severity], c.finding.confidence),
            reverse=True,
        )
        kept = ranked[: check.max_findings]
        tally.over_limit += len(ranked) - len(kept)
        for candidate in kept:
            session.add(self._finding(repo, run, check, candidate, details))

        details.update(
            parts=len(groups),
            files_over_limit=scope.over_limit,
            files_unreadable=scope.unreadable,
            dropped=tally.as_dict(),
            notes=notes[:MAX_NOTES],
        )
        run.tool_versions = details
        run.findings_count = len(kept)
        run.finished_at = utcnow()
        if failure is not None:
            over_budget = isinstance(failure, BudgetExceeded)
            run.status = "budget_exceeded" if over_budget else "failed"
            run.error = f"part {details['failed_part']}: {failure}"[:2000]
        elif not_analyzed:
            run.status = "skipped"
            run.error = (" ".join(notes) or "the model did not carry out the check")[:2000]
        else:
            run.status = "succeeded"
        self._record(session, project, repo, run, check, failure)
        if kept:
            queue.enqueue(
                session,
                tenant_id=repo.tenant_id,
                kind="aggregate",
                payload={"project_id": project.id},
                idempotency_key=f"aggregate:check:{run.id}",
            )
        return run

    @staticmethod
    def _collect(
        repo: Repository,
        check: Check,
        group: list[ScopedFile],
        output: CheckOutput,
        redactor: Redactor,
        candidates: dict[str, _Candidate],
        tally: _Tally,
    ) -> None:
        """Keep the findings that quote the file they name. The quote is matched against the
        file as the model saw it (numbered, then redacted), and stored in that form."""
        by_ref = {scoped.ref: scoped for scoped in group}
        seen: dict[str, list[tuple[int, str]]] = {}
        for found in output.findings:
            scoped = by_ref.get(found.file_ref)
            if scoped is None:
                tally.unknown_file += 1
                continue
            if scoped.ref not in seen:
                seen[scoped.ref] = seen_lines(str(redactor.redact(scoped.numbered())))
            located = locate(found.evidence, seen[scoped.ref], found.line)
            if located is None:
                tally.unverified_evidence += 1
                continue
            # The identity is the code that was quoted, as the file has it: the same problem
            # quoted twice, or with line numbers copied along, is one finding.
            digest = hashlib.sha256(
                f"{repo.id}|check.{check.name}|{scoped.path}|{located.text}".encode()
            ).hexdigest()
            fingerprint = digest[:32]
            if fingerprint in candidates:
                tally.duplicate += 1
                continue
            candidates[fingerprint] = _Candidate(
                found, scoped.path, located.line, found.evidence.strip(), fingerprint
            )

    @staticmethod
    def _finding(
        repo: Repository,
        run: AuditRun,
        check: Check,
        candidate: _Candidate,
        details: dict[str, Any],
    ) -> Finding:
        found = candidate.finding
        description = found.description.strip()
        if found.suggested_fix.strip():
            description += f"\n\nSuggested fix: {found.suggested_fix.strip()}"
        return Finding(
            tenant_id=repo.tenant_id,
            project_id=repo.project_id,
            source=f"check:{check.name}",
            source_run_id=run.id,
            category=check.category or found.category,
            rule_id=f"check.{check.name}",
            title=found.title,
            description=description,
            severity=found.severity,
            confidence=round(found.confidence, 3),
            evidence_refs=[f"audit:{run.id}#{candidate.fingerprint[:12]}"],
            repository_id=repo.id,
            path=candidate.path[:500],
            line=candidate.line,
            fingerprint=candidate.fingerprint,
            evidence_strength=STRENGTH,
            status="new",
            extra={
                "snippet": candidate.evidence[:MAX_SNIPPET_CHARS],
                "check": check.name,
                "commit": run.commit_sha,
                "suggested_fix": found.suggested_fix.strip(),
                "model": details.get("model"),
            },
        )

    @staticmethod
    def _record(
        session: Session,
        project: Project,
        repo: Repository,
        run: AuditRun,
        check: Check,
        failure: LLMError | None,
    ) -> None:
        summary: dict[str, Any] = {
            "check": check.name,
            "status": run.status,
            "files": run.files_analyzed,
            "findings": run.findings_count,
            "dropped": run.tool_versions.get("dropped", {}),
            "files_over_limit": run.tool_versions.get("files_over_limit", 0),
        }
        if run.status == "succeeded":
            action = "check.completed"
        elif run.status == "skipped":
            action = "check.skipped"
        else:
            action = "check.failed"
        audit_log.record(
            session,
            tenant_id=repo.tenant_id,
            entity_type="audit_run",
            entity_id=run.id,
            action=action,
            details=summary if run.error is None else {**summary, "error": run.error[:500]},
        )
        if run.status == "succeeded":
            return
        over_budget = isinstance(failure, BudgetExceeded)
        what = {"skipped": "did nothing", "budget_exceeded": "stopped: budget exceeded"}.get(
            run.status, "failed"
        )
        notifications.notify(
            session,
            tenant_id=repo.tenant_id,
            project_id=project.id,
            event="budget.exceeded" if over_budget else "check.failed",
            subject=f"Check {check.name} {what} on {repo.name}"[:SUBJECT_CHARS],
            body=run.error or "",
        )
