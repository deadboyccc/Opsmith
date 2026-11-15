"""Grafana Loki connector (``/loki/api/v1/query_range``)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx

from opsmith.connectors.base import ConnectorError, FetchResult, LogConnector, register
from opsmith.schemas import LogEvent

_LEVEL_LABELS = ("level", "severity", "detected_level", "lvl")


@register("loki")
class LokiConnector(LogConnector):
    """config: ``url``, ``query`` (LogQL), optional ``limit`` (default 5000),
    ``service_label`` (default ``service_name``)."""

    def fetch(self, start: datetime, end: datetime, cursor: dict[str, Any]) -> FetchResult:
        params = {
            "query": self.config["query"],
            "start": str(int(start.timestamp() * 1e9)),
            "end": str(int(end.timestamp() * 1e9)),
            "limit": str(self.config.get("limit", 5000)),
            "direction": "forward",
        }
        url = self.config["url"].rstrip("/") + "/loki/api/v1/query_range"
        try:
            resp = self.http.get(url, params=params)
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ConnectorError(f"loki query failed: {exc}") from exc
        if body.get("status") != "success":
            raise ConnectorError(f"loki returned status {body.get('status')!r}")
        service_label = self.config.get("service_label", "service_name")
        events: list[LogEvent] = []
        for stream in body.get("data", {}).get("result", []):
            labels: dict[str, str] = stream.get("stream", {})
            severity = next((labels[k] for k in _LEVEL_LABELS if k in labels), None)
            for ts_ns, line in stream.get("values", []):
                message, attrs, line_sev = _parse_line(line)
                events.append(
                    LogEvent(
                        timestamp=datetime.fromtimestamp(int(ts_ns) / 1e9, tz=UTC),
                        service=labels.get(service_label) or self.default_service,
                        environment=labels.get("env", labels.get("environment", "production")),
                        severity=line_sev or severity or "info",
                        message=message,
                        attributes={**labels, **attrs},
                        trace_id=attrs.get("trace_id") or labels.get("trace_id"),
                    )
                )
        return FetchResult(logs=events, cursor={"last_end": end.isoformat()})


def _parse_line(line: str) -> tuple[str, dict[str, Any], str | None]:
    """Loki lines are often JSON; unwrap the message and level when they are."""
    if line.startswith("{"):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return line, {}, None
        if isinstance(data, dict):
            message = str(data.pop("message", data.pop("msg", line)))
            level = data.pop("level", data.pop("severity", None))
            return message, data, str(level) if level else None
    return line, {}, None
