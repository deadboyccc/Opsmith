from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from opsmith.connectors import ConnectorError, available_types, build_connector
from opsmith.db import utcnow
from opsmith.localfs import LocalAccessError
from opsmith.models import Connector

START = datetime(2026, 3, 1, tzinfo=UTC)
END = START + timedelta(hours=1)


def fake_dns(host: str) -> list[str]:
    """Connector hosts are resolved before use; tests must not touch real DNS."""
    return ["10.0.0.5"]


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
    res = build_connector("file_logs", {"path": str(jl)}, local_root=tmp_path).fetch(START, END, {})
    assert [e.message for e in res.logs] == ["boom"]
    assert res.logs[0].trace_id == "t1"
    assert res.logs[0].attributes == {"user": "u1"}

    txt = tmp_path / "b.log"
    txt.write_text("2026-03-01T00:20:00+00:00 WARN [billing] slow query 900ms\ngarbage line\n")
    res = build_connector(
        "file_logs", {"path": str(txt), "format": "text"}, default_service="x", local_root=tmp_path
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
        "file_metrics",
        {"path": str(f), "baseline_seconds": 7200},
        default_service="api",
        local_root=tmp_path,
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
        resolver=fake_dns,
    )
    res = conn.fetch(START, END, {})
    assert seen["params"]["query"] == '{app="api"}'
    assert [e.message for e in res.logs] == ["db down", "plain line"]
    assert res.logs[0].trace_id == "abc"
    assert res.logs[0].severity == "error"


def test_loki_errors_raise_connector_error() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    conn = build_connector(
        "loki", {"url": "http://loki", "query": "{}"}, http=client, resolver=fake_dns
    )
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
        resolver=fake_dns,
    )
    res = conn.fetch(START, END, {})
    assert len(res.metrics) == 1
    assert res.metrics[0].labels == {"service": "api", "route": "/pay"}
    assert res.metrics[0].name == "latency_p95"


# --------------------------------------------------------------------------- file confinement


def test_file_connectors_only_read_inside_the_local_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    data = root / "data"
    outside = tmp_path / "outside"
    for d in (root, data, outside):
        d.mkdir()
    line = '{"timestamp": "2026-03-01T00:10:00Z", "message": "m", "name": "n", "value": 1}\n'
    (root / "ok.jsonl").write_text(line)
    (data / "stored.jsonl").write_text(line)
    (outside / "secret.jsonl").write_text(line)
    (root / "link.jsonl").symlink_to(outside / "secret.jsonl")

    def fetch(kind: str, path: Path | str, **kw: object) -> int:
        conn = build_connector(kind, {"path": str(path)}, **kw)  # type: ignore[arg-type]
        res = conn.fetch(START, END, {})
        return len(res.logs) + len(res.metrics)

    for kind in ("file_logs", "file_metrics"):
        assert fetch(kind, root / "ok.jsonl", local_root=root, local_deny=(data,)) == 1
        refused = {
            "outside the root": outside / "secret.jsonl",
            "glob outside the root, matching nothing": outside / "nothing-*.jsonl",
            "parent traversal": root / ".." / "outside" / "*.jsonl",
            "the platform data directory": data / "*.jsonl",
            "a glob that also reaches the data directory": root / "**" / "stored.jsonl",
            "a symlink that leaves the root": root / "link.jsonl",
        }
        for path in refused.values():
            with pytest.raises(LocalAccessError):
                fetch(kind, path, local_root=root, local_deny=(data,))
        with pytest.raises(LocalAccessError, match="disabled"):  # no root configured
            fetch(kind, root / "ok.jsonl")


# --------------------------------------------------------------------------- malformed records


