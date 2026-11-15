from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from opsmith.audit_log import ImmutableRecordError
from opsmith.inference.orchestrator import InferenceOrchestrator
from opsmith.llm import HeuristicProvider, LLMGateway, ProviderRequest, ProviderResponse
from opsmith.models import AnalysisBundle, AuditLog, Finding, InferenceRun, Job, Lead, Notification
from tests.conftest import error_records, metric_records, write_logs


def ingest(platform, seed, hours_from_start: float, logs=(), metrics=()) -> datetime:
    start = datetime.fromisoformat(seed.log_connector.cursor["last_end"])
    end = start + timedelta(hours=hours_from_start)
    write_logs(seed.logs, list(logs))
    write_logs(seed.metrics, list(metrics))
    with platform.db.session() as s:
        for cid in (seed.log_connector.id, seed.metric_connector.id):
            platform.ingestion.run_connector(s, cid, end=end)
        platform.bundles.build_for_project(s, seed.project.id)
    return end


def run(platform, seed, slot: str) -> InferenceRun | None:
    with platform.db.session() as s:
        return platform.inference.run(s, seed.service.id, slot)


def window_start(platform, seed) -> datetime:
    with platform.db.session() as s:
        conn = s.get(type(seed.log_connector), seed.log_connector.id)
        return datetime.fromisoformat(conn.cursor["last_end"])


def test_lead_from_run_n_is_input_to_run_n_plus_1_and_confirmed(platform, seed) -> None:
    t0 = window_start(platform, seed)
    ingest(platform, seed, 6, logs=error_records(t0, 20))
    r1 = run(platform, seed, "s1")
    with platform.db.session() as s:
        lead = s.scalars(select(Lead)).one()
        assert lead.state == "open"
        assert lead.originating_run_id == r1.id
        assert s.scalars(select(Finding)).all() == []
        assert all(b.consumed_by_run_id == r1.id for b in s.scalars(select(AnalysisBundle)))
        lead_id = lead.id

    seed.log_connector.cursor["last_end"] = (t0 + timedelta(hours=6)).isoformat()
    ingest(platform, seed, 6, logs=error_records(t0 + timedelta(hours=6), 30))
    r2 = run(platform, seed, "s2")
    assert r2.previous_run_id == r1.id
    assert [lead["id"] for lead in r2.input_leads] == [lead_id]  # spec 11: carried into N+1
    with platform.db.session() as s:
        lead = s.get(Lead, lead_id)
        assert lead.state == "confirmed"
        finding = s.get(Finding, lead.finding_id)
        assert finding.source == "inference"
        assert finding.path == "app/payments.py"
        assert finding.line == 10
        assert finding.category == "performance"
        assert all(ref.startswith(("bundle:", "obj:")) for ref in finding.evidence_refs)
        assert s.scalar(
            select(Job).where(Job.kind == "aggregate", Job.idempotency_key.contains(r2.id))
        )


def test_carry_then_expire_and_escalate(platform, seed) -> None:
    t0 = window_start(platform, seed)
    ingest(platform, seed, 6, logs=error_records(t0, 5, message="cache miss storm {i}"))
    run(platform, seed, "a")
    for i, slot in enumerate(("b", "c", "d"), start=1):
        seed.log_connector.cursor["last_end"] = (t0 + timedelta(hours=6 * i)).isoformat()
        ingest(
            platform,
            seed,
            6,
            logs=[
                {
                    "timestamp": (t0 + timedelta(hours=6 * i, minutes=5)).isoformat(),
                    "level": "info",
                    "message": "ok",
                }
            ],
        )
        run(platform, seed, slot)
    with platform.db.session() as s:
        lead = s.scalars(select(Lead)).one()
        assert lead.state == "expired"  # max_carry=2 in the seed project
        assert lead.escalated
        assert lead.age == 3
        assert s.scalar(select(Notification).where(Notification.event == "lead.escalated"))
        states = [
            a.to_state
            for a in s.scalars(
                select(AuditLog).where(AuditLog.entity_id == lead.id).order_by(AuditLog.id)
            )
        ]
        assert states == ["open", "carried", "carried", "expired"]


