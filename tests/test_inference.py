from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from opsmith.audit_log import ImmutableRecordError
from opsmith.db import utcnow
from opsmith.inference.orchestrator import InferenceOrchestrator
from opsmith.llm import HeuristicProvider, LLMGateway, ProviderRequest, ProviderResponse
from opsmith.models import (
    AnalysisBundle,
    AuditLog,
    Finding,
    InferenceRun,
    Job,
    Lead,
    Notification,
    Task,
    UsageRecord,
)
from opsmith.provenance import task_provenance
from opsmith.queue import enqueue
from opsmith.schemas import InferenceOutput
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


# --------------------------------------------------------------------------- scripted models


class Scripted:
    """A provider that answers with fixed output (or what ``fn`` builds from the request)."""

    name = "scripted"

    def __init__(self, fn: Any, *, model: str = "heuristic-v1", tokens: int = 0) -> None:
        self.fn = fn
        self.model = model
        self.tokens = tokens
        self.requests: list[ProviderRequest] = []

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        data = self.fn(request) if callable(self.fn) else self.fn
        return ProviderResponse(
            data=data, input_tokens=self.tokens, output_tokens=self.tokens, model=self.model
        )


def use(platform, seed, provider: Scripted, **settings: Any) -> None:
    """Route the platform's own gateway (orchestrator and worker included) to ``provider``."""
    platform.gateway.providers["scripted"] = provider
    platform.gateway.process_allowlist.add("scripted")
    with platform.db.session() as s:
        project = s.get(type(seed.project), seed.project.id)
        project.settings = {
            **project.settings,
            "llm": {"default": {"provider": "scripted", "model": provider.model}},
            **settings,
        }


def next_window(platform, seed, t0: datetime, n: int, logs=(), metrics=()) -> None:
    seed.log_connector.cursor["last_end"] = (t0 + timedelta(hours=6 * n)).isoformat()
    ingest(platform, seed, 6, logs=logs, metrics=metrics)


FINDING = {
    "title": "t",
    "description": "d",
    "category": "fix",
    "severity": "low",
    "confidence": 0.5,
}


def test_failed_stage_keeps_its_billed_usage_and_surfaces(platform, seed, drain) -> None:
    t0 = window_start(platform, seed)
    ingest(platform, seed, 6, logs=error_records(t0, 20))
    broken = Scripted({"findings": "not-a-list"}, model="claude-opus-5-5", tokens=50_000)
    use(platform, seed, broken, budgets={"max_cost_per_day_usd": 2.0})
    with platform.db.session() as s:
        enqueue(
            s,
            tenant_id=seed.tenant.id,
            kind="inference",
            payload={"service_id": seed.service.id, "slot": "s1"},
            idempotency_key="inference:test:s1",
        )
    drain()
    with platform.db.session() as s:
        job = s.scalars(select(Job).where(Job.kind == "inference")).one()
        assert job.status == "succeeded"  # the handler returned, so its transaction committed
        assert job.result["status"] == "failed"
        run_row = s.scalars(select(InferenceRun)).one()
        assert run_row.status == "failed"
        assert "failed validation twice" in run_row.error
        usage = s.scalars(select(UsageRecord)).all()
        assert len(usage) == 2  # both billed attempts survive the failure
        assert sum(u.cost_usd for u in usage) == pytest.approx(2.4)
        assert {u.run_id for u in usage} == {run_row.id}
        assert s.scalar(select(Notification).where(Notification.event == "inference.failed"))
        assert s.scalar(select(AuditLog).where(AuditLog.action == "inference.failed"))
        # Nothing was analysed: the input is neither consumed nor dropped.
        assert all(b.consumed_by_run_id is None for b in s.scalars(select(AnalysisBundle)))
        assert s.scalars(select(Finding)).all() == []

    # The same slot is not paid for twice, and the recorded spend lets the day cap stop the
    # next slot before another call is made.
    assert run(platform, seed, "s1").id == run_row.id
    assert run(platform, seed, "s2").status == "budget_exceeded"
    assert len(broken.requests) == 2

    # Once the model behaves (and the budget allows), the next slot analyses the same bundles.
    healthy = Scripted(lambda r: HeuristicProvider().complete(r).data)
    use(platform, seed, healthy, budgets={"max_cost_per_day_usd": 50.0})
    assert run(platform, seed, "s3").status == "succeeded"
    with platform.db.session() as s:
        assert all(b.consumed_by_run_id for b in s.scalars(select(AnalysisBundle)))


def _two_leads(platform, seed) -> tuple[datetime, str, str]:
    t0 = window_start(platform, seed)
    ingest(
        platform,
        seed,
        6,
        logs=error_records(t0, 5, message="cache miss storm {i}")
        + error_records(t0, 5, message="queue lag exceeded {i}", path="app/queue.py"),
    )
    run(platform, seed, "a")
    with platform.db.session() as s:
        first, second = s.scalars(select(Lead).order_by(Lead.created_at, Lead.id)).all()
        return t0, first.id, second.id