def test_file_logs_skip_and_count_bad_records(tmp_path: Path) -> None:
    f = tmp_path / "a.jsonl"
    good = '{"timestamp": "2026-03-01T00:10:00Z", "message": "good %d"}\n'
    f.write_bytes(
        (
            good % 1 + '{"timestamp": "2026-03-01T00:11:00Z", "message": "ids", "version": 3, '
            '"trace_id": 12345, "correlation_id": 7}\n'
            + '{"timestamp": "Mar  1 00:10:00", "message": "syslog-style timestamp"}\n'
            + '{"timestamp": null, "message": "null timestamp"}\n'
            + '{"message": "no timestamp at all"}\n'
            + '{"timestamp": 1e30, "message": "absurd epoch"}\n'
            + '{"timestamp": "2026-03-01T00:12:00Z", "message": "m", "trace_id": {"a": 1}}\n'
            + "not json\n"
            + '["a", "list"]\n'
            + '{"timestamp": 1772323920, "message": "epoch seconds"}\n'
            + '{"timestamp": 1772323920000000000, "message": "epoch nanoseconds"}\n'
        ).encode()
        + b'{"timestamp": "2026-03-01T00:13:00Z", "message": "bad utf8 \xff byte"}\n'
        + (good % 2).encode()
    )
    res = build_connector("file_logs", {"path": str(f)}, local_root=tmp_path).fetch(START, END, {})
    messages = [e.message for e in res.logs]
    assert messages[0] == "good 1"
    assert messages[-1] == "good 2"
    assert {"ids", "epoch seconds", "epoch nanoseconds"} <= set(messages)
    assert any(m.startswith("bad utf8") for m in messages)
    ids = next(e for e in res.logs if e.message == "ids")
    assert (ids.version, ids.trace_id, ids.correlation_id) == ("3", "12345", "7")
    assert res.skipped == 7
    assert not any("timestamp" in m for m in messages)  # undated records are not stamped "now"
    assert res.cursor["last_end"] == END.isoformat()


def test_file_metrics_skip_and_count_bad_records(tmp_path: Path) -> None:
    f = tmp_path / "m.jsonl"
    f.write_bytes(
        b'{"timestamp": "2026-03-01T00:10:00Z", "name": "lat", "value": 1}\n'
        b'{"timestamp": "2026-03-01T00:10:00Z", "name": "lat", "value": 1, "labels": null}\n'
        b'{"timestamp": "2026-03-01T00:10:00Z", "name": "lat", "value": 1, "labels": ["a"]}\n'
        b'{"timestamp": "2026-03-01T00:10:00Z", "value": 1}\n'
        b'{"timestamp": 1e30, "name": "lat", "value": 1}\n'
        b'{"timestamp": "2026-03-01T00:10:00Z", "name": "lat", "value": "high"}\n'
        b"[1, 2]\n"
        b'{"timestamp": "2026-03-01T00:11:00Z", "name": "lat\xff", "value": 2}\n'
    )
    res = build_connector(
        "file_metrics", {"path": str(f), "environment": "staging"}, local_root=tmp_path
    ).fetch(START, END, {})
    assert [p.value for p in res.metrics] == [1.0, 1.0, 2.0]  # null labels mean no labels
    assert res.skipped == 5
    assert {p.environment for p in res.metrics} == {"staging"}  # connector-level environment


def test_file_logs_pattern_is_checked_when_the_connector_is_built(tmp_path: Path) -> None:
    for pattern in (r"^(?P<message>.*)$", "("):
        with pytest.raises(ConnectorError, match="pattern"):
            build_connector(
                "file_logs", {"path": str(tmp_path / "x.log"), "format": "text", "pattern": pattern}
            )


# --------------------------------------------------------------------------- HTTP hardening


def _client(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"a request was sent to {request.url}")


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "ftp://10.0.0.5",
        "file:///etc/passwd",
        "http://user:pw@10.0.0.5",
        "http://10.0.0.5/?x=",  # a query would swallow the API path appended to the base
        "http://10.0.0.5/v1/kv/secrets#",
        "http://10.0.0.5 /x",
        "http://10.0.0.5\\@evil.example",
        "http://10.0.0.5:99999",
        "http://169.254.169.254",  # cloud metadata
        "http://169.254.169.254/latest/meta-data",
        "http://[fe80::1]:3100",
        "http://[::ffff:169.254.169.254]",
        "http://[fd00:ec2::254]",
    ],
)
@pytest.mark.parametrize("kind", ["loki", "prometheus"])
def test_connector_url_is_validated_before_anything_is_sent(kind: str, url: object) -> None:
    config = {"url": url, "query": "{}", "queries": [{"name": "n", "promql": "up"}]}
    with pytest.raises(ConnectorError):
        build_connector(kind, config, http=_client(_unreachable), resolver=fake_dns)


def test_connector_host_is_resolved_and_link_local_refused() -> None:
    answers = iter([["10.0.0.5"], ["10.0.0.5", "169.254.169.254"]])
    config = {"url": "http://loki.internal:3100/base/", "query": "{}"}
    conn = build_connector(
        "loki", config, http=_client(_unreachable), resolver=lambda host: next(answers)
    )
    # The name is resolved again before each fetch: it now also points at the metadata address.
    with pytest.raises(ConnectorError, match="link-local or metadata"):
        conn.fetch(START, END, {})
    for answer in (["169.254.10.10"], ["fe80::1%eth0"], [], ["not-an-address"]):
        with pytest.raises(ConnectorError):
            build_connector("loki", config, resolver=lambda host, a=answer: a)
    # Loopback and private ranges stay reachable (self-hosted Loki), with a base path.
    ok = build_connector("loki", {"url": "http://127.0.0.1:3100/base/", "query": "{}"})
    assert ok.base_url() == "http://127.0.0.1:3100/base"