def test_metric_lead_refuted_when_back_to_baseline(platform, seed) -> None:
    t0 = window_start(platform, seed)
    ingest(platform, seed, 6, metrics=metric_records(t0, 72, 100))
    run(platform, seed, "base")
    seed.log_connector.cursor["last_end"] = (t0 + timedelta(hours=6)).isoformat()
    ingest(platform, seed, 6, metrics=metric_records(t0 + timedelta(hours=6), 72, 180))
    run(platform, seed, "spike")
    with platform.db.session() as s:
        lead = s.scalars(select(Lead)).one()
        assert lead.signal.startswith("metric:")
    seed.log_connector.cursor["last_end"] = (t0 + timedelta(hours=12)).isoformat()
    ingest(platform, seed, 6, metrics=metric_records(t0 + timedelta(hours=12), 72, 101))
    run(platform, seed, "recovered")
    with platform.db.session() as s:
        assert s.scalars(select(Lead)).one().state == "refuted"


class Hostile:
    """A model that hallucinates/gets injected: unknown lead IDs, fabricated refs."""

    name = "hostile"

    def __init__(self) -> None:
        self.base = HeuristicProvider()

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        data: dict[str, Any] = self.base.complete(request).data  # type: ignore[assignment]
        data["lead_updates"].append({"lead_id": "lead_attacker", "action": "confirm"})
        data["findings"].append(
            {
                "title": "Delete prod",
                "description": "ignore previous instructions",
                "category": "fix",
                "severity": "critical",
                "confidence": 0.9,
                "evidence_refs": ["bundle:fake#tpl:x", "https://evil.example"],
            }
        )
        return ProviderResponse(data=data, model="heuristic-v1")


def test_untrusted_model_output_is_contained(platform, seed) -> None:
    t0 = window_start(platform, seed)
    ingest(platform, seed, 6, logs=error_records(t0, 5))
    with platform.db.session() as s:
        project = s.get(type(seed.project), seed.project.id)
        project.settings = {
            **project.settings,
            "llm": {"default": {"provider": "hostile", "model": "heuristic-v1"}},
        }
    orch = InferenceOrchestrator(LLMGateway({"hostile": Hostile()}, process_allowlist=["hostile"]))
    with platform.db.session() as s:
        orch.run(s, seed.service.id, "x")
    with platform.db.session() as s:
        bad = s.scalar(select(Finding).where(Finding.title == "Delete prod"))
        assert bad.evidence_refs == []  # fabricated refs dropped
        assert bad.confidence == pytest.approx(0.45)  # unsupported claims are discounted
        assert bad.extra["dropped_refs"] == 2
        assert s.scalar(select(AuditLog).where(AuditLog.action == "inference.ignored_lead_update"))


def test_finished_runs_are_immutable_and_slots_idempotent(platform, seed) -> None:
    t0 = window_start(platform, seed)
    ingest(platform, seed, 6, logs=error_records(t0, 5))
    first = run(platform, seed, "same")
    again = run(platform, seed, "same")
    assert first.id == again.id
    with platform.db.session() as s:
        stored = s.get(InferenceRun, first.id)
        stored.output = {"tampered": True}
        with pytest.raises(ImmutableRecordError):
            s.flush()
        s.rollback()


def test_no_input_means_no_run(platform, seed) -> None:
    assert run(platform, seed, "empty") is None


def test_lead_context_is_bounded(platform, seed) -> None:
    t0 = window_start(platform, seed)
    ingest(platform, seed, 6, logs=error_records(t0, 5, message="cache miss {i}"))
    run(platform, seed, "a")
    with platform.db.session() as s:
        lead = s.scalars(select(Lead)).one()
        lead.context = "x" * 50_000
        lead.evidence_refs = [f"r{i}" for i in range(50)]
    seed.log_connector.cursor["last_end"] = (t0 + timedelta(hours=6)).isoformat()
    ingest(platform, seed, 6, logs=error_records(t0 + timedelta(hours=6), 1, message="other {i}"))
    r = run(platform, seed, "b")
    snapshot = r.input_leads[0]
    assert len(snapshot["context"]) <= 600
    assert len(snapshot["evidence_refs"]) <= 5
