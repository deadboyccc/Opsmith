"""Aggregation, correlation, deduplication, ranking and task generation (spec 5.4, 5.5).

* Findings sharing a fingerprint, or a runtime finding whose stack trace points at a file an
  audit flagged, are merged into one group (confidence combines via noisy-OR).
* Groups matching an open task enrich it; matching a recently closed task are suppressed;
  matching a rejected task are suppressed unless the evidence *materially* changed.
* A task a human has already approved is never changed underneath them: evidence for a task
  that is ``approved`` or ``executing`` waits until the execution has finished.
* Everything else becomes a ranked task and is submitted to the human review queue.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsmith import audit_log, notifications
from opsmith.aggregation.scoring import noisy_or, priority_for, score
from opsmith.audit_log import DONE_TASK_STATES, OPEN_TASK_STATES, transition_task
from opsmith.db import SERIALIZE, new_id, utcnow
from opsmith.llm import HeuristicProvider, LLMError, LLMGateway
from opsmith.llm.base import ProviderRequest
from opsmith.models import (
    Finding,
    Project,
    Repository,
    SuppressionRule,
    Task,
    TaskFingerprint,
)
from opsmith.protection import classify
from opsmith.schemas import SEVERITY_RANK, ProjectSettings, TaskDraft

log = logging.getLogger(__name__)
LINE_PROXIMITY = 25
EXACT_LINE = 2
_STRENGTH_MULTIPLIER = {"false_positive": 3.0, "insufficient_evidence": 2.0}
_SEVERITY_ONLY = {"wont_fix", "duplicate", "out_of_scope"}
# Between a human approval and the end of the execution the task is frozen: it is exactly
# what the reviewer approved, and what the coding agent is working from.
FROZEN_TASK_STATES = frozenset({"approved", "executing"})


_PROTECTION_RANK = {"none": 0, "elevated": 1, "excluded": 2}


def _norm(path: str | None) -> str | None:
    if not path:
        return None
    return path.strip().removeprefix("./").lstrip("/")


def _same_file(a: str | None, b: str | None) -> bool:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def source_kind(finding: Finding) -> str:
    return "runtime" if finding.source == "inference" else "static"


def correlation_distance(
    a_path: str | None,
    a_line: int | None,
    a_cat: str,
    b_path: str | None,
    b_line: int | None,
    b_cat: str,
) -> int | None:
    """How strongly a runtime and a static finding point at the same defect (lower is
    closer), or ``None`` when they should not be merged. Same file is required; then either
    the same category near the same line, or (almost) the exact same line."""
    if not _same_file(a_path, b_path):
        return None
    if a_line is not None and b_line is not None:
        dist = abs(a_line - b_line)
        if dist <= EXACT_LINE or (a_cat == b_cat and dist <= LINE_PROXIMITY):
            return dist
        return None
    return LINE_PROXIMITY if a_cat == b_cat else None


@dataclass
class Group:
    findings: list[Finding] = field(default_factory=list)

    @property
    def fingerprints(self) -> set[str]:
        return {f.fingerprint for f in self.findings}

    @property
    def primary(self) -> Finding:
        # Static findings carry the most stable identity and precise location.
        return max(
            self.findings,
            key=lambda f: (
                SEVERITY_RANK.get(f.severity, 2),
                source_kind(f) == "static",
                f.confidence,
            ),
        )

    @property
    def severity(self) -> str:
        return max((f.severity for f in self.findings), key=lambda s: SEVERITY_RANK.get(s, 2))

    @property
    def category(self) -> str:
        cats = [f.category for f in self.findings]
        return "security" if "security" in cats else self.primary.category

    @property
    def confidence(self) -> float:
        by_kind: dict[str, float] = {}
        for f in self.findings:
            kind = source_kind(f)
            by_kind[kind] = max(by_kind.get(kind, 0.0), f.confidence)
        return noisy_or(list(by_kind.values()))

    @property
    def distinct(self) -> list[Finding]:
        """Findings with repeated static detections collapsed. An audit re-reporting the same
        defect (same fingerprint) is the same evidence again, not more evidence; every
        runtime finding is a separate occurrence."""
        out: list[Finding] = []
        seen: set[str] = set()
        ranked = sorted(
            self.findings,
            key=lambda f: (SEVERITY_RANK.get(f.severity, 2), f.confidence),
            reverse=True,
        )
        for f in ranked:
            if source_kind(f) == "static":
                if f.fingerprint in seen:
                    continue
                seen.add(f.fingerprint)
            out.append(f)
        return out

    @property
    def evidence_strength(self) -> float:
        return round(sum(f.evidence_strength for f in self.distinct), 3)

    @property
    def source_kinds(self) -> set[str]:
        return {source_kind(f) for f in self.findings}

    @property
    def paths(self) -> list[str]:
        out: list[str] = []
        for f in sorted(self.findings, key=lambda f: source_kind(f) != "static"):
            p = _norm(f.path)
            if p and not any(_same_file(p, q) for q in out):
                out.append(p)
        return out

    @property
    def services(self) -> set[str]:
        return {f.service_id for f in self.findings if f.service_id}

    @property
    def occurrences(self) -> int:
        return len(self.distinct)


class Aggregator:
    def __init__(self, gateway: LLMGateway) -> None:
        self.gateway = gateway
        self._fallback = HeuristicProvider()

    def run(self, session: Session, project_id: str) -> dict[str, int]:
        # One aggregation per project at a time: two workers reading the same `new` findings
        # would both find no task for them and draft two. The second waits here, then finds
        # them taken. (SQLite serializes writers anyway.)
        project = session.get(Project, project_id, with_for_update=SERIALIZE)
        assert project is not None
        settings = ProjectSettings.model_validate(project.settings)
        new = session.scalars(
            select(Finding)
            .where(Finding.project_id == project_id, Finding.status == "new")
            .order_by(Finding.created_at)
        ).all()
        stats = {
            "findings": len(new),
            "groups": 0,
            "created": 0,
            "merged": 0,
            "reopened": 0,
            "suppressed": 0,
            "deferred": 0,
        }
        if not new:
            return stats
        groups = self.group(list(new))
        stats["groups"] = len(groups)
        groups.sort(key=lambda g: (SEVERITY_RANK.get(g.severity, 2), g.confidence), reverse=True)
        created = 0
        for group in groups:
            outcome = self._dedup(session, project, settings, group)
            if outcome == "new":
                if group.confidence < settings.min_task_confidence:
                    self._suppress(group, "below_confidence_threshold")
                    stats["suppressed"] += 1
                    continue
                if created >= settings.max_tasks_per_aggregation:
                    stats["deferred"] += 1  # stays `new`; picked up by the next aggregation
                    continue
                self._create_task(session, project, settings, group)
                created += 1
                stats["created"] += 1
            else:
                stats[outcome] += 1
        audit_log.record(
            session,
            tenant_id=project.tenant_id,
            entity_type="project",
            entity_id=project.id,
            action="aggregation.completed",
            details=stats,
        )
        return stats

    # ------------------------------------------------------------------ grouping
    def group(self, findings: list[Finding]) -> list[Group]:
        parent = list(range(len(findings)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        by_fp: dict[str, int] = {}
        for i, f in enumerate(findings):
            if f.fingerprint in by_fp:
                union(by_fp[f.fingerprint], i)
            else:
                by_fp[f.fingerprint] = i
        statics = [(i, f) for i, f in enumerate(findings) if source_kind(f) == "static" and f.path]
        for i, f in enumerate(findings):
            if source_kind(f) != "runtime" or not f.path:
                continue
            j = self._best_static_match(f, statics)
            if j is not None:
                union(j, i)
        buckets: dict[int, Group] = {}
        for i, f in enumerate(findings):
            buckets.setdefault(find(i), Group()).findings.append(f)
        return list(buckets.values())

    @staticmethod
    def _best_static_match(runtime: Finding, statics: list[tuple[int, Finding]]) -> int | None:
        best: tuple[int, int] | None = None
        for i, s in statics:
            if (
                runtime.repository_id
                and s.repository_id
                and s.repository_id != runtime.repository_id
            ):
                continue
            dist = correlation_distance(
                runtime.path, runtime.line, runtime.category, s.path, s.line, s.category
            )
            if dist is not None and (best is None or dist < best[0]):
                best = (dist, i)
        return best[1] if best else None

    # ------------------------------------------------------------------ dedup
    def _dedup(
        self, session: Session, project: Project, settings: ProjectSettings, group: Group
    ) -> str:
        task = self._existing_task(session, project.id, group)
        if task is None:
            return "new"
        # Locked and re-read, like a review does: an approval of this task that is in flight
        # either finishes first (the task is then frozen, below) or waits for this change.
        session.refresh(task, with_for_update=SERIALIZE)
        if task.status in FROZEN_TASK_STATES:
            return "deferred"  # findings stay `new`; attached once the execution is over
        if task.status in OPEN_TASK_STATES or task.status == "regressed":
            self._attach(session, task, settings, group)
            if task.status == "regressed":
                transition_task(
                    session, task, "in_review", details={"reason": "regression evidence attached"}
                )
            return "merged"
        if task.status in DONE_TASK_STATES:
            if utcnow() - task.updated_at <= timedelta(days=settings.recently_closed_days):
                self._suppress(group, f"recently_{task.status}:{task.id}")
                return "suppressed"
            return "new"  # old fix regressed long after - treat as a new problem
        if task.status == "rejected":
            rule = session.scalar(
                select(SuppressionRule).where(
                    SuppressionRule.task_id == task.id, SuppressionRule.active.is_(True)
                )
            )
            if rule is None or self.materially_changed(rule, group):
                if rule is not None:
                    rule.active = False
                self._attach(session, task, settings, group)
                task.rejection_reason = None
                transition_task(
                    session,
                    task,
                    "in_review",
                    details={"reason": "re-proposed: evidence materially changed"},
                )
                notifications.notify(
                    session,
                    tenant_id=task.tenant_id,
                    project_id=task.project_id,
                    event="task.reproposed",
                    subject=f"Re-proposed with new evidence: {task.title}",
                )
                return "reopened"
            self._suppress(group, f"previously_rejected:{rule.reason_code}")
            return "suppressed"
        return "new"

    @staticmethod
    def materially_changed(rule: SuppressionRule, group: Group) -> bool:
        if SEVERITY_RANK.get(group.severity, 2) > SEVERITY_RANK.get(rule.severity_at_rejection, 2):
            return True
        if rule.reason_code in _SEVERITY_ONLY:
            return False
        if group.source_kinds - set(rule.sources_at_rejection):
            return True
        multiplier = _STRENGTH_MULTIPLIER.get(rule.reason_code, 2.0)
        return group.evidence_strength >= multiplier * max(rule.evidence_strength_at_rejection, 0.1)

    def _existing_task(self, session: Session, project_id: str, group: Group) -> Task | None:
        task_ids = session.scalars(
            select(TaskFingerprint.task_id).where(
                TaskFingerprint.project_id == project_id,
                TaskFingerprint.fingerprint.in_(group.fingerprints),
            )
        ).all()
        if task_ids:
            tasks = session.scalars(select(Task).where(Task.id.in_(set(task_ids)))).all()
            # A task that is still open wins over a newer closed one, so evidence is not
            # written off as "recently closed" while someone is still working on the problem.
            return max(
                tasks,
                key=lambda t: (
                    t.status in OPEN_TASK_STATES or t.status == "regressed",
                    t.created_at,
                ),
            )
        # Cross-run correlation: runtime evidence arriving after a static finding (or the
        # reverse) attaches to the open task that already holds the other kind of evidence.
        located = [f for f in group.findings if f.path]
        if not located:
            return None
        candidates = session.scalars(
            select(Task).where(Task.project_id == project_id, Task.status.in_(OPEN_TASK_STATES))
        ).all()
        best: tuple[int, Task] | None = None
        for task in candidates:
            for ev in task.evidence:
                if not ev.get("path") or ev.get("kind") not in {"runtime", "static"}:
                    continue
                for f in located:
                    if ev["kind"] == source_kind(f):
                        continue
                    if (
                        task.repository_id
                        and f.repository_id
                        and f.repository_id != task.repository_id
                    ):
                        continue  # the same relative path in another repository
                    dist = correlation_distance(
                        f.path,
                        f.line,
                        f.category,
                        ev["path"],
                        ev.get("line"),
                        ev.get("category", ""),
                    )
                    if dist is not None and (best is None or dist < best[0]):
                        best = (dist, task)
        return best[1] if best else None

    def _attach(
        self, session: Session, task: Task, settings: ProjectSettings, group: Group
    ) -> None:
        for f in group.findings:
            f.task_id = task.id
            f.status = "grouped"
        existing_fps = set(
            session.scalars(
                select(TaskFingerprint.fingerprint).where(TaskFingerprint.task_id == task.id)
            ).all()
        )
        for fp in group.fingerprints - existing_fps:
            session.add(
                TaskFingerprint(
                    tenant_id=task.tenant_id,
                    project_id=task.project_id,
                    task_id=task.id,
                    fingerprint=fp,
                )
            )
        # A static finding re-detected by a later audit is the same evidence, not new evidence;
        # runtime findings are new occurrences of the problem.
        fresh = [
            f
            for f in group.distinct
            if source_kind(f) == "runtime" or f.fingerprint not in existing_fps
        ]
        task.last_seen_at = utcnow()
        # A re-detection can still raise the severity (triage re-rated it). Record that even
        # when nothing else is new, so a task re-proposed for that reason really differs.
        raised = SEVERITY_RANK.get(group.severity, 2) > SEVERITY_RANK.get(task.severity, 2)
        if raised:
            task.severity = group.severity
        if not fresh and not raised:
            return
        if fresh:
            task.evidence = (task.evidence + [self._evidence(f) for f in fresh])[-50:]
            new_paths = [
                path
                for path in group.paths
                if not any(_same_file(path, existing) for existing in task.paths)
            ]
            if new_paths:
                task.paths = [*task.paths, *new_paths]
                # The scope grew, so the protection level is re-evaluated (never lowered here).
                level = classify(task.paths, settings.protected_paths)
                if _PROTECTION_RANK[level] > _PROTECTION_RANK.get(task.protection, 0):
                    task.protection = level
            task.occurrences += len(fresh)
            task.evidence_strength = round(
                task.evidence_strength + sum(f.evidence_strength for f in fresh), 3
            )
            # Same rule as at creation: the best confidence per kind of evidence, combined
            # across kinds. Repeated evidence of one kind does not compound.
            by_kind: dict[str, float] = {}
            for ev in task.evidence:
                kind = ev.get("kind")
                if kind:
                    by_kind[kind] = max(by_kind.get(kind, 0.0), float(ev.get("confidence") or 0))
            task.confidence = max(task.confidence, noisy_or(list(by_kind.values())))
            task.provenance = self._merge_provenance(task.provenance, group)
        kinds = {e.get("kind") for e in task.evidence}
        services = set(
            session.scalars(
                select(Finding.service_id).where(
                    Finding.task_id == task.id, Finding.service_id.is_not(None)
                )
            ).all()
        )
        task.score = score(
            severity=task.severity,
            confidence=task.confidence,
            evidence_strength=task.evidence_strength,
            occurrences=task.occurrences,
            services_affected=max(len(services | group.services), 1),
            source_kinds=len(kinds - {None}),
            effort=task.effort_estimate,
            category=task.category,
            last_seen=task.last_seen_at,
        )
        task.priority = priority_for(task.score)
        # Anyone who loaded the task before this change reviews a stale copy; the version
        # bump makes `expected_version` catch that.
        task.version += 1
        audit_log.record(
            session,
            tenant_id=task.tenant_id,
            entity_type="task",
            entity_id=task.id,
            action="task.evidence_added",
            details={"findings": [f.id for f in group.findings], "severity_raised": raised},
        )

    @staticmethod
    def _suppress(group: Group, reason: str) -> None:
        for f in group.findings:
            f.status = "suppressed"
            f.status_reason = reason[:100]

    # ------------------------------------------------------------------ task generation
    @staticmethod
    def _evidence(f: Finding) -> dict[str, Any]:
        return {
            "type": "finding",
            "finding_id": f.id,
            "source": f.source,
            "kind": source_kind(f),
            "category": f.category,
            "title": f.title,
            "severity": f.severity,
            "confidence": f.confidence,
            "path": f.path,
            "line": f.line,
            "refs": f.evidence_refs[:5],
            "rule_id": f.rule_id,
            "signal": f.signal,
        }

    @staticmethod
    def _merge_provenance(prov: dict[str, Any], group: Group) -> dict[str, Any]:
        out = {k: list(v) for k, v in prov.items()}

        def add(key: str, value: str | None) -> None:
            if value and value not in out.setdefault(key, []):
                out[key].append(value)

        for f in group.findings:
            add("finding_ids", f.id)
            add("lead_ids", f.lead_id)
            add(
                "inference_run_ids" if f.source == "inference" else "audit_run_ids", f.source_run_id
            )
        return out

    def _repository_for(self, session: Session, project: Project, group: Group) -> str | None:
        for f in sorted(group.findings, key=lambda f: source_kind(f) != "static"):
            if f.repository_id:
                return f.repository_id
        repos = session.scalars(
            select(Repository.id).where(Repository.project_id == project.id)
        ).all()
        return repos[0] if len(repos) == 1 else None

    def _draft(self, session: Session, project: Project, group: Group) -> TaskDraft:
        variables = {
            "category": group.category,
            "paths": group.paths,
            "signals": sorted({f.signal for f in group.findings if f.signal}),
            "findings": [
                {
                    "source": f.source,
                    "title": f.title,
                    "description": f.description[:1500],
                    "category": f.category,
                    "severity": f.severity,
                    "confidence": f.confidence,
                    "path": f.path,
                    "line": f.line,
                    "rule_id": f.rule_id,
                    "evidence_refs": f.evidence_refs[:5],
                }
                for f in sorted(group.findings, key=lambda f: source_kind(f) != "static")[:10]
            ],
        }
        try:
            return self.gateway.structured(
                session,
                project=project,
                stage="task",
                prompt_id="task.draft",
                variables=variables,
                schema=TaskDraft,
            ).output
        except LLMError as exc:
            log.warning("task drafting fell back to heuristic", extra={"error": str(exc)})
            audit_log.record(
                session,
                tenant_id=project.tenant_id,
                entity_type="project",
                entity_id=project.id,
                action="task.draft_fallback",
                details={"error": str(exc)[:300]},
            )
            data = self._fallback.complete(
                ProviderRequest(
                    stage="task",
                    prompt_id="task.draft",
                    model="heuristic-v1",
                    system="",
                    user="",
                    schema=TaskDraft,
                    variables=variables,
                )
            ).data
            return TaskDraft.model_validate(data)

    def _create_task(
        self, session: Session, project: Project, settings: ProjectSettings, group: Group
    ) -> Task:
        draft = self._draft(session, project, group)
        task_id = new_id("task")
        slug = re.sub(r"[^a-z0-9]+", "-", draft.title.lower()).strip("-")[:40] or "task"
        paths = group.paths
        task = Task(
            id=task_id,
            tenant_id=project.tenant_id,
            project_id=project.id,
            title=draft.title,
            category=group.category,
            severity=group.severity,
            confidence=group.confidence,
            problem_statement=draft.problem_statement,
            evidence=[self._evidence(f) for f in group.distinct][:50],
            proposed_approach=draft.proposed_approach,
            acceptance_criteria=draft.acceptance_criteria,
            repository_id=self._repository_for(session, project, group),
            paths=paths,
            branch_name=f"opsmith/{group.category}/{slug}-{task_id[-6:]}",
            risk_assessment=draft.risk_assessment,
            effort_estimate=draft.effort_estimate,
            verification={
                "tests_to_add": draft.tests_to_add,
                "metrics_to_watch": draft.metrics_to_watch,
                "signals": sorted({f.signal for f in group.findings if f.signal}),
            },
            provenance=self._merge_provenance({}, group),
            evidence_strength=group.evidence_strength,
            occurrences=group.occurrences,
            protection=classify(paths, settings.protected_paths),
            last_seen_at=utcnow(),
        )
        task.score = score(
            severity=task.severity,
            confidence=task.confidence,
            evidence_strength=task.evidence_strength,
            occurrences=task.occurrences,
            services_affected=max(len(group.services), 1),
            source_kinds=len(group.source_kinds),
            effort=task.effort_estimate,
            category=task.category,
            last_seen=task.last_seen_at,
        )
        task.priority = priority_for(task.score)
        session.add(task)
        session.flush()
        for fp in group.fingerprints:
            session.add(
                TaskFingerprint(
                    tenant_id=task.tenant_id, project_id=project.id, task_id=task.id, fingerprint=fp
                )
            )
        for f in group.findings:
            f.task_id = task.id
            f.status = "grouped"
        audit_log.record(
            session,
            tenant_id=task.tenant_id,
            entity_type="task",
            entity_id=task.id,
            action="task.proposed",
            to_state="proposed",
            details={"findings": [f.id for f in group.findings], "score": task.score},
        )
        if settings.auto_submit_for_review:
            transition_task(session, task, "in_review", details={"auto_submitted": True})
            notifications.notify(
                session,
                tenant_id=task.tenant_id,
                project_id=project.id,
                event="task.in_review",
                subject=f"[{task.priority}] Review needed: {task.title}",
                body=task.problem_statement[:500],
                link=f"/ui/tasks/{task.id}",
            )
        return task
