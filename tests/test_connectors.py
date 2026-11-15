from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from opsmith.connectors import ConnectorError, available_types, build_connector

START = datetime(2026, 3, 1, tzinfo=UTC)
END = START + timedelta(hours=1)


def test_registry_lists_builtin_types() -> None:
    types = available_types()
    assert types["file_logs"] == "logs"
    assert types["loki"] == "logs"
    assert types["prometheus"] == "metrics"
    with pytest.raises(ConnectorError):
        build_connector("nope", {})


def test_file_logs_jsonl_and_text(tmp_path: Path) -> None:
    jl = tmp_path / "a.jsonl"
    jl.write_text(
        '{"timestamp": "2026-03-01T00:10:00Z", "level": "ERROR", "message": "boom", '
        '"service": "api", "trace_id": "t1", "user": "u1"}\n'
        "not json\n"
        '{"timestamp": "2026-03-01T05:00:00Z", "level": "info", "message": "outside"}\n'
    )
    res = build_connector("file_logs", {"path": str(jl)}).fetch(START, END, {})
    assert [e.message for e in res.logs] == ["boom"]
    assert res.logs[0].trace_id == "t1"
    assert res.logs[0].attributes == {"user": "u1"}

    txt = tmp_path / "b.log"
    txt.write_text("2026-03-01T00:20:00+00:00 WARN [billing] slow query 900ms\ngarbage line\n")
    res = build_connector(
        "file_logs", {"path": str(txt), "format": "text"}, default_service="x"
    ).fetch(START, END, {})
    assert res.logs[0].service == "billing"
    assert res.logs[0].severity == "WARN"
    assert res.cursor["last_end"] == END.isoformat()


def test_file_metrics_include_baseline(tmp_path: Path) -> None:
    f = tmp_path / "m.jsonl"
    f.write_text(
        '{"timestamp": "2026-02-28T23:00:00Z", "name": "lat", "value": 1}\n'
        '{"timestamp": "2026-03-01T00:30:00Z", "name": "lat", "value": "2", "labels": {"r": 1}}\n'
        '{"timestamp": "2026-02-28T10:00:00Z", "name": "lat", "value": 9}\n'
    )
    res = build_connector(
        "file_metrics", {"path": str(f), "baseline_seconds": 7200}, default_service="api"
    ).fetch(START, END, {})
    assert sorted(p.value for p in res.metrics) == [1.0, 2.0]
    assert res.metrics[1].labels == {"r": "1"}


def test_loki_connector_parses_streams() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        seen["auth"] = request.headers.get("authorization")
        ts = str(int((START + timedelta(minutes=5)).timestamp() * 1e9))
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [
                        {
                            "stream": {"service_name": "api", "level": "error"},
                            "values": [
                                [ts, '{"message": "db down", "trace_id": "abc"}'],
                                [ts, "plain line"],
                            ],
                        },
                    ]
                },
            },
        )

    conn = build_connector(
        "loki",
        {"url": "http://loki:3100", "query": '{app="api"}'},
        credential="tok",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    res = conn.fetch(START, END, {})
    assert seen["params"]["query"] == '{app="api"}'
    assert [e.message for e in res.logs] == ["db down", "plain line"]
    assert res.logs[0].trace_id == "abc"
    assert res.logs[0].severity == "error"


def test_loki_errors_raise_connector_error() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    conn = build_connector("loki", {"url": "http://loki", "query": "{}"}, http=client)
    with pytest.raises(ConnectorError):
        conn.fetch(START, END, {})


def test_prometheus_connector() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/query_range"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [
                        {
                            "metric": {"__name__": "x", "service": "api", "route": "/pay"},
                            "values": [[START.timestamp(), "0.5"], [START.timestamp() + 60, "NaN"]],
                        },
                    ]
                },
            },
        )

    conn = build_connector(
        "prometheus",
        {
            "url": "http://prom:9090",
            "queries": [{"name": "latency_p95", "promql": "histogram_quantile(...)"}],
        },
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    res = conn.fetch(START, END, {})
    assert len(res.metrics) == 1
    assert res.metrics[0].labels == {"service": "api", "route": "/pay"}
    assert res.metrics[0].name == "latency_p95"
