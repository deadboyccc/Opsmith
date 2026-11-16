"""Adversarial tests for ingestion: hostile connector configuration, hostile or malformed
signals, and overlapping jobs. Each test pins one way the pipeline used to leak, lose,
duplicate or wedge data."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from opsmith import queue
from opsmith.config import Settings
from opsmith.connectors import ConnectorError, FetchResult, LogConnector, build_connector
from opsmith.db import utcnow
from opsmith.etl import pipeline
from opsmith.etl.pipeline import (
    BundleBuilder,
    BundleBuildError,
    ConnectorDisabledError,
    IngestionService,
)
from opsmith.models import AnalysisBundle, Connector, Job, Project, RawBatch, Service
from opsmith.platform import Platform
from opsmith.schemas import LogEvent, ProjectSettings
from opsmith.security import create_tenant
from tests.conftest import Seed, error_records, metric_records, write_logs


@pytest.fixture
def ingestion(platform: Platform, settings: Settings) -> IngestionService:
    """Built the way the platform must build it: file connectors confined to the local root."""
    return IngestionService(
        platform.store, local_root=settings.local_root, local_deny=(settings.data_dir,)
    )


def _start(seed: Seed) -> datetime:
    return datetime.fromisoformat(seed.log_connector.cursor["last_end"])


def _rows(platform: Platform, batch: RawBatch) -> list[dict[str, Any]]:
    return list(platform.store.iter_jsonl(batch.object_key))


def _log(start: datetime, minutes: float, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "timestamp": (start + timedelta(minutes=minutes)).isoformat(),
        "level": "error",
        "service": "payments-api",
        "message": message,
        **extra,
    }


# --------------------------------------------------------------------------- confinement


def test_platform_confines_file_connectors(platform: Platform, settings: Settings) -> None:
    assert platform.ingestion.local_root == settings.local_root
    assert settings.data_dir in platform.ingestion.local_deny


def test_file_connector_cannot_read_another_tenants_stored_signals(
    platform: Platform, settings: Settings, seed: Seed, ingestion: IngestionService
) -> None:
    start = _start(seed)
    end = start + timedelta(hours=6)
    write_logs(seed.logs, error_records(start, 3, message="ACME-CONFIDENTIAL order {i} failed"))
    with platform.db.session() as s:
        ingestion.run_connector(s, seed.log_connector.id, end=end)

    targets = {
        "the object store": f"{settings.object_store_dir}/**/logs/*.jsonl",
        "a broad glob that reaches the object store": f"{settings.local_root}/**/logs/*.jsonl",
        "a file outside the local root": "/etc/hostname",
        "a glob outside the local root": "/proc/self/*",
    }
    with platform.db.session() as s:
        evil = create_tenant(s, "evil")
        project = Project(
            tenant_id=evil.id, name="p", settings=ProjectSettings().model_dump(mode="json")
        )
        s.add(project)
        s.flush()
        connectors = {}
        for i, (why, path) in enumerate(targets.items()):
            conn = Connector(
                tenant_id=evil.id,
                project_id=project.id,
                name=f"steal-{i}",
                type="file_logs",
                kind="logs",
                config={"path": path},
                cursor={"last_end": start.isoformat()},
            )
            s.add(conn)
            s.flush()
            connectors[why] = conn.id
        evil_id = evil.id
    for connector_id in connectors.values():
        with platform.db.session() as s:
            # Not retryable: the job goes straight to dead with the reason.
            with pytest.raises(queue.PermanentFailure, match="file connector"):
                ingestion.run_connector(s, connector_id, end=end)
            s.rollback()
    with platform.db.session() as s:
        assert not s.scalars(select(RawBatch).where(RawBatch.tenant_id == evil_id)).all()

    # With no local root configured, file connectors are off altogether.
    unconfined = IngestionService(platform.store)
    with platform.db.session() as s:
        with pytest.raises(queue.PermanentFailure, match="disabled"):
            unconfined.run_connector(s, seed.log_connector.id, end=end + timedelta(hours=1))
        s.rollback()


def test_connector_cannot_reach_the_metadata_endpoint(
    platform: Platform, seed: Seed, ingestion: IngestionService
) -> None:
    with platform.db.session() as s:
        for i, url in enumerate(
            ["http://169.254.169.254/latest/meta-data?x=", "http://169.254.169.254", "file:///etc"]
        ):
            conn = Connector(
                tenant_id=seed.tenant.id,
                project_id=seed.project.id,
                name=f"ssrf-{i}",
                type="loki",
                kind="logs",
                config={"url": url, "query": "{}"},
            )
            s.add(conn)
            s.flush()
            with pytest.raises(ConnectorError, match=r"url|host"):
                ingestion.run_connector(s, conn.id)


def test_factories_and_connectors_with_the_old_signature_still_work(
    platform: Platform, seed: Seed
) -> None:
    """An out-of-tree connector (or factory) that predates ``local_root`` keeps working."""
    start = _start(seed)

    class Legacy(LogConnector):
        def __init__(self, config: dict[str, Any], *, default_service: str = "unknown") -> None:
            self.config, self.default_service = config, default_service

        def fetch(self, start: datetime, end: datetime, cursor: dict[str, Any]) -> FetchResult:
            event = LogEvent(timestamp=start, service=self.default_service, message="legacy")
            return FetchResult(logs=[event], cursor={"last_end": end.isoformat()})

    def factory(
        type_name: str, config: dict[str, Any], *, default_service: str, credential: str | None
    ) -> Legacy:
        return Legacy(config, default_service=default_service)

    service = IngestionService(platform.store, factory, local_root=Path("/nowhere"))
    with platform.db.session() as s:
        result = service.run_connector(s, seed.log_connector.id, end=start + timedelta(hours=1))
    assert result["records"] == 1
    assert result["skipped_records"] == 0


# --------------------------------------------------------------------------- hostile signals


def test_one_malformed_line_does_not_wedge_the_connector(
    platform: Platform, seed: Seed, ingestion: IngestionService
) -> None:
    start = _start(seed)
    write_logs(seed.logs, error_records(start, 3))
    with seed.logs.open("a") as fh:
        fh.write(json.dumps(_log(start, 1, "deploy ok", version=3, trace_id=99)) + "\n")
        fh.write('{"timestamp": "yesterday", "message": "bad timestamp"}\n')
        fh.write('{"message": "no timestamp"}\n')
        fh.write(json.dumps(_log(start, 2, "nested id", trace_id={"a": 1})) + "\n")
        fh.write("not json at all\n")
    with platform.db.session() as s:
        result = ingestion.run_connector(s, seed.log_connector.id, end=start + timedelta(hours=6))
        assert result["records"] == 4
        assert result["skipped_records"] == 4  # visible in the job result, not silent
        conn = s.get(Connector, seed.log_connector.id)
        assert conn is not None
        assert conn.cursor["last_end"] == (start + timedelta(hours=6)).isoformat()
        rows = _rows(platform, s.scalars(select(RawBatch)).one())
        deploy = next(r for r in rows if r["message"] == "deploy ok")
        assert (deploy["version"], deploy["trace_id"]) == ("3", "99")


@pytest.mark.parametrize(
    "bad",
    [
        {"environment": "e" * 51},  # longer than services.environment (fails on PostgreSQL)
        {"service": "s" * 201},  # longer than services.name
        {"payload": "deep"},  # nested deeper than pydantic serializes
    ],
)
def test_one_unstorable_record_does_not_wedge_the_connector(
    platform: Platform, seed: Seed, ingestion: IngestionService, bad: dict[str, Any]
) -> None:
    if bad.get("payload") == "deep":
        nested: Any = "x"
        for _ in range(300):
            nested = {"a": nested}
        bad = {"payload": nested}
    start = _start(seed)
    write_logs(seed.logs, error_records(start, 3))
    with seed.logs.open("a") as fh:
        fh.write(json.dumps(_log(start, 1, "unstorable", **bad)) + "\n")
    with platform.db.session() as s:
        result = ingestion.run_connector(s, seed.log_connector.id, end=start + timedelta(hours=6))
        assert result["records"] == 3
        assert result["skipped_records"] == 1  # counted, not silent
        conn = s.get(Connector, seed.log_connector.id)
        assert conn is not None
        assert conn.cursor["last_end"] == (start + timedelta(hours=6)).isoformat()


def test_non_finite_metric_values_are_dropped_not_fatal(
    platform: Platform, seed: Seed, ingestion: IngestionService, drain: Any
) -> None:
    start = _start(seed)
    records = metric_records(start - timedelta(hours=5), 60, 100) + metric_records(start, 60, 400)
    records[3]["value"] = float("nan")  # in the baseline look-back
    records[5]["value"] = float("inf")
    records[70]["value"] = float("-inf")  # in the current window
    write_logs(seed.metrics, records)  # json.dumps writes NaN / Infinity, json.loads reads them
    end = start + timedelta(hours=6)
    with platform.db.session() as s:
        result = ingestion.run_connector(s, seed.metric_connector.id, end=end)
        assert result["records"] == 117
        assert result["skipped_records"] == 3
    assert all(r.status == "succeeded" for r in drain())
    with platform.db.session() as s:
        batch = s.scalars(select(RawBatch)).one()
        assert all(
            isinstance(r["value"], float) and abs(r["value"]) < 1e9 for r in _rows(platform, batch)
        )
        feature = s.scalars(select(AnalysisBundle)).one().metric_features[0]
        assert feature["anomalous"]
        assert feature["mean"] is not None
        assert (feature["count"], feature["baseline_count"]) == (59, 58)

    # The push API takes the same route.
    with platform.db.session() as s:
        conn = s.get(Connector, seed.metric_connector.id)
        assert conn is not None
        pushed = ingestion.ingest_push(
            s,
            conn,
            [
                {"timestamp": utcnow().isoformat(), "service": "x", "name": "n", "value": v}
                for v in ("NaN", "Infinity", 1.5)
            ],
        )
        assert [b.record_count for b in pushed] == [1]


def test_pushed_naive_timestamps_are_utc_and_never_break_bundling(
    platform: Platform, seed: Seed, ingestion: IngestionService, drain: Any
) -> None:
    with platform.db.session() as s:
        metrics = s.get(Connector, seed.metric_connector.id)
        logs = s.get(Connector, seed.log_connector.id)
        assert metrics is not None
        assert logs is not None
        ingestion.ingest_push(
            s,
            metrics,
            [
                {"timestamp": "2026-03-01T00:00:00", "service": "api", "name": "lat", "value": 1.0},
                {
                    "timestamp": "2026-03-01T02:00:05+02:00",
                    "service": "api",
                    "name": "lat",
                    "value": 2,
                },
            ],
        )
        ingestion.ingest_push(  # naive and aware in one push used to raise TypeError (HTTP 500)
            s,
            logs,
            [
                {"timestamp": "2026-03-01T00:00:00", "service": "api", "message": "naive"},
                {"timestamp": "2026-03-01T00:00:01Z", "service": "api", "message": "aware"},
            ],
        )
    assert [r.status for r in drain() if r.kind == "build_bundles"]
    with platform.db.session() as s:
        assert not s.scalars(select(Job).where(Job.status != "succeeded")).all()
        bundle = s.scalars(select(AnalysisBundle)).one()
        assert bundle.stats["events"] == 2
        assert bundle.metric_features[0]["count"] == 2
        assert bundle.window_start == datetime(2026, 3, 1, tzinfo=UTC)
        stamps = [r["timestamp"] for b in s.scalars(select(RawBatch)) for r in _rows(platform, b)]
        assert stamps
        assert all(ts.endswith("Z") for ts in stamps)
        assert "2026-03-01T00:00:05Z" in stamps  # +02:00 was converted, not just relabelled


def test_oversized_fields_are_truncated_before_any_pattern_runs(
    platform: Platform, seed: Seed, ingestion: IngestionService
) -> None:
    start = _start(seed)
    limit = pipeline.MAX_FIELD_CHARS
    blob = "a/" * 150_000  # 300 kB in one token: minutes of regex time before the cap
    write_logs(
        seed.logs,
        [
            _log(
                start,
                1,
                "payload " + blob,
                stack=blob + " app/payments.py:10",
                nested={"k": [blob]},
            ),
            _log(start, 2, "short", trace_id=blob, version=blob),
        ],
    )
    write_logs(
        seed.metrics,
        [{**metric_records(start, 1, 1.0)[0], "labels": {"path": blob}}],
    )
    end = start + timedelta(hours=6)
    with platform.db.session() as s:
        ingestion.run_connector(s, seed.log_connector.id, end=end)
        ingestion.run_connector(s, seed.metric_connector.id, end=end)
        rows = [r for b in s.scalars(select(RawBatch)) for r in _rows(platform, b)]

        def strings(value: Any) -> list[str]:
            if isinstance(value, str):
                return [value]
            if isinstance(value, dict):
                return [x for v in value.values() for x in strings(v)]
            if isinstance(value, list):
                return [x for v in value for x in strings(v)]
            return []

        longest = max(len(text) for row in rows for text in strings(row))
        assert longest <= limit + 40
        big = next(r for r in rows if r.get("message", "").startswith("payload"))
        assert big["message"].endswith(f"...[truncated {len('payload ' + blob) - limit} chars]")
        assert "[truncated" in big["attributes"]["stack"]
        assert "[truncated" in big["attributes"]["nested"]["k"][0]
        bundle = platform.bundles.build_for_project(s, seed.project.id)[0]
        assert bundle.stats["events"] == 2


def test_structured_secrets_and_id_fields_are_redacted_before_storage(
    platform: Platform, seed: Seed, ingestion: IngestionService
) -> None:
    start = _start(seed)
    secrets = {
        "password": "hunter22hunter",
        "card": "4111111111111111",
        "redis": "sup3rs3cret",
        "email": "jane.doe@example.com",
        "ip": "10.1.2.3",
        "version-email": "bob@example.com",
        "aws": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "basic": "dXNlcjpodW50ZXIyMmh1bnRlcg==",
    }
    record = _log(
        start,
        1,
        f'login body={{"user":"bob","password":"{secrets["password"]}"}} '
        f"AWS_SECRET_ACCESS_KEY={secrets['aws']} Authorization: Basic {secrets['basic']}",
        password=secrets["password"],
        card_number=int(secrets["card"]),
        db_url=f"redis://:{secrets['redis']}@cache:6379/0",
        headers={"Authorization": f"Basic {secrets['basic']}", "X-Api-Key": secrets["password"]},
        correlation_id=secrets["email"],
        trace_id=secrets["ip"],
        version=secrets["version-email"],
    )
    write_logs(seed.logs, [record])
    with platform.db.session() as s:
        ingestion.run_connector(s, seed.log_connector.id, end=start + timedelta(hours=6))
        batch = s.scalars(select(RawBatch)).one()
        stored = json.dumps(_rows(platform, batch))
        bundle = platform.bundles.build_for_project(s, seed.project.id)[0]
        sent = json.dumps([bundle.log_templates, bundle.stats])
        for name, value in secrets.items():
            assert value not in stored, f"{name} stored unredacted"
            assert value not in sent, f"{name} in the bundle"
        assert batch.redactions["credential_assignment"] >= 4
        assert batch.redactions["credit_card"] == 1
        assert batch.redactions["email"] == 2


def test_redacted_metric_labels_do_not_merge_series(
    platform: Platform, seed: Seed, ingestion: IngestionService
) -> None:
    start = _start(seed)
    records = []
    for i in range(144):  # 6h baseline + 6h window, one point every 5 minutes
        ts = start - timedelta(hours=6) + timedelta(minutes=5 * i)
        for instance in range(1, 11):
            regressed = ts >= start and instance == 1  # only 10.0.0.1 gets slow
            records.append(
                {
                    "timestamp": ts.isoformat(),
                    "name": "latency_ms",
                    "service": "payments-api",
                    "value": 500.0 if regressed else 100.0 + (i % 2),
                    "labels": {"instance": f"10.0.0.{instance}:9100", "zone": "a"},
                }
            )
    write_logs(seed.metrics, records)
    with platform.db.session() as s:
        ingestion.run_connector(s, seed.metric_connector.id, end=start + timedelta(hours=6))
        batch = s.scalars(select(RawBatch)).one()
        rows = _rows(platform, batch)
        assert "10.0.0." not in json.dumps(rows)  # the address itself is never stored
        assert len({r["labels"][pipeline.LABEL_DIGEST] for r in rows}) == 10
        features = platform.bundles.build_for_project(s, seed.project.id)[0].metric_features
    assert len(features) == 10
    assert all((f["count"], f["baseline_count"]) == (72, 72) for f in features)
    assert [f["anomalous"] for f in features].count(True) == 1
    slow = next(f for f in features if f["anomalous"])
    assert slow["mean"] == 500.0
    assert slow["delta_pct"] > 300

    # Labels that redaction leaves alone get no digest, so their series names are unchanged.
    with platform.db.session() as s:
        conn = s.get(Connector, seed.metric_connector.id)
        assert conn is not None
        point = {"timestamp": utcnow().isoformat(), "service": "x", "name": "n", "value": 1}
        pushed = ingestion.ingest_push(s, conn, [{**point, "labels": {"route": "/pay"}}])
        assert _rows(platform, pushed[0])[0]["labels"] == {"route": "/pay"}


# --------------------------------------------------------------------------- windows


def test_pull_window_ends_a_little_in_the_past(
    platform: Platform, seed: Seed, ingestion: IngestionService
) -> None:
    now = utcnow()
    write_logs(seed.logs, [_log(now, -10, "ten minutes ago"), _log(now, -0.2, "12 seconds ago")])
    with platform.db.session() as s:
        result = ingestion.run_connector(s, seed.log_connector.id)  # as the scheduled job does
        end = datetime.fromisoformat(result["window"][1])
        assert timedelta(seconds=59) <= utcnow() - end <= timedelta(seconds=90)
        assert result["records"] == 1  # the fresh line is left for the next window ...
    # ... which picks it up once the lag has passed (here: lag switched off in the config).
    with platform.db.session() as s:
        conn = s.get(Connector, seed.log_connector.id)
        assert conn is not None
        conn.config = {**conn.config, "lag_seconds": 0}
    with platform.db.session() as s:
        result = ingestion.run_connector(s, seed.log_connector.id)
        assert result["records"] == 1
        messages = [r["message"] for b in s.scalars(select(RawBatch)) for r in _rows(platform, b)]
        assert sorted(messages) == ["12 seconds ago", "ten minutes ago"]


def test_catch_up_reads_one_bounded_window_per_run(
    platform: Platform, seed: Seed, ingestion: IngestionService
) -> None:
    now = utcnow()
    behind = now - timedelta(hours=60)
    with platform.db.session() as s:
        conn = s.get(Connector, seed.log_connector.id)
        assert conn is not None
        conn.cursor = {"last_end": behind.isoformat()}
    write_logs(seed.logs, [_log(behind, 60 * h, f"hour {h}") for h in (1, 23, 25, 47, 49, 59)])
    seen: list[list[str]] = []
    for _ in range(3):
        with platform.db.session() as s:
            result = ingestion.run_connector(s, seed.log_connector.id, end=now)
            window = [datetime.fromisoformat(ts) for ts in result["window"]]
            assert window[1] - window[0] <= pipeline.MAX_WINDOW
            newest = s.scalars(select(RawBatch).order_by(RawBatch.window_start.desc())).first()
            assert newest is not None
            seen.append(sorted(r["message"] for r in _rows(platform, newest)))
    assert seen == [["hour 1", "hour 23"], ["hour 25", "hour 47"], ["hour 49", "hour 59"]]
    with platform.db.session() as s:
        conn = s.get(Connector, seed.log_connector.id)
        assert conn is not None
        assert conn.cursor["last_end"] == now.isoformat()


def test_cursor_follows_a_connector_that_stopped_early(platform: Platform, seed: Seed) -> None:
    """Loki with more entries than its page budget stops mid-window; the cursor must too."""
    start = _start(seed)
    end = start + timedelta(hours=1)
    stopped = start + timedelta(minutes=20)

    def factory(type_name: str, config: dict[str, Any], **_: Any) -> Any:
        return build_connector(
            "loki",
            {"url": "http://10.0.0.5:3100", "query": "{}", "limit": 2},
            http=httpx.Client(transport=httpx.MockTransport(loki)),
        )

    entries = [(start + timedelta(minutes=5 * i), f"line {i}") for i in range(9)]

    def loki(request: httpx.Request) -> httpx.Response:
        lo, hi = int(request.url.params["start"]), int(request.url.params["end"])
        page = [
            [str(int(ts.timestamp()) * 10**9 + ts.microsecond * 1000), line]
            for ts, line in entries
            if lo <= int(ts.timestamp()) * 10**9 + ts.microsecond * 1000 < hi
        ][: int(request.url.params["limit"])]
        result = [{"stream": {"service_name": "payments-api"}, "values": page}]
        return httpx.Response(200, json={"status": "success", "data": {"result": result}})

    service = IngestionService(platform.store, factory)
    messages: list[str] = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("opsmith.connectors.loki.MAX_PAGES", 4)
        with platform.db.session() as s:
            first = service.run_connector(s, seed.log_connector.id, end=end)
            conn = s.get(Connector, seed.log_connector.id)
            assert conn is not None
            assert datetime.fromisoformat(conn.cursor["last_end"]) == stopped
            assert first["window"] == [start.isoformat(), stopped.isoformat()]
            batch = s.scalars(select(RawBatch)).one()
            assert batch.window_end == stopped
            messages += [r["message"] for r in _rows(platform, batch)]
        for _ in range(5):
            with platform.db.session() as s:
                result = service.run_connector(s, seed.log_connector.id, end=end)
                if result.get("skipped") == "empty window":
                    break
                for batch_id in result["batches"]:
                    batch = s.get(RawBatch, batch_id)
                    assert batch is not None
                    messages += [r["message"] for r in _rows(platform, batch)]
    assert messages == [line for _, line in entries]  # each line once, across the runs


# --------------------------------------------------------------------------- bundling


def test_quiet_metric_series_never_evict_the_log_templates(
    platform: Platform, seed: Seed, ingestion: IngestionService
) -> None:
    start = _start(seed)
    write_logs(seed.logs, error_records(start, 50))
    points = [
        {
            "timestamp": (start + timedelta(minutes=5 * j)).isoformat(),
            "name": "http_requests_total",
            "value": 1.0,
            "service": "payments-api",
            "labels": {"route": f"/api/v1/resource{i}", "pod": f"pod-{i % 7}"},
        }
        for i in range(150)
        for j in range(3)
    ]
    write_logs(seed.metrics, points)
    end = start + timedelta(hours=6)
    with platform.db.session() as s:
        ingestion.run_connector(s, seed.log_connector.id, end=end)
        ingestion.run_connector(s, seed.metric_connector.id, end=end)
        bundle = platform.bundles.build_for_project(s, seed.project.id)[0]
        assert bundle.stats["metric_series"] == 150
        assert len(bundle.log_templates) == 1
        assert bundle.log_templates[0]["count"] == 50
        assert 0 < len(bundle.metric_features) < 150
        assert bundle.token_estimate <= platform.bundles.max_tokens + 200


def test_one_unbundleable_service_does_not_block_the_others(
    platform: Platform, settings: Settings, seed: Seed, ingestion: IngestionService, drain: Any
) -> None:
    start = _start(seed)
    write_logs(seed.logs, error_records(start, 3) + error_records(start, 2, service="checkout-api"))
    with platform.db.session() as s:
        ingestion.run_connector(s, seed.log_connector.id, end=start + timedelta(hours=6))
    with platform.db.session() as s:
        broken = s.scalar(select(Service).where(Service.name == "checkout-api"))
        assert broken is not None
        bad = s.scalars(select(RawBatch).where(RawBatch.service_id == broken.id)).one()
        (Path(settings.object_store_dir) / bad.object_key).write_text("{ this is not json\n")
        broken_id, bad_id = broken.id, bad.id

    results = [r for r in drain() if r.kind == "build_bundles"]
    # The failure is loud: the job is retried (and dead-lettered if it keeps failing) ...
    assert [r.status for r in results] == ["retrying"]
    assert broken_id in (results[0].error or "")
    with platform.db.session() as s:
        # ... but the healthy service was bundled and committed all the same.
        bundle = s.scalars(select(AnalysisBundle)).one()
        assert bundle.service_id == seed.service.id
        assert bundle.stats["events"] == 3
        still_pending = s.scalars(select(RawBatch).where(RawBatch.bundle_id.is_(None))).all()
        assert [b.id for b in still_pending] == [bad_id]
        with pytest.raises(BundleBuildError, match=broken_id):
            platform.bundles.build_for_project(s, seed.project.id)


def test_pushed_metrics_take_their_baseline_from_earlier_batches(
    platform: Platform, seed: Seed, ingestion: IngestionService
) -> None:
    t0 = utcnow().replace(microsecond=0) - timedelta(hours=3)

    def push(offset_minutes: int, value: float) -> None:
        records = [
            {
                "timestamp": (t0 + timedelta(minutes=offset_minutes + i)).isoformat(),
                "service": "payments-api",
                "name": "latency_ms",
                "value": value + (i % 2),
            }
            for i in range(30)
        ]
        with platform.db.session() as s:
            conn = s.get(Connector, seed.metric_connector.id)
            assert conn is not None
            ingestion.ingest_push(s, conn, records)
            platform.bundles.build_for_project(s, seed.project.id)

    push(0, 100)
    push(40, 100)
    push(90, 450)
    with platform.db.session() as s:
        bundles = s.scalars(select(AnalysisBundle).order_by(AnalysisBundle.window_start)).all()
        first, second, third = (b.metric_features[0] for b in bundles)
    assert (first["baseline_count"], first["anomalous"]) == (0, False)
    assert (second["baseline_count"], second["anomalous"]) == (30, False)
    assert third["baseline_count"] == 60
    assert third["count"] == 30
    assert third["anomalous"]
    assert third["delta_pct"] > 300


# --------------------------------------------------------------------------- overlapping jobs


def test_two_builders_never_bundle_the_same_batches(
    platform: Platform, seed: Seed, ingestion: IngestionService
) -> None:
    start = _start(seed)
    write_logs(seed.logs, error_records(start, 5))
    with platform.db.session() as s:
        ingestion.run_connector(s, seed.log_connector.id, end=start + timedelta(hours=6))

    class RacingStore:
        """While this builder reads the objects, another one bundles the same batches."""

        def __init__(self) -> None:
            self.raced = False

        def iter_jsonl(self, key: str) -> Any:
            if not self.raced:
                self.raced = True
                with platform.db.session() as other:
                    assert (
                        len(BundleBuilder(platform.store).build_for_project(other, seed.project.id))
                        == 1
                    )
            return platform.store.iter_jsonl(key)

    with platform.db.session() as s:
        late = BundleBuilder(RacingStore()).build_for_project(s, seed.project.id)  # type: ignore[arg-type]
        assert late == []  # it lost the claim and kept nothing
    with platform.db.session() as s:
        bundle = s.scalars(select(AnalysisBundle)).one()
        assert all(b.bundle_id == bundle.id for b in s.scalars(select(RawBatch)))


def test_overlapping_runs_of_one_connector_store_the_window_once(
    platform: Platform, seed: Seed, ingestion: IngestionService
) -> None:
    start = _start(seed)
    end = start + timedelta(hours=6)
    write_logs(seed.logs, error_records(start, 5))
    slow = platform.db.new_session()
    held = slow.get(Connector, seed.log_connector.id)  # the slow run has read the cursor ...
    assert held is not None
    assert held.cursor["last_end"] == start.isoformat()
    with platform.db.session() as fast:  # ... and a second run finishes first
        assert ingestion.run_connector(fast, seed.log_connector.id, end=end)["records"] == 5
    with pytest.raises(queue.RetryLater, match="concurrent run"):
        ingestion.run_connector(slow, seed.log_connector.id, end=end + timedelta(seconds=5))
    slow.rollback()
    slow.close()
    with platform.db.session() as s:
        assert [b.record_count for b in s.scalars(select(RawBatch))] == [5]
        conn = s.get(Connector, seed.log_connector.id)
        assert conn is not None
        assert conn.cursor["last_end"] == end.isoformat()
        # The retried run then starts from the advanced cursor and finds nothing new.
        again = ingestion.run_connector(s, seed.log_connector.id, end=end + timedelta(seconds=5))
        assert again["records"] == 0


def test_objects_of_a_rolled_back_ingest_are_removed(
    platform: Platform, settings: Settings, seed: Seed, ingestion: IngestionService
) -> None:
    store = platform.store
    if not hasattr(store, "delete"):  # the store grows `delete`; the hook must use it
        store.delete = lambda key: store._path(key).unlink(missing_ok=True)  # type: ignore[attr-defined]
    start = _start(seed)
    write_logs(seed.logs, error_records(start, 2))

    def objects() -> list[Path]:
        return list(Path(settings.object_store_dir).rglob("*.jsonl"))

    session = platform.db.new_session()
    ingestion.run_connector(session, seed.log_connector.id, end=start + timedelta(hours=6))
    assert len(objects()) == 1
    session.rollback()  # the job failed after the object was written
    session.close()
    assert objects() == []
    with platform.db.session() as s:
        assert not s.scalars(select(RawBatch)).all()

    with (
        platform.db.session() as s
    ):  # a committed ingest keeps its object, also on a later rollback
        ingestion.run_connector(s, seed.log_connector.id, end=start + timedelta(hours=6))
        s.commit()
        s.rollback()
    assert len(objects()) == 1


def test_push_to_a_disabled_connector_is_refused(
    platform: Platform, seed: Seed, ingestion: IngestionService
) -> None:
    with platform.db.session() as s:
        conn = s.get(Connector, seed.log_connector.id)
        assert conn is not None
        conn.enabled = False
        record = {"timestamp": utcnow().isoformat(), "service": "a", "message": "x"}
        with pytest.raises(ConnectorDisabledError, match="disabled"):
            ingestion.ingest_push(s, conn, [record])
        assert not s.scalars(select(RawBatch)).all()