def test_finding_index_only_addresses_the_models_own_findings(platform, seed) -> None:
    t0, lead_a, lead_b = _two_leads(platform, seed)
    use(
        platform,
        seed,
        Scripted(
            {
                "findings": [],
                "lead_updates": [
                    {"lead_id": lead_a, "action": "confirm"},  # no finding: one is synthesized
                    {"lead_id": lead_b, "action": "confirm", "finding_index": 0},  # out of range
                ],
            }
        ),
    )
    next_window(platform, seed, t0, 1, logs=error_records(t0 + timedelta(hours=6), 1))
    run(platform, seed, "b")
    with platform.db.session() as s:
        a, b = s.get(Lead, lead_a), s.get(Lead, lead_b)
        assert a.state == b.state == "confirmed"
        assert a.finding_id != b.finding_id  # B is not bound to the finding synthesized for A
        assert s.get(Finding, a.finding_id).lead_id == lead_a
        assert s.get(Finding, b.finding_id).lead_id == lead_b
        assert s.get(Finding, b.finding_id).title == b.title


def test_confirm_without_index_uses_the_finding_raised_from_that_lead(platform, seed) -> None:
    t0, lead_a, _ = _two_leads(platform, seed)
    with platform.db.session() as s:
        signal = s.get(Lead, lead_a).signal
    use(
        platform,
        seed,
        Scripted(
            {
                "findings": [
                    {**FINDING, "title": "unrelated", "signal": "other"},
                    {
                        **FINDING,
                        "title": "Cache miss storm overloads the database",
                        "category": "performance",
                        "signal": signal,
                        "from_lead_id": lead_a,
                    },
                ],
                "lead_updates": [{"lead_id": lead_a, "action": "confirm"}],
            }
        ),
    )
    next_window(platform, seed, t0, 1, logs=error_records(t0 + timedelta(hours=6), 1))
    run(platform, seed, "b")
    with platform.db.session() as s:
        findings = s.scalars(select(Finding)).all()
        assert len(findings) == 2  # no synthesized duplicate of the model's own finding
        lead = s.get(Lead, lead_a)
        confirmed = s.get(Finding, lead.finding_id)
        assert confirmed.title == "Cache miss storm overloads the database"
        assert confirmed.lead_id == lead_a


def test_redacted_identifiers_are_matched_back_to_the_real_ones(platform, seed) -> None:
    # The label value is harmless on its own, but the composed series key looks like a
    # credential assignment, so the gateway's redaction pass rewrites the ref and the signal.
    labels = {"namespace": "prod", "secret": "db-credentials"}

    def points(start: datetime, value: float) -> list[dict[str, Any]]:
        return [
            {**rec, "labels": labels}
            for rec in metric_records(start, 72, value, name="kube_secret_sync_latency_ms")
        ]

    recorder = Scripted(lambda r: HeuristicProvider().complete(r).data)
    use(platform, seed, recorder)
    t0 = window_start(platform, seed)
    ingest(platform, seed, 6, metrics=points(t0, 100))
    run(platform, seed, "base")
    next_window(platform, seed, t0, 1, metrics=points(t0 + timedelta(hours=6), 900))
    result = run(platform, seed, "spike")
    with platform.db.session() as s:
        bundle = s.scalars(
            select(AnalysisBundle).order_by(AnalysisBundle.window_start.desc())
        ).first()
        feature = bundle.metric_features[0]
        assert "db-credentials" in feature["ref"]
        finding = s.scalars(select(Finding)).one()
        assert finding.evidence_refs == [feature["ref"]]  # cited in redacted form, stored real
        assert finding.extra["dropped_refs"] == 0
        assert finding.confidence == result.output["findings"][0]["confidence"]  # not discounted
        assert finding.signal == feature["signal"]  # what post-merge verification compares
    # The redaction itself is intact: the provider never saw the value.
    assert all("db-credentials" not in r.user for r in recorder.requests)
    assert "[REDACTED:credential_assignment]" in recorder.requests[-1].user