def test_connector_timeout_is_clamped() -> None:
    def build(timeout: object) -> object:
        return build_connector(
            "loki", {"url": "http://10.0.0.5", "query": "{}", "timeout": timeout}
        )

    assert build(86_400).timeout == 120  # type: ignore[attr-defined]
    assert build(86_400).http.timeout.read == 120  # type: ignore[attr-defined]
    assert build(5).timeout == 5  # type: ignore[attr-defined]
    for bad in (0, -1, "soon", None, float("inf")):
        with pytest.raises(ConnectorError, match="timeout"):
            _ = build(bad).timeout  # type: ignore[attr-defined]


@pytest.mark.parametrize("kind", ["loki", "prometheus"])
def test_connector_errors_never_echo_the_upstream_body(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = {
        "url": "http://10.0.0.5",
        "query": "{}",
        "queries": [{"name": "q", "promql": "up"}],
    }
    secret = "INTERNAL-SERVICE-SECRET"
    responses = {
        "an error status": httpx.Response(500, text=secret),
        "a redirect": httpx.Response(302, headers={"location": "http://169.254.169.254/"}),
        "an upstream error message": httpx.Response(200, json={"status": "error", "error": secret}),
        "a body that is not JSON": httpx.Response(200, text=secret),
        "a JSON list": httpx.Response(200, json=[secret]),
        "a JSON string": httpx.Response(200, json=secret),
        "no result list": httpx.Response(200, json={"status": "success", "data": secret}),
    }
    for why, response in responses.items():
        conn = build_connector(kind, config, http=_client(lambda r, resp=response: resp))
        with pytest.raises(ConnectorError) as err:
            conn.fetch(START, END, {})
        assert secret not in str(err.value), why
    failing = build_connector(kind, config, http=_client(lambda r: httpx.Response(500)))
    with pytest.raises(ConnectorError, match="HTTP 500"):
        failing.fetch(START, END, {})

    monkeypatch.setattr("opsmith.connectors.base.MAX_RESPONSE_BYTES", 2_000)
    huge = httpx.Response(200, json={"status": "success", "data": {"result": ["x" * 5_000]}})
    with pytest.raises(ConnectorError, match="larger than 2000 bytes"):
        build_connector(kind, config, http=_client(lambda r: huge)).fetch(START, END, {})


# --------------------------------------------------------------------------- Loki paging


def _loki(entries: list[tuple[int, str]], **config: object) -> tuple[object, list[dict[str, str]]]:
    """A Loki that honours start/end/limit over ``entries`` (ns timestamp, line)."""
    requests: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        requests.append(params)
        lo, hi, limit = int(params["start"]), int(params["end"]), int(params["limit"])
        page = [[str(ts), line] for ts, line in entries if lo <= ts < hi][:limit]
        result = [{"stream": {"service_name": "api"}, "values": page}]
        return httpx.Response(200, json={"status": "success", "data": {"result": result}})

    conn = build_connector(
        "loki", {"url": "http://10.0.0.5:3100", "query": "{}", **config}, http=_client(handler)
    )
    return conn, requests


def test_loki_reads_every_page_of_a_window() -> None:
    t0 = int(START.timestamp()) * 10**9
    # 23 entries; several share one nanosecond exactly where a page of 5 ends.
    stamps = [t0 + i * 10**9 for i in range(4)] + [t0 + 4 * 10**9] * 4
    stamps += [t0 + i * 10**9 for i in range(5, 20)]
    entries = [(ts, f"line {i}") for i, ts in enumerate(stamps)]
    conn, requests = _loki(entries, limit=5)
    res = conn.fetch(START, END, {})  # type: ignore[attr-defined]
    assert sorted(e.message for e in res.logs) == sorted(line for _, line in entries)
    assert res.cursor["last_end"] == END.isoformat()
    assert len(requests) > 4

    conn, requests = _loki(entries)  # default page size: one request is enough
    assert len(conn.fetch(START, END, {}).logs) == 23
    assert len(requests) == 1


def test_loki_cursor_stops_where_reading_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("opsmith.connectors.loki.MAX_PAGES", 2)
    t0 = int(START.timestamp()) * 10**9
    entries = [(t0 + i * 10**9 + 123, f"line {i}") for i in range(30)]
    conn, _ = _loki(entries, limit=5)
    seen: list[str] = []
    start = START
    for _ in range(20):
        res = conn.fetch(start, END, {})  # type: ignore[attr-defined]
        seen += [e.message for e in res.logs]
        reached = datetime.fromisoformat(res.cursor["last_end"])
        assert start < reached <= END
        if reached == END:
            break
        assert len(res.logs) < 10  # the window was not finished, and the cursor says so
        start = reached
    # Every entry exactly once across the runs: nothing dropped, nothing read twice.
    assert seen == [line for _, line in entries]

    # More entries on one timestamp than a page holds cannot be paged past (Loki pages by
    # time only): the fetch fails rather than silently dropping them.
    conn, _ = _loki([(t0, f"same instant {i}") for i in range(30)], limit=5)
    with pytest.raises(ConnectorError, match="share one timestamp"):
        conn.fetch(START, END, {})  # type: ignore[attr-defined]
    # ... and so does a page budget that ends inside a single microsecond.
    conn, _ = _loki([(t0 + i, f"same microsecond {i}") for i in range(30)], limit=5)
    with pytest.raises(ConnectorError, match="without advancing"):
        conn.fetch(START, END, {})  # type: ignore[attr-defined]


def test_loki_skips_entries_it_cannot_parse() -> None:
    t0 = int(START.timestamp()) * 10**9
    body = {
        "status": "success",
        "data": {
            "result": [
                {
                    "stream": {"service_name": "api"},
                    "values": [
                        [str(t0), '{"msg": "numeric trace", "trace_id": 12345, "level": 50}'],
                        [str(t0), '{"msg": "nested trace", "trace_id": {"a": 1}}'],
                        ["not-a-number", "bad timestamp"],
                        [str(t0)],
                        [str(t0), "plain"],
                    ],
                },
                "not a stream",
            ]
        },
    }
    conn = build_connector(
        "loki",
        {"url": "http://10.0.0.5", "query": "{}"},
        http=_client(lambda r: httpx.Response(200, json=body)),
    )
    res = conn.fetch(START, END, {})
    assert [e.message for e in res.logs] == ["numeric trace", "plain"]
    assert res.logs[0].trace_id == "12345"
    assert res.logs[0].severity == "50"
    assert res.skipped == 3


def test_prometheus_skips_bad_samples_and_keeps_infinity_for_the_pipeline() -> None:
    body = {
        "status": "success",
        "data": {
            "result": [
                {
                    "metric": {"service": "api"},
                    "values": [[START.timestamp(), "1"], [START.timestamp(), "+Inf"], ["x", "1"]],
                },
                {"metric": None, "values": [[START.timestamp(), "oops"], [START.timestamp()]]},
            ]
        },
    }
    conn = build_connector(
        "prometheus",
        {"url": "http://10.0.0.5", "queries": [{"name": "ratio", "promql": "a/b"}]},
        http=_client(lambda r: httpx.Response(200, json=body)),
    )
    res = conn.fetch(START, END, {})
    assert [p.value for p in res.metrics] == [1.0, float("inf")]  # dropped in store_signals
    assert res.skipped == 3
    with pytest.raises(ConnectorError, match="queries"):
        build_connector(
            "prometheus",
            {"url": "http://10.0.0.5", "queries": [{"name": "no promql"}]},
            http=_client(_unreachable),
        ).fetch(START, END, {})


def test_push_only_connectors_accept_pushes_and_poll_nothing(platform, seed, drain) -> None:
    """A collector needs a connector to push to. The push types need no host files and no
    URL, and their scheduled poll is a harmless no-op (not a failing job)."""
    assert available_types()["push_logs"] == "logs"
    assert available_types()["push_metrics"] == "metrics"
    with platform.db.session() as s:
        conn = Connector(
            tenant_id=seed.tenant.id,
            project_id=seed.project.id,
            name="collector",
            type="push_logs",
            kind="logs",
            service_id=seed.service.id,
        )
        s.add(conn)
        s.flush()
        connector_id = conn.id
        polled = platform.ingestion.run_connector(s, connector_id)
        assert polled["records"] == 0
        record = {"timestamp": utcnow().isoformat(), "service": "payments-api", "message": "hi"}
        batches = platform.ingestion.ingest_push(s, s.get(Connector, connector_id), [record])
        assert sum(b.record_count for b in batches) == 1
