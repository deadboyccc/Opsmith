"""The Loki and Prometheus connectors against real servers.

The regular suite tests the connectors against mocked HTTP. These tests run them against a
real Loki and a real Prometheus, so the published compatibility matrix
(docs/guide/compatibility.md) rests on evidence. They are skipped unless the server URLs are set;
the ``connectors`` job in CI runs them against every version in the matrix:

    OPSMITH_TEST_LOKI_URL=http://127.0.0.1:3100 \\
    OPSMITH_TEST_PROMETHEUS_URL=http://127.0.0.1:9090 \\
        uv run pytest tests/test_connectors_live.py -v
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from opsmith.connectors import ConnectorError, build_connector

LOKI_URL = os.environ.get("OPSMITH_TEST_LOKI_URL", "").rstrip("/")
PROMETHEUS_URL = os.environ.get("OPSMITH_TEST_PROMETHEUS_URL", "").rstrip("/")

live_loki = pytest.mark.skipif(not LOKI_URL, reason="set OPSMITH_TEST_LOKI_URL to run")
live_prometheus = pytest.mark.skipif(
    not PROMETHEUS_URL, reason="set OPSMITH_TEST_PROMETHEUS_URL to run"
)


def _ns(moment: datetime) -> str:
    return str(int(moment.timestamp()) * 1_000_000_000 + moment.microsecond * 1000)


def _push_to_loki(streams: list[dict[str, object]]) -> None:
    response = httpx.post(f"{LOKI_URL}/loki/api/v1/push", json={"streams": streams}, timeout=30)
    assert response.status_code == 204, response.text


def _wait_for_loki(query: str, start: datetime, end: datetime, expected: int) -> None:
    """Pushed entries become queryable shortly after the push is acknowledged."""
    deadline = time.monotonic() + 60
    while True:
        response = httpx.get(
            f"{LOKI_URL}/loki/api/v1/query_range",
            params={"query": query, "start": _ns(start), "end": _ns(end), "limit": "1000"},
            timeout=30,
        )
        found = sum(len(s["values"]) for s in response.json()["data"]["result"])
        if found >= expected or time.monotonic() > deadline:
            assert found == expected, f"loki has {found} of {expected} pushed entries"
            return
        time.sleep(1)


@live_loki
def test_loki_reads_every_entry_once_across_pages() -> None:
    """Two services, JSON and plain lines, entries sharing a nanosecond, and a page size small
    enough that the window is read in several pages."""
    run = uuid.uuid4().hex[:12]  # isolates this run's entries from anything else in Loki
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
    at = [base + timedelta(seconds=i) for i in range(6)]
    api = {"service_name": "payments-api", "env": "production", "run": run}
    worker = {"service_name": "payments-worker", "env": "staging", "run": run}
    _push_to_loki(
        [
            {
                "stream": api,
                "values": [
                    [
                        _ns(at[0]),
                        json.dumps(
                            {"level": "error", "message": "upstream timeout", "trace_id": "t-1"}
                        ),
                    ],
                    [_ns(at[1]), json.dumps({"level": "warn", "msg": "slow query 900ms"})],
                    # Two different lines on the same nanosecond, at a page boundary.
                    [_ns(at[2]), "plain line A"],
                    [_ns(at[2]), "plain line B"],
                    [_ns(at[4]), "plain line C"],
                ],
            },
            {
                "stream": worker,
                "values": [
                    [_ns(at[3]), json.dumps({"level": "info", "message": "job done"})],
                    [_ns(at[5]), "worker stopped"],
                ],
            },
        ]
    )
    query = f'{{run="{run}"}}'
    start, end = base - timedelta(seconds=1), base + timedelta(minutes=1)
    _wait_for_loki(query, start, end, expected=7)

    connector = build_connector(
        "loki", {"url": LOKI_URL, "query": query, "limit": 3}, default_service="fallback"
    )
    result = connector.fetch(start, end, {})

    assert result.skipped == 0
    assert result.cursor == {"last_end": end.isoformat()}
    got = sorted((e.timestamp, e.message) for e in result.logs)
    assert got == sorted(
        [
            (at[0], "upstream timeout"),
            (at[1], "slow query 900ms"),
            (at[2], "plain line A"),
            (at[2], "plain line B"),
            (at[3], "job done"),
            (at[4], "plain line C"),
            (at[5], "worker stopped"),
        ]
    )
    by_message = {e.message: e for e in result.logs}
    first = by_message["upstream timeout"]
    assert (first.service, first.environment, first.severity) == (
        "payments-api",
        "production",
        "error",
    )
    assert first.trace_id == "t-1"
    assert by_message["slow query 900ms"].severity == "warn"
    job = by_message["job done"]
    assert (job.service, job.environment, job.severity) == ("payments-worker", "staging", "info")
    assert by_message["plain line C"].attributes["run"] == run


@live_loki
def test_loki_window_bounds_are_respected() -> None:
    run = uuid.uuid4().hex[:12]
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)
    _push_to_loki(
        [
            {
                "stream": {"service_name": "api", "run": run},
                "values": [
                    [_ns(base), "before the window"],
                    [_ns(base + timedelta(seconds=10)), "inside the window"],
                    [_ns(base + timedelta(seconds=20)), "at the end, excluded"],
                ],
            }
        ]
    )
    query = f'{{run="{run}"}}'
    _wait_for_loki(query, base, base + timedelta(minutes=1), expected=3)
    connector = build_connector("loki", {"url": LOKI_URL, "query": query})
    result = connector.fetch(base + timedelta(seconds=1), base + timedelta(seconds=20), {})
    assert [e.message for e in result.logs] == ["inside the window"]


@live_loki
def test_loki_reports_an_invalid_query() -> None:
    connector = build_connector("loki", {"url": LOKI_URL, "query": "{this is not logql"})
    now = datetime.now(UTC)
    with pytest.raises(ConnectorError):
        connector.fetch(now - timedelta(minutes=5), now, {})


def _wait_for_prometheus_scrapes() -> None:
    """A fresh Prometheus needs a few scrapes of itself before there is a range to read."""
    deadline = time.monotonic() + 90
    while True:
        response = httpx.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": 'count_over_time(up{job="prometheus"}[5m])'},
            timeout=30,
        )
        result = response.json()["data"]["result"]
        if (result and float(result[0]["value"][1]) >= 2) or time.monotonic() > deadline:
            assert result, "prometheus has not scraped itself"
            return
        time.sleep(2)


@live_prometheus
def test_prometheus_reads_scraped_series_with_their_labels() -> None:
    _wait_for_prometheus_scrapes()
    end = datetime.now(UTC)
    start = end - timedelta(minutes=2)
    connector = build_connector(
        "prometheus",
        {
            "url": PROMETHEUS_URL,
            "queries": [{"name": "up", "promql": 'up{job="prometheus"}'}],
            "step": "5s",
            "baseline_seconds": 60,
            "service_label": "job",
        },
        default_service="fallback",
    )
    result = connector.fetch(start, end, {})
    assert result.skipped == 0
    assert result.cursor == {"last_end": end.isoformat()}
    assert result.metrics, "no points for up{job='prometheus'}"
    assert {p.service for p in result.metrics} == {"prometheus"}
    assert {p.name for p in result.metrics} == {"up"}
    assert {p.value for p in result.metrics} == {1.0}
    assert all("__name__" not in p.labels and "instance" in p.labels for p in result.metrics)
    assert all(start - timedelta(seconds=60) <= p.timestamp <= end for p in result.metrics)


@live_prometheus
def test_prometheus_steps_baseline_and_missing_values() -> None:
    """A constant has a value at every step, including the baseline before the window. A
    query with no value (NaN) at any step yields no points rather than an error."""
    end = datetime.now(UTC).replace(microsecond=0)
    start = end - timedelta(minutes=5)
    connector = build_connector(
        "prometheus",
        {
            "url": PROMETHEUS_URL,
            "queries": [
                {"name": "constant", "promql": "vector(7)"},
                {"name": "nothing", "promql": "vector(0) / 0"},
            ],
            "step": "60s",
            "baseline_seconds": 600,
        },
        default_service="checkout",
    )
    result = connector.fetch(start, end, {})
    constant = sorted(p.timestamp for p in result.metrics if p.name == "constant")
    # 10 minutes of baseline plus the 5-minute window, one point a minute, both ends included.
    assert len(constant) == 16
    assert constant[0] == start - timedelta(minutes=10)
    assert constant[-1] == end
    assert all(
        p.value == 7.0 and p.service == "checkout" for p in result.metrics if p.name == "constant"
    )
    assert [p for p in result.metrics if p.name == "nothing"] == []


@live_prometheus
def test_prometheus_reports_an_invalid_query() -> None:
    connector = build_connector(
        "prometheus",
        {"url": PROMETHEUS_URL, "queries": [{"name": "bad", "promql": "rate(up[5m]"}]},
    )
    now = datetime.now(UTC)
    with pytest.raises(ConnectorError):
        connector.fetch(now - timedelta(minutes=5), now, {})
