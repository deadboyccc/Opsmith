"""Chained batch inference with lead carry-over (spec 5.2).

Each run consumes the service's unconsumed bundles plus a *summarized* snapshot of its
active leads (bounded chain depth: never the full history of earlier runs). Model output is
validated, then applied defensively:

* lead updates for unknown lead IDs are ignored (and audited) - injection/hallucination guard;
* evidence refs are kept only when they exist in this run's input;
* leads the model did not mention are carried (never silently dropped);
* leads carried more than ``max_carry`` times expire and are escalated to humans.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsmith import audit_log, notifications, queue
from opsmith.audit_log import ACTIVE_LEAD_STATES, transition_lead
from opsmith.db import new_id, utcnow
from opsmith.llm import BudgetExceeded, LLMError, LLMGateway
from opsmith.models import AnalysisBundle, Finding, InferenceRun, Lead, Project, Service, Task
from opsmith.observability import METRICS
from opsmith.redaction import Redactor
from opsmith.schemas import MAX_SIGNAL_CHARS, InferenceFinding, InferenceOutput, ProjectSettings

log = logging.getLogger(__name__)
LEAD_CONTEXT_CHARS = 600
MAX_LEAD_REFS = 5
SUBJECT_CHARS = 300  # models.Notification.subject


def finding_fingerprint(*parts: str | None) -> str:
    raw = "|".join(p or "" for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def summarize_lead(lead: Lead) -> dict[str, Any]:
    return {
        "id": lead.id,
        "title": lead.title,
        "hypothesis": lead.hypothesis,
        "state": lead.state,
        "age": lead.age,
        "max_carry": lead.max_carry,
        "signal": lead.signal,
        "suspected_path": lead.suspected_path,
        "source": lead.source,
        "context": lead.context[-LEAD_CONTEXT_CHARS:],
        "evidence_refs": lead.evidence_refs[-MAX_LEAD_REFS:],
    }


class InferenceOrchestrator:
    def __init__(self, gateway: LLMGateway, max_input_tokens: int = 60_000) -> None:
        self.gateway = gateway
        self.max_input_tokens = max_input_tokens

    def run(self, session: Session, service_id: str, slot: str) -> InferenceRun | None:
        service = session.get(Service, service_id)
        if service is None:
            raise queue.PermanentFailure(f"service {service_id} not found")
        existing = session.scalar(
            select(InferenceRun).where(
                InferenceRun.service_id == service_id, InferenceRun.slot == slot
            )
        )
        if existing is not None and existing.status != "running":
            return existing  # idempotent re-delivery
        project = session.get(Project, service.project_id)
        assert project is not None
        settings = ProjectSettings.model_validate(project.settings)

        bundles = self._select_bundles(session, service_id)
        leads = list(
            session.scalars(
                select(Lead)
                .where(Lead.service_id == service_id, Lead.state.in_(ACTIVE_LEAD_STATES))
                .order_by(Lead.created_at)
            ).all()
        )
        if not bundles and not leads:
            return None

        previous = session.scalar(
            select(InferenceRun)
            .where(InferenceRun.service_id == service_id, InferenceRun.status == "succeeded")
            .order_by(InferenceRun.started_at.desc())
            .limit(1)
        )
        lead_snapshot = [summarize_lead(lead) for lead in leads]
        run = existing or InferenceRun(
            id=new_id("inf"),
            tenant_id=service.tenant_id,
            project_id=project.id,
            service_id=service_id,
            slot=slot,
        )
        run.previous_run_id = previous.id if previous else None
        run.input_bundle_ids = [b.id for b in bundles]
        run.input_leads = lead_snapshot
        run.status = "running"
        session.add(run)
        session.flush()

        variables = {
            "service": {
                "name": service.name,
                "environment": service.environment,
                "path_prefixes": service.path_prefixes,
            },
            "bundles": [self._bundle_view(b) for b in bundles],
            "leads": lead_snapshot,
            "rejections": self._rejections(session, project.id),
        }
        stage_cfg = settings.llm.for_stage("inference")
        try:
            with METRICS.timer("opsmith_stage_seconds", stage="inference"):
                result = self.gateway.structured(
                    session,
                    project=project,
                    stage="inference",
                    prompt_id="inference.analyze",
                    variables=variables,
                    schema=InferenceOutput,
                    run_id=run.id,
                )
        except LLMError as exc:
            # Finish the run instead of raising: the UsageRecords of calls the provider billed
            # (invalid output, refusal, truncation) live in this transaction and must commit, or
            # the cost caps never see them. Nothing was analysed, so the bundles stay unconsumed
            # and the leads untouched; the next slot picks both up again.
            over_budget = isinstance(exc, BudgetExceeded)
            self._finish(
                run,
                "budget_exceeded" if over_budget else "failed",
                provider=stage_cfg.provider,
                model=stage_cfg.model,
                error=str(exc),
            )
            if not over_budget:
                audit_log.record(
                    session,
                    tenant_id=run.tenant_id,
                    entity_type="inference_run",
                    entity_id=run.id,
                    action="inference.failed",
                    details={"error": str(exc)[:500], "bundles": len(bundles)},
                )
            what = "budget exceeded" if over_budget else "failed"
            notifications.notify(
                session,
                tenant_id=run.tenant_id,
                project_id=project.id,
                event="budget.exceeded" if over_budget else "inference.failed",
                subject=f"Inference {what} for {service.name}"[:SUBJECT_CHARS],
                body=str(exc)[:2000],
            )
            return run

        # The model only ever saw identifiers after the gateway's redaction pass, so its
        # citations are matched in that form and stored in their real one.
        redactor = self.gateway.redactor(settings)
        refs = self._as_sent(self._valid_refs(bundles, leads), redactor)
        signals = self._as_sent(self._input_signals(bundles, leads), redactor, MAX_SIGNAL_CHARS)
        output = result.output
        created = self._apply(session, run, service, settings, output, leads, refs, signals)
        for bundle in bundles:
            bundle.consumed_by_run_id = run.id
        run.output = output.model_dump(mode="json")
        run.provider, run.model = result.provider, result.model
        run.prompt_id, run.prompt_version = result.prompt_id, result.prompt_version
        run.input_tokens, run.output_tokens = result.input_tokens, result.output_tokens
        run.cost_usd = result.cost_usd
        self._finish(run, "succeeded")
        audit_log.record(
            session,
            tenant_id=run.tenant_id,
            entity_type="inference_run",
            entity_id=run.id,
            action="inference.completed",
            actor=audit_log.Actor("agent", f"llm:{result.model}"),
            details={
                "findings": created,
                "bundles": len(bundles),
                "leads_in": len(leads),
                "cost_usd": result.cost_usd,
            },
        )
        queue.enqueue(
            session,
            tenant_id=run.tenant_id,
            kind="aggregate",
            payload={"project_id": project.id},
            idempotency_key=f"aggregate:inference:{run.id}",
        )
        return run

    # ------------------------------------------------------------------ helpers
    def _select_bundles(self, session: Session, service_id: str) -> list[AnalysisBundle]:
        candidates = session.scalars(
            select(AnalysisBundle)
            .where(
                AnalysisBundle.service_id == service_id, AnalysisBundle.consumed_by_run_id.is_(None)
            )
            .order_by(AnalysisBundle.window_start)
        ).all()
        chosen: list[AnalysisBundle] = []
        budget = self.max_input_tokens
        for bundle in candidates:  # oldest first; the rest wait for the next run
            if chosen and bundle.token_estimate > budget:
                break
            chosen.append(bundle)
            budget -= bundle.token_estimate
        return chosen

    @staticmethod
    def _bundle_view(bundle: AnalysisBundle) -> dict[str, Any]:
        def bounded(rows: list[Any]) -> list[Any]:
            # Signals are shown at the length they can be stored at (findings and leads keep
            # them in a bounded column), so the next run still recognises a lead's signal.
            return [{**row, "signal": row["signal"][:MAX_SIGNAL_CHARS]} for row in rows]

        return {
            "id": bundle.id,
            "window": [bundle.window_start.isoformat(), bundle.window_end.isoformat()],
            "stats": bundle.stats,
            "log_templates": bounded(bundle.log_templates),
            "metric_features": bounded(bundle.metric_features),
        }

    @staticmethod
    def _valid_refs(bundles: list[AnalysisBundle], leads: list[Lead]) -> set[str]:
        refs: set[str] = set()
        for b in bundles:
            for t in b.log_templates:
                refs.add(t["ref"])
                refs.update(t.get("event_refs", []))
            for f in b.metric_features:
                refs.add(f["ref"])
        for lead in leads:
            refs.update(lead.evidence_refs)
        return refs

    @staticmethod
    def _input_signals(bundles: list[AnalysisBundle], leads: list[Lead]) -> set[str]:
        signals: set[str] = set()
        for b in bundles:
            signals.update(row["signal"] for row in (*b.log_templates, *b.metric_features))
        signals.update(lead.signal for lead in leads if lead.signal)
        return signals

    @staticmethod
    def _as_sent(
        values: Iterable[str], redactor: Redactor, limit: int | None = None
    ) -> dict[str, str]:
        """Map each identifier as the model saw it (bounded to ``limit``, then redacted by the
        gateway) back to the identifier Opsmith stores. Sorted, so that when two identifiers
        redact to the same text the choice between them is at least deterministic."""
        sent: dict[str, str] = {}
        for value in sorted(values):
            real = value[:limit]
            sent.setdefault(str(redactor.redact(real))[:limit], real)
        return sent

    @staticmethod
    def _rejections(session: Session, project_id: str) -> list[dict[str, Any]]:
        rows = session.scalars(
            select(Task)
            .where(Task.project_id == project_id, Task.status == "rejected")
            .order_by(Task.updated_at.desc())
            .limit(10)
        ).all()
        out = []
        for task in rows:
            signals = task.verification.get("signals", [])
            out.append(
                {
                    "title": task.title,
                    "category": task.category,
                    "reason": task.rejection_reason,
                    "signal": signals[0] if signals else None,
                }
            )
        return out

    @staticmethod
    def _finish(
        run: InferenceRun,
        status: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        error: str | None = None,
    ) -> None:
        run.status = status
        run.finished_at = utcnow()
        if provider:
            run.provider = provider
        if model:
            run.model = model
        if error:
            run.error = error

    def _apply(
        self,
        session: Session,
        run: InferenceRun,
        service: Service,
        settings: ProjectSettings,
        output: InferenceOutput,
        leads: list[Lead],
        refs: dict[str, str],
        signals: dict[str, str],
    ) -> int:
        lead_by_id = {lead.id: lead for lead in leads}
        created: list[Finding] = []
        claimed: dict[str, Finding] = {}  # lead id -> the finding the model raised from it
        for f in output.findings:
            row = self._create_finding(session, run, service, f, refs, signals)
            created.append(row)
            if f.from_lead_id:
                claimed.setdefault(f.from_lead_id, row)
        # ``finding_index`` may only point at the model's own findings, never at one
        # synthesized below for another lead.
        own = len(created)

        touched: set[str] = set()
        for upd in output.lead_updates:
            lead = lead_by_id.get(upd.lead_id)
            if lead is None or upd.lead_id in touched:
                audit_log.record(
                    session,
                    tenant_id=run.tenant_id,
                    entity_type="inference_run",
                    entity_id=run.id,
                    action="inference.ignored_lead_update",
                    details={"lead_id": upd.lead_id[:80], "reason": "unknown or duplicate"},
                )
                continue
            touched.add(lead.id)
            cited = self._cited(upd.evidence_refs, refs)
            if cited:
                lead.evidence_refs = list(dict.fromkeys([*lead.evidence_refs, *cited]))[-20:]
            if upd.context_update:
                lead.context = (lead.context + f"\n[{run.slot}] {upd.context_update}").strip()
            if upd.action == "confirm":
                finding = None
                if upd.finding_index is not None and 0 <= upd.finding_index < own:
                    finding = created[upd.finding_index]
                if finding is None:
                    finding = claimed.get(lead.id)
                if finding is None:
                    finding = self._finding_from_lead(session, run, service, lead)
                    created.append(finding)
                finding.lead_id = lead.id
                lead.finding_id = finding.id
                transition_lead(session, lead, "confirmed", run_id=run.id)
                self._resolve_investigation(session, lead, "confirmed", finding)
            elif upd.action == "refute":
                transition_lead(session, lead, "refuted", run_id=run.id)
                self._resolve_investigation(session, lead, "refuted", None)
            else:
                self._carry(session, run, lead)
        for lead in leads:  # not mentioned -> carried, never silently dropped
            if lead.id not in touched:
                lead.context = (lead.context + f"\n[{run.slot}] not addressed by model").strip()
                self._carry(session, run, lead)

        for nl in output.new_leads:
            fp_signal = signals.get(nl.signal or "", nl.signal) or finding_fingerprint(
                service.id, nl.title
            )
            duplicate = session.scalar(
                select(Lead).where(
                    Lead.service_id == service.id,
                    Lead.signal == fp_signal,
                    Lead.state.in_(ACTIVE_LEAD_STATES),
                )
            )
            if duplicate is not None or any(f.signal == fp_signal for f in created):
                continue
            lead = Lead(
                tenant_id=run.tenant_id,
                project_id=run.project_id,
                service_id=service.id,
                title=nl.title,
                hypothesis=nl.hypothesis,
                signal=fp_signal,
                suspected_path=nl.suspected_path,
                evidence_refs=self._cited(nl.evidence_refs, refs),
                max_carry=settings.lead_max_carry,
                originating_run_id=run.id,
                last_run_id=run.id,
            )
            session.add(lead)
            session.flush()
            audit_log.record(
                session,
                tenant_id=run.tenant_id,
                entity_type="lead",
                entity_id=lead.id,
                action="lead.open",
                to_state="open",
                details={"run_id": run.id},
            )
        return len(created)

    def _carry(self, session: Session, run: InferenceRun, lead: Lead) -> None:
        lead.age += 1
        if lead.age > lead.max_carry:
            lead.escalated = True
            transition_lead(
                session,
                lead,
                "expired",
                run_id=run.id,
                details={"escalated": True, "age": lead.age},
            )
            notifications.notify(
                session,
                tenant_id=lead.tenant_id,
                project_id=lead.project_id,
                event="lead.escalated",
                subject=f"Lead needs human attention: {lead.title}"[:SUBJECT_CHARS],
                body=f"Carried {lead.age - 1} times without resolution.\n{lead.context[-500:]}",
            )
            self._resolve_investigation(session, lead, "expired", None)
        else:
            transition_lead(session, lead, "carried", run_id=run.id, details={"age": lead.age})

    def _create_finding(
        self,
        session: Session,
        run: InferenceRun,
        service: Service,
        f: InferenceFinding,
        refs: dict[str, str],
        signals: dict[str, str],
    ) -> Finding:
        cited = self._cited(f.evidence_refs, refs)
        dropped = sum(1 for r in f.evidence_refs if r not in refs)
        strength = 0.5 + 0.25 * min(len(cited), 6)
        signal = signals.get(f.signal or "", f.signal)
        finding = Finding(
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            source="inference",
            source_run_id=run.id,
            category=f.category,
            title=f.title,
            description=f.description,
            severity=f.severity,
            # unsupported claims lose confidence
            confidence=round(f.confidence * (1.0 if cited else 0.5), 3),
            evidence_refs=cited,
            repository_id=service.repository_id,
            service_id=service.id,
            path=f.suspected_path,
            line=f.suspected_line,
            signal=signal,
            fingerprint=finding_fingerprint(service.id, signal or f.title, f.category),
            evidence_strength=strength,
            extra={
                "suggested_next_investigation": f.suggested_next_investigation,
                "dropped_refs": dropped,
            },
        )
        session.add(finding)
        session.flush()
        return finding

    @staticmethod
    def _cited(cited: list[str], refs: dict[str, str]) -> list[str]:
        """The distinct input refs behind a model's citations. Refs that were not in the input
        are dropped, and repeating one ref does not make the evidence any stronger."""
        return list(dict.fromkeys(refs[r] for r in cited if r in refs))

    def _finding_from_lead(
        self, session: Session, run: InferenceRun, service: Service, lead: Lead
    ) -> Finding:
        lead_refs = list(dict.fromkeys(lead.evidence_refs))
        finding = Finding(
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            source="inference",
            source_run_id=run.id,
            lead_id=lead.id,
            category="fix",
            title=lead.title,
            description=f"{lead.hypothesis}\n\n{lead.context[-800:]}",
            severity="medium",
            confidence=0.6,
            evidence_refs=lead_refs[-MAX_LEAD_REFS:],
            repository_id=service.repository_id,
            service_id=service.id,
            path=lead.suspected_path,
            signal=lead.signal,
            fingerprint=finding_fingerprint(service.id, lead.signal or lead.title, "fix"),
            evidence_strength=0.5 + 0.25 * min(len(lead_refs), 6),
        )
        session.add(finding)
        session.flush()
        return finding

    @staticmethod
    def _resolve_investigation(
        session: Session, lead: Lead, outcome: str, finding: Finding | None
    ) -> None:
        """A reviewer-requested investigation returns its task to the review queue."""
        if lead.source != "review_request" or not lead.task_id:
            return
        task = session.get(Task, lead.task_id)
        if task is None or task.status != "investigating":
            return
        note = {
            "type": "investigation",
            "lead_id": lead.id,
            "outcome": outcome,
            "context": lead.context[-800:],
        }
        if finding is not None:
            note["finding_id"] = finding.id
            finding.task_id = task.id
            finding.status = "grouped"
        task.evidence = [*task.evidence, note]
        audit_log.transition_task(
            session, task, "in_review", details={"investigation": outcome, "lead_id": lead.id}
        )