def test_oversized_and_nonsensical_model_values_are_contained(platform, seed) -> None:
    out = InferenceOutput.model_validate(
        {
            "findings": [
                {**FINDING, "signal": "s" * 400, "suspected_path": "p" * 900, "suspected_line": n}
                for n in (10**30, 0, -4, 12)
            ],
            "new_leads": [
                {
                    "title": "T" * 300,
                    "hypothesis": "h",
                    "signal": "m" * 400,
                    "suspected_path": "a.py",
                }
            ],
        }
    )
    assert [f.suspected_line for f in out.findings] == [None, None, None, 12]
    assert {len(f.signal) for f in out.findings} == {200}  # still one stable identity
    assert {f.suspected_path for f in out.findings} == {None}  # a cut-off path is another file
    assert len(out.new_leads[0].signal) == 200

    t0 = window_start(platform, seed)
    ingest(platform, seed, 6, logs=error_records(t0, 5))
    use(
        platform,
        seed,
        Scripted(
            out.model_dump(mode="json")
            | {
                "findings": [
                    {
                        **FINDING,
                        "signal": "s" * 400,
                        "suspected_path": "p" * 900,
                        "suspected_line": 10**30,
                    }
                ]
            }
        ),
    )
    assert run(platform, seed, "a").status == "succeeded"  # used to die at flush time
    with platform.db.session() as s:
        finding = s.scalars(select(Finding)).one()
        assert (len(finding.signal), finding.path, finding.line) == (200, None, None)
        lead = s.scalars(select(Lead)).one()
        assert (len(lead.title), len(lead.signal)) == (300, 200)
    # Carried past max_carry (2 in the seed project), the lead escalates with a subject that
    # fits the notification column although its title alone fills it.
    use(platform, seed, Scripted({}))
    for slot in ("b", "c", "d"):
        run(platform, seed, slot)
    with platform.db.session() as s:
        note = s.scalars(select(Notification).where(Notification.event == "lead.escalated")).one()
        assert len(note.subject) == 300


def test_repeating_a_ref_does_not_strengthen_the_evidence(platform, seed) -> None:
    t0 = window_start(platform, seed)
    ingest(platform, seed, 6, logs=error_records(t0, 5))
    with platform.db.session() as s:
        ref = s.scalars(select(AnalysisBundle)).one().log_templates[0]["ref"]
    use(
        platform,
        seed,
        Scripted(
            {
                "findings": [
                    {**FINDING, "signal": "once", "evidence_refs": [ref]},
                    {**FINDING, "signal": "repeated", "evidence_refs": [ref] * 6 + ["bundle:x"]},
                ]
            }
        ),
    )
    run(platform, seed, "a")
    with platform.db.session() as s:
        once, repeated = s.scalars(select(Finding).order_by(Finding.signal)).all()
        assert repeated.evidence_refs == once.evidence_refs == [ref]
        assert repeated.evidence_strength == once.evidence_strength == 0.75
        assert repeated.extra["dropped_refs"] == 1  # only the fabricated one counts as dropped


def test_provenance_lists_an_investigation_lead_once(platform, seed) -> None:
    with platform.db.session() as s:
        ids = {"tenant_id": seed.tenant.id, "project_id": seed.project.id}
        task = Task(**ids, title="t", category="fix")
        s.add(task)
        s.flush()
        lead = Lead(
            **ids, service_id=seed.service.id, title="l", source="review_request", task_id=task.id
        )
        other = Lead(**ids, service_id=seed.service.id, title="other")
        s.add_all([lead, other])
        s.flush()
        for linked in (lead, other):  # the investigation lead is reachable both ways
            s.add(
                Finding(
                    **ids,
                    source="inference",
                    source_run_id="run_x",
                    lead_id=linked.id,
                    task_id=task.id,
                    category="fix",
                    title="f",
                    fingerprint=f"fp-{linked.id}",
                )
            )
        s.flush()
        listed = [row["id"] for row in task_provenance(s, task)["leads"]]
        assert sorted(listed) == sorted([lead.id, other.id])


# --------------------------------------------------------------------------- regressions (1.2.1)


@pytest.mark.parametrize("z_score", [9.0, 3.5])  # a finding, and a lead
def test_a_long_metric_series_name_does_not_stall_inference(platform, seed, z_score) -> None:
    """The built-in rules put the whole series key in a title. A series with a few ordinary
    labels is longer than the 300 characters a title may have, so the rules' own output failed
    validation; the bundle stayed unconsumed and every later run failed on it again."""
    labels = ",".join(f"label_{i}=value-number-{i}-of-a-kubernetes-pod" for i in range(8))
    key = "http_server_request_duration_seconds{" + labels + "}"
    assert len(key) > 300
    feature = {
        "series": key,
        "name": "http_server_request_duration_seconds",
        "signal": ("metric:" + key)[:200],
        "anomalous": True,
        "z_score": z_score,
        "delta_pct": 250.0,
        "ref": "bundle:bnd_long#metric:" + key,
    }
    now = utcnow()
    with platform.db.session() as s:
        s.add(
            AnalysisBundle(
                id="bnd_long",
                tenant_id=seed.tenant.id,
                project_id=seed.project.id,
                service_id=seed.service.id,
                window_start=now - timedelta(hours=6),
                window_end=now,
                metric_features=[feature],
                token_estimate=100,
            )
        )
    with platform.db.session() as s:
        run = platform.inference.run(s, seed.service.id, "slot-long")
        assert (run.status, run.error) == ("succeeded", None)
        assert s.get(AnalysisBundle, "bnd_long").consumed_by_run_id == run.id
