import json
from datetime import UTC, datetime, timedelta, timezone

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


# --------------------------------------------------------------------------- hardening


def test_severity_names_and_numeric_levels() -> None:
    assert normalize_severity("SEVERE") == "error"  # java.util.logging
    assert normalize_severity("[WARN]") == "warning"
    assert normalize_severity("ERROR:") == "error"
    assert normalize_severity("FINE") == "debug"
    # pino/bunyan numeric levels
    assert [normalize_severity(str(n)) for n in (10, 20, 30, 40, 50, 60)] == [
        "debug",
        "debug",
        "info",
        "warning",
        "error",
        "critical",
    ]
    assert normalize_severity("3") == "info"  # not a pino level; unknown stays info


def _series(name: str, base: list[float], cur: list[float]) -> dict:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    window = start + timedelta(hours=6)
    points = [
        MetricPoint(timestamp=start + timedelta(minutes=i), service="s", name=name, value=v)
        for i, v in enumerate(base)
    ] + [
        MetricPoint(timestamp=window + timedelta(minutes=i), service="s", name=name, value=v)
        for i, v in enumerate(cur)
    ]
    return compute_features(points, window)[0]


def test_flat_baseline_change_has_a_direction() -> None:
    # success_rate is lower-is-worse: a 40% drop from a perfectly flat baseline is a regression.
    drop = _series("success_rate", [100.0] * 72, [60.0] * 72)
    assert drop["anomalous"]
    assert drop["z_score"] == -999.0
    # ... and the same change upwards is an improvement, not an anomaly.
    rise = _series("success_rate", [60.0] * 72, [100.0] * 72)
    assert not rise["anomalous"]
    assert rise["z_score"] == 999.0
    worse = _series("latency_ms", [100.0] * 72, [140.0] * 72)
    assert worse["anomalous"]
    assert worse["z_score"] == 999.0
    better = _series("latency_ms", [100.0] * 72, [60.0] * 72)
    assert not better["anomalous"]
    assert better["z_score"] == -999.0


def test_non_finite_points_do_not_poison_a_series() -> None:
    nan, inf = float("nan"), float("inf")
    clean = _series("latency_ms", [100.0, 101.0] * 36, [400.0] * 72)
    for base, cur in (
        ([100.0, 101.0] * 36 + [nan], [400.0] * 72),
        ([100.0, 101.0] * 36 + [inf, -inf], [400.0] * 72),
        ([100.0, 101.0] * 36, [400.0] * 72 + [nan, inf]),
    ):
        feat = _series("latency_ms", base, cur)
        assert feat["anomalous"]
        assert (feat["mean"], feat["baseline_mean"], feat["count"]) == (
            clean["mean"],
            clean["baseline_mean"],
            clean["count"],
        )


def test_locations_keep_dot_directories_and_long_tokens_are_cheap() -> None:
    text = (
        'File ".github/scripts/deploy.py", line 3\n'
        "at x (.venv/lib/a.py:9)\n"
        'File "./src/app.py", line 1\n'
        'File "../../lib/util.py", line 2\n'
        'File "/srv/app/main.py", line 7'
    )
    assert extract_locations(text) == [
        (".github/scripts/deploy.py", 3),
        ("src/app.py", 1),
        ("lib/util.py", 2),
        ("srv/app/main.py", 7),
        (".venv/lib/a.py", 9),
    ]
    # One long token used to cost time quadratic in its length (41 s for 40 kB of "a/").
    # No timing assertion: a regression shows as a hang.
    for unit in ("a", "a/", "a.", "ab/cd.py:"):
        assert extract_locations(unit * (400_000 // len(unit))) == []
    deep = "/".join(["pkg"] * 40) + "/mod.py:12"
    assert extract_locations(f"error at {deep}") == [(deep.rsplit(":", 1)[0], 12)]


def test_quiet_metric_rows_are_trimmed_before_any_log_template() -> None:
    builder = BundleBuilder(store=None, max_tokens=3000)  # type: ignore[arg-type]
    templates = [
        {"ref": f"t{i}", "samples": ["s" * 200] * 3, "event_refs": ["e"] * 5} for i in range(3)
    ]
    features = [
        {
            "series": f"http_requests_total{{route=/r{i}}}",
            "anomalous": i in (7, 90),
            "pad": "x" * 80,
        }
        for i in range(200)
    ]
    kept_templates, kept_features = builder._fit(templates, features)
    assert [t["ref"] for t in kept_templates] == ["t0", "t1", "t2"]
    assert [f["anomalous"] for f in kept_features[:2]] == [True, True]  # anomalies survive
    assert 2 < len(kept_features) < 200
    assert len(json.dumps({"t": kept_templates, "m": kept_features})) // 4 <= 3000

    # Only when the templates alone exceed the budget are templates dropped (lowest weight last).
    big = [{"ref": f"t{i}", "samples": ["s" * 400], "event_refs": []} for i in range(40)]
    kept_templates, kept_features = builder._fit(big, features)
    assert 0 < len(kept_templates) < 40
    assert [t["ref"] for t in kept_templates] == [f"t{i}" for i in range(len(kept_templates))]
    assert all(f["anomalous"] for f in kept_features)


def test_overlapping_baselines_count_each_point_once(platform, seed) -> None:
    start = datetime.fromisoformat(seed.metric_connector.cursor["last_end"])
    records = []
    for i in range(144):  # 6h baseline, then two 3h windows; the second one regresses 4x
        ts = start - timedelta(hours=6) + timedelta(minutes=5 * i)
        value = 400.0 if ts >= start + timedelta(hours=3) else 100.0 + (i % 2)
        records.append(
            {
                "timestamp": ts.isoformat(),
                "name": "latency_ms",
                "service": "payments-api",
                "value": value,
            }
        )
    write_logs(seed.metrics, records)
    with platform.db.session() as s:
        for hours in (3, 6):  # both batches carry a 6h look-back, which overlaps
            platform.ingestion.run_connector(
                s, seed.metric_connector.id, end=start + timedelta(hours=hours)
            )
        feature = platform.bundles.build_for_project(s, seed.project.id)[0].metric_features[0]
        assert (feature["count"], feature["baseline_count"]) == (72, 72)
        assert feature["mean"] == 250.25


def test_first_and_last_seen_are_ordered_by_time_not_text(platform, seed) -> None:
    start = datetime.fromisoformat(seed.log_connector.cursor["last_end"]).replace(microsecond=0)
    early = start + timedelta(minutes=10)
    plus2 = timezone(timedelta(hours=2))
    stamps = [
        (early + timedelta(minutes=30)).isoformat(),  # +00:00
        early.astimezone(plus2).isoformat(),  # the earliest instant, written with +02:00
        (early + timedelta(minutes=30, microseconds=500_000)).isoformat(),  # the latest
        (early + timedelta(minutes=5)).replace(tzinfo=None).isoformat(),  # naive: taken as UTC
    ]
    write_logs(
        seed.logs,
        [
            {"timestamp": ts, "level": "error", "service": "payments-api", "message": "boom a"}
            for ts in stamps
        ],
    )
    with platform.db.session() as s:
        platform.ingestion.run_connector(s, seed.log_connector.id, end=start + timedelta(hours=6))
        template = platform.bundles.build_for_project(s, seed.project.id)[0].log_templates[0]
    assert template["count"] == 4
    assert datetime.fromisoformat(template["first_seen"]) == early
    assert datetime.fromisoformat(template["last_seen"]) == early + timedelta(
        minutes=30, microseconds=500_000
    )
    assert template["first_seen"].endswith("Z")
    assert template["last_seen"].endswith("Z")
