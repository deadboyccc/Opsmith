"""VictoriaLogs connector (``/select/logsql/query``), read-only.

VictoriaLogs answers a LogsQL query with one JSON object per line: ``_time``, ``_msg``,
``_stream`` and the entry's fields. Each run reads its window oldest first, a page of ``limit``
entries at a time, each page starting at the last timestamp the one before it reached.

A log shipped through Docker's syslog driver carries the syslog severity in ``level``, and
that driver calls everything written to stderr an error. So the level a message states
itself wins: the ``level`` of a JSON message, or a level word near its start (``ERROR:``,
``WARN``, or nginx's ``[notice]``). The syslog level is the fallback.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from opsmith.connectors.base import ConnectorError, FetchResult, LogConnector, register
from opsmith.connectors.loki import _parse_line
from opsmith.schemas import LogEvent

DEFAULT_LIMIT = 5000
MAX_PAGES = 20  # as the Loki connector: more than this is continued by the next run
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_TIME = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})")
# Upper-case level words only: "error" in an ordinary sentence is not a level.
_TEXT_LEVEL = re.compile(
    r"\b(TRACE|DEBUG|INFO|NOTICE|LOG|WARN(?:ING)?|ERROR|FATAL|PANIC|CRIT(?:ICAL)?)\b"
    # nginx writes its level in lower case, in brackets: "[notice] 1#1: ..."
    r"|\[(debug|info|notice|warn|error|crit|alert|emerg)\]"
)
_LEVEL_SCAN_CHARS = 160
_INTERNAL = frozenset({"_time", "_msg", "_stream", "_stream_id"})


def _parse_time(text: str) -> int:
    """Nanoseconds since the epoch, from VictoriaLogs' RFC 3339 ``_time``."""
    found = _TIME.fullmatch(text)
    if found is None:
        raise ValueError(f"not a timestamp: {text!r}")
    whole, fraction, zone = found.groups()
    seconds = datetime.fromisoformat(whole + ("+00:00" if zone == "Z" else zone))
    whole_ns = ((seconds - _EPOCH) // timedelta(seconds=1)) * 1_000_000_000
    return whole_ns + int((fraction or "0").ljust(9, "0"))


def _format_time(ns: int) -> str:
    moment = _EPOCH + timedelta(seconds=ns // 1_000_000_000)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{ns % 1_000_000_000:09d}Z"


def _to_ns(moment: datetime) -> int:
    return ((moment - _EPOCH) // timedelta(microseconds=1)) * 1000


@register("victorialogs")
class VictoriaLogsConnector(LogConnector):
    """config: ``url``; optional ``query`` (a LogsQL filter, default ``*``), ``limit`` (page
    size, default 5000) and ``service_field`` (the field that names the service, default
    ``app_name``, the syslog tag)."""

    uses_url = True

    def fetch(self, start: datetime, end: datetime, cursor: dict[str, Any]) -> FetchResult:
        url = self.base_url() + "/select/logsql/query"
        query = self.config.get("query") or "*"
        if not isinstance(query, str):
            raise ConnectorError("victorialogs 'query' must be a LogsQL filter")
        try:
            limit = int(self.config.get("limit", DEFAULT_LIMIT))
        except (TypeError, ValueError):
            limit = 0
        if limit < 1:
            raise ConnectorError("victorialogs 'limit' must be a positive integer")
        service_field = str(self.config.get("service_field", "app_name"))
        start_ns, end_ns = _to_ns(start), _to_ns(end)

        events: list[tuple[int, LogEvent]] = []
        skipped = 0
        page_start = start_ns
        boundary: set[tuple[str, int, str]] = set()  # entries the last page ended on
        complete = False
        for _ in range(MAX_PAGES):
            logsql = (
                f"_time:[{_format_time(page_start)}, {_format_time(end_ns)}) ({query})"
                f" | sort by (_time) | limit {limit}"
            )
            lines = self.get_lines(url, {"query": logsql})
            page_last = page_start
            fresh: set[tuple[str, int, str]] = set()
            for line in lines:
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise TypeError("not a JSON object")
                    ts_ns = _parse_time(str(row["_time"]))
                    message = row.get("_msg")
                    if not isinstance(message, str):
                        raise TypeError("_msg is not a string")
                    page_last = max(page_last, ts_ns)
                    key = (str(row.get("_stream", "")), ts_ns, message)
                    if key in boundary:
                        continue
                    event = self._event(ts_ns, message, row, service_field)
                except (ValueError, TypeError, KeyError, RecursionError):
                    skipped += 1
                    continue
                fresh.add(key)
                events.append((ts_ns, event))
            if len(lines) < limit:
                complete = True
                break
            if page_last == page_start:
                raise ConnectorError(
                    f"more than {limit} victorialogs entries share one timestamp; raise 'limit'"
                )
            page_start = page_last
            boundary = {key for key in fresh if key[1] == page_last}

        if complete:
            return FetchResult(
                logs=[event for _, event in events],
                cursor={"last_end": end.isoformat()},
                skipped=skipped,
            )
        # Out of pages: what is before ``page_start`` (to the whole microsecond) has been read.
        cut_ns = (page_start // 1000) * 1000
        if cut_ns <= start_ns:
            raise ConnectorError(
                f"victorialogs returned more than {MAX_PAGES * limit} entries without "
                "advancing; narrow the query or raise 'limit'"
            )
        last_end = (_EPOCH + timedelta(microseconds=cut_ns // 1000)).isoformat()
        return FetchResult(
            logs=[event for ts, event in events if ts < cut_ns],
            cursor={"last_end": last_end},
            skipped=skipped,
        )

    def _event(self, ts_ns: int, raw: str, row: dict[str, Any], service_field: str) -> LogEvent:
        message, attrs, json_level = _parse_line(raw)
        text_level = None
        if json_level is None:
            found = _TEXT_LEVEL.search(message[:_LEVEL_SCAN_CHARS])
            text_level = (found.group(1) or found.group(2)) if found else None
        fields = {k: v for k, v in row.items() if k not in _INTERNAL}
        syslog_level = fields.get("level") or fields.get("severity")
        return LogEvent(
            timestamp=_EPOCH + timedelta(microseconds=ts_ns // 1000),
            service=str(fields.get(service_field) or self.default_service),
            environment=str(fields.get("environment") or self.environment),
            severity=str(json_level or text_level or syslog_level or "info"),
            message=message,
            attributes={**fields, **attrs},
            trace_id=attrs.get("trace_id") or fields.get("trace_id"),
        )
