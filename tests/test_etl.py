from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from opsmith.etl.features import compute_features, direction, percentile
from opsmith.etl.pipeline import BundleBuilder, normalize_severity
from opsmith.etl.templates import TemplateMiner, extract_locations, fingerprint_of, mask
from opsmith.models import AnalysisBundle, Job, RawBatch, Schedule, Service
from opsmith.schemas import MetricPoint
from tests.conftest import error_records, metric_records, write_logs


def test_mask_and_clustering_are_stable() -> None:
    assert mask("took 123ms for id=42 uuid 123e4567-e89b-12d3-a456-426614174000") == (
        "took <num> for id=<num> uuid <uuid>"
    )
    miner = TemplateMiner()
    for i in range(20):
        miner.add(f"connection reset by peer 10.0.0.{i} after {i} retries", i)
    miner.add("disk full on /var/lib", 99)
    clusters = miner.clusters()
    assert clusters[0].count == 20
    assert len(clusters) == 2
    assert fingerprint_of("svc", clusters[0].template) == fingerprint_of(
        "svc", clusters[0].template
    )


def test_extract_locations_python_jvm_js() -> None:
    text = (
        'File "app/db.py", line 42, in q\n'
        "at com.acme.Pay.charge(Pay.java:17)\n"
        "    at handler (src/routes/pay.ts:88:13)"
    )
    locs = extract_locations(text)
    assert ("app/db.py", 42) in locs
    assert ("Pay.java", 17) in locs
    assert ("src/routes/pay.ts", 88) in locs


def test_metric_features_detect_regressions_by_direction() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    window = start + timedelta(hours=6)

    def pts(name: str, base: float, cur: float) -> list[MetricPoint]:
        out = [
            MetricPoint(
                timestamp=start + timedelta(minutes=5 * i),
                service="s",
                name=name,
                value=base + (i % 3),
            )
            for i in range(72)
        ]
        out += [
            MetricPoint(
                timestamp=window + timedelta(minutes=5 * i),
                service="s",
                name=name,
                value=cur + (i % 3),
            )
            for i in range(72)
        ]
        return out

    feats = {
        f["name"]: f
        for f in compute_features(
            pts("latency_ms", 100, 400) + pts("throughput_rps", 500, 200) + pts("error_rate", 1, 1),
            window,
        )
    }
    assert feats["latency_ms"]["anomalous"]
    assert feats["latency_ms"]["delta_pct"] > 200
    assert feats["throughput_rps"]["anomalous"]  # lower is worse
    assert not feats["error_rate"]["anomalous"]
    assert direction("http_requests_success_total") == "lower_is_worse"
    assert percentile([1, 2, 3, 4], 0.5) == 2.5


def test_severity_aliases() -> None:
    assert normalize_severity("WARN") == "warning"
    assert normalize_severity("Fatal") == "critical"
    assert normalize_severity("weird") == "info"


def test_ingest_stores_redacted_batches_and_builds_bundle(platform, seed, drain) -> None:
    start = datetime.fromisoformat(seed.log_connector.cursor["last_end"])
    write_logs(seed.logs, error_records(start, 12))
    write_logs(
        seed.logs,
        [
            {
                "timestamp": (start + timedelta(minutes=1)).isoformat(),
                "level": "info",
                "service": "payments-api",
                "message": "login ok for bob@example.com",
            }
        ],
    )
    write_logs(seed.metrics, metric_records(start, 60, 100))
    end = start + timedelta(hours=6)
    with platform.db.session() as s:
        result = platform.ingestion.run_connector(s, seed.log_connector.id, end=end)
        platform.ingestion.run_connector(s, seed.metric_connector.id, end=end)
        assert result["records"] == 13
    with platform.db.session() as s:
        batch = s.scalars(select(RawBatch).where(RawBatch.kind == "logs")).one()
        raw = "\n".join(str(r) for r in platform.store.iter_jsonl(batch.object_key))
        assert "bob@example.com" not in raw
        assert batch.redactions.get("email") == 1
        conn = s.get(type(seed.log_connector), seed.log_connector.id)
        assert conn.cursor["last_end"] == end.isoformat()
        assert s.scalar(select(Job).where(Job.kind == "build_bundles")) is not None
    drain()
    with platform.db.session() as s:
        bundle = s.scalars(select(AnalysisBundle)).one()
        top = bundle.log_templates[0]
        assert top["count"] == 12
        assert top["locations"][0] == {"path": "app/payments.py", "line": 10}
        assert top["ref"].startswith(f"bundle:{bundle.id}#tpl:")
        assert top["event_refs"][0].startswith("obj:")
        assert bundle.stats["events"] == 13
        assert bundle.metric_features
        assert all(b.bundle_id == bundle.id for b in s.scalars(select(RawBatch)))


def test_unknown_service_is_discovered_with_schedule(platform, seed) -> None:
    start = datetime.fromisoformat(seed.log_connector.cursor["last_end"])
    write_logs(seed.logs, error_records(start, 3, service="checkout-api"))
    with platform.db.session() as s:
        platform.ingestion.run_connector(s, seed.log_connector.id, end=start + timedelta(hours=6))
    with platform.db.session() as s:
        svc = s.scalar(select(Service).where(Service.name == "checkout-api"))
        assert svc is not None
        assert s.scalar(
            select(Schedule).where(Schedule.target_id == svc.id, Schedule.job_kind == "inference")
        )


def test_bundle_is_trimmed_to_token_budget(platform, seed) -> None:
    start = datetime.fromisoformat(seed.log_connector.cursor["last_end"])
    records = []
    for i in range(60):
        records += error_records(
            start, 2, message=f"distinct failure family{i} " + "x" * 300 + " code {i}"
        )
    write_logs(seed.logs, records)
    with platform.db.session() as s:
        platform.ingestion.run_connector(s, seed.log_connector.id, end=start + timedelta(hours=6))
        bundle = BundleBuilder(platform.store, max_tokens=2000).build_for_project(
            s, seed.project.id
        )[0]
        assert bundle.token_estimate <= 2200
        assert len(bundle.log_templates) < 40
