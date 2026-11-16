"""Grafana Loki connector (``/loki/api/v1/query_range``)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from opsmith.connectors.base import ConnectorError, FetchResult, LogConnector, register
from opsmith.schemas import LogEvent

_LEVEL_LABELS = ("level", "severity", "detected_level", "lvl")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
DEFAULT_LIMIT = 5000
# Pages of ``limit`` entries read per fetch. A window with more than MAX_PAGES * limit
# entries is not finished in one run: the cursor stops where reading stopped.
MAX_PAGES = 20


def _to_ns(moment: datetime) -> int:
    return ((moment - _EPOCH) // timedelta(microseconds=1)) * 1000


def _from_ns(ns: int) -> datetime:
    return _EPOCH + timedelta(microseconds=ns // 1000)


@register("loki")
class LokiConnector(LogConnector):
    """config: ``url``, ``query`` (LogQL), optional ``limit`` (page size, default 5000),
    ``service_label`` (default ``service_name``)."""

    uses_url = True

    def fetch(self, start: datetime, end: datetime, cursor: dict[str, Any]) -> FetchResult:
        url = self.base_url() + "/loki/api/v1/query_range"
        query = self.config.get("query")
        if not isinstance(query, str) or not query:
            raise ConnectorError("loki connector config needs a 'query'")
        try:
            limit = int(self.config.get("limit", DEFAULT_LIMIT))
        except (TypeError, ValueError):
            limit = 0
        if limit < 1:
            raise ConnectorError("loki 'limit' must be a positive integer")
        service_label = self.config.get("service_label", "service_name")
        start_ns, end_ns = _to_ns(start), _to_ns(end)

        events: list[tuple[int, LogEvent]] = []
        skipped = 0
        page_start = start_ns
        # Entries at ``page_start`` that an earlier page already returned. The next page
        # starts on the same nanosecond (several entries can share it), so they come again.
        boundary: set[tuple[str, int, str]] = set()
        complete = False
        for _ in range(MAX_PAGES):
            body = self.get_json(
                url,
                {
                    "query": query,
                    "start": str(page_start),
                    "end": str(end_ns),
                    "limit": str(limit),
                    "direction": "forward",
                },
            )
            if body.get("status") != "success":
                raise ConnectorError("loki did not report success")
            data = body.get("data")
            streams = data.get("result") if isinstance(data, dict) else None
            if not isinstance(streams, list):
                raise ConnectorError("loki response has no result list")
            returned = 0
            page_last = page_start  # newest timestamp on this page
            fresh: set[tuple[str, int, str]] = set()
            for stream in streams:
                if not isinstance(stream, dict):
                    continue
                raw_labels = stream.get("stream")
                labels = (
                    {str(k): str(v) for k, v in raw_labels.items()}
                    if isinstance(raw_labels, dict)
                    else {}
                )
                stream_key = json.dumps(labels, sort_keys=True)
                values = stream.get("values")
                for item in values if isinstance(values, list) else []:
                    returned += 1
                    try:
                        ts_ns, line = int(item[0]), item[1]
                        page_last = max(page_last, ts_ns)
                        if not isinstance(line, str):
                            raise TypeError("log line is not a string")
                        key = (stream_key, ts_ns, line)
                        if key in boundary:
                            continue
                        event = self._event(ts_ns, line, labels, service_label)
                    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                        skipped += 1
                        continue
                    fresh.add(key)
                    events.append((ts_ns, event))
            if returned < limit:
                complete = True
                break
            if page_last == page_start:
                # A full page that never gets past its first nanosecond. Loki pages by time
                # only, so the entries behind it cannot be reached; say so instead of
                # dropping them.
                raise ConnectorError(
                    f"more than {limit} loki entries share one timestamp; raise 'limit'"
                )
            page_start = page_last
            boundary = {key for key in fresh if key[1] == page_last}

        if complete:
            return FetchResult(
                logs=[event for _, event in events],
                cursor={"last_end": end.isoformat()},
                skipped=skipped,
            )
        # The page budget ran out before the window did. Everything before ``page_start``
        # has been read. The cursor has microsecond resolution, so cut at that whole
        # microsecond: entries before it are returned now, the rest is read by the next run.
        cut_ns = (page_start // 1000) * 1000
        if cut_ns <= start_ns:
            raise ConnectorError(
                f"loki returned more than {MAX_PAGES * limit} entries without advancing; "
                "narrow the query or raise 'limit'"
            )
        return FetchResult(
            logs=[event for ts, event in events if ts < cut_ns],
            cursor={"last_end": _from_ns(cut_ns).isoformat()},
            skipped=skipped,
        )

    def _event(self, ts_ns: int, line: str, labels: dict[str, str], service_label: str) -> LogEvent:
        message, attrs, line_sev = _parse_line(line)
        severity = next((labels[k] for k in _LEVEL_LABELS if k in labels), None)
        return LogEvent(
            timestamp=_from_ns(ts_ns),
            service=labels.get(service_label) or self.default_service,
            environment=labels.get("env", labels.get("environment", "production")),
            severity=line_sev or severity or "info",
            message=message,
            attributes={**labels, **attrs},
            trace_id=attrs.get("trace_id") or labels.get("trace_id"),
        )


def _parse_line(line: str) -> tuple[str, dict[str, Any], str | None]:
    """Loki lines are often JSON; unwrap the message and level when they are."""
    if line.startswith("{"):
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            return line, {}, None
        if isinstance(data, dict):
            # The fallback key is only taken when the main one is missing; a line with both
            # keeps the other in its attributes.
            message = str(data.pop("message") if "message" in data else data.pop("msg", line))
            level = data.pop("level") if "level" in data else data.pop("severity", None)
            return message, data, str(level) if level else None
    return line, {}, None
