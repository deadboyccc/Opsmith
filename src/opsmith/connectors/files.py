"""File-based connectors: JSONL / plain-text logs and JSONL metrics.

Useful for local development, batch backfills, and log shippers that write to disk.

The ``path`` comes from connector configuration, so every file it matches must lie inside
the operator's ``OPSMITH_LOCAL_ROOT`` and outside the platform's data directory
(``opsmith.localfs.confine``). Lines that cannot be parsed are left out and counted in
``FetchResult.skipped``; one bad record never fails the fetch.
"""

from __future__ import annotations

import glob
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opsmith.connectors.base import (
    ConnectorError,
    FetchResult,
    LogConnector,
    MetricConnector,
    register,
)
from opsmith.localfs import confine
from opsmith.schemas import LogEvent, MetricPoint

DEFAULT_TEXT_PATTERN = (
    r"^(?P<timestamp>\S+)\s+(?P<severity>[A-Za-z]+)\s+"
    r"(?:\[(?P<service>[^\]]+)\]\s+)?(?P<message>.*)$"
)
_GLOB_MAGIC = re.compile(r"[*?\[]")
# Everything a malformed record can raise while it is turned into a signal. pydantic's
# ValidationError is a ValueError.
_BAD_RECORD = (
    ValueError,
    TypeError,
    KeyError,
    AttributeError,
    OverflowError,
    OSError,
    RecursionError,
)


_EPOCH_TEXT = re.compile(r"[+-]?\d{9,}(?:\.\d+)?")  # nine digits: from 1973 as seconds


def parse_ts(value: Any) -> datetime:
    """Parse an ISO-8601 string or an epoch number (s, ms, us or ns). Raises ``ValueError``."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        for limit, scale in ((1e17, 1e9), (1e14, 1e6), (1e11, 1e3)):
            if abs(seconds) > limit:
                seconds /= scale
                break
        try:
            dt = datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError) as exc:
            raise ValueError(f"timestamp out of range: {value!r}") from exc
    elif isinstance(value, str):
        text = value.strip()
        if _EPOCH_TEXT.fullmatch(text):
            # Before ISO: ``fromisoformat`` accepts the basic form ``YYYYMMDDTHHMM``, which a
            # 13-digit epoch in milliseconds happens to fit about once in two hundred.
            return parse_ts(float(text))
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return parse_ts(float(text))  # epoch written some other way, e.g. 1.7e9
    else:
        raise ValueError(f"not a timestamp: {value!r}")
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def confined_files(pattern: Any, root: Path | None, deny: tuple[Path, ...]) -> list[Path]:
    """Files matching the glob ``pattern``, each one checked against the local root.

    Raises ``LocalAccessError`` if the pattern points outside the root, or if any matched
    file (after following symlinks) is outside it or inside a denied directory.
    """
    if not isinstance(pattern, str) or not pattern:
        raise ConnectorError("file connector config needs a 'path'")
    # The fixed part of the pattern is checked first, so a pattern aimed outside the root is
    # refused (and never walked) even when it matches nothing.
    magic = _GLOB_MAGIC.search(pattern)
    fixed = pattern[: magic.start()] if magic else pattern
    if magic and not fixed.endswith(os.sep):
        fixed = os.path.dirname(fixed)
    confine(fixed or ".", root, what="file connector", deny=deny)
    return [
        confine(match, root, what="file connector", deny=deny)
        for match in sorted(glob.glob(pattern, recursive=True))
        if Path(match).is_file()
    ]


@register("file_logs")
class FileLogConnector(LogConnector):
    """config: ``path`` (glob), ``format`` (jsonl|text), optional field names / ``pattern``."""

    def __init__(self, config: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(config, **kwargs)
        try:
            self._pattern = re.compile(config.get("pattern", DEFAULT_TEXT_PATTERN))
        except (re.error, TypeError) as exc:
            raise ConnectorError(
                f"file_logs 'pattern' is not a valid regular expression: {exc}"
            ) from None
        if "timestamp" not in self._pattern.groupindex:
            raise ConnectorError("file_logs 'pattern' needs a (?P<timestamp>...) group")

    def fetch(self, start: datetime, end: datetime, cursor: dict[str, Any]) -> FetchResult:
        fmt = self.config.get("format", "jsonl")
        fields = {
            "timestamp": self.config.get("timestamp_field", "timestamp"),
            "message": self.config.get("message_field", "message"),
            "severity": self.config.get("severity_field", "level"),
            "service": self.config.get("service_field", "service"),
        }
        events: list[LogEvent] = []
        skipped = 0
        for path in confined_files(self.config.get("path"), self.local_root, self.local_deny):
            with path.open(encoding="utf-8", errors="replace") as fh:
                for raw_line in fh:
                    line = raw_line.rstrip("\n")
                    if not line.strip():
                        continue
                    try:
                        record = (
                            self._parse_json(line, fields)
                            if fmt == "jsonl"
                            else self._parse_text(line)
                        )
                    except _BAD_RECORD:
                        record = None
                    if record is None:
                        skipped += 1
                    elif start <= record.timestamp < end:
                        events.append(record)
        return FetchResult(logs=events, cursor={"last_end": end.isoformat()}, skipped=skipped)

    def _parse_json(self, line: str, fields: dict[str, str]) -> LogEvent | None:
        raw = json.loads(line)
        # A record without a timestamp cannot be placed in a window; it is skipped (and
        # counted) rather than stamped "now", which no window ever contained.
        if not isinstance(raw, dict) or fields["message"] not in raw:
            return None
        if raw.get(fields["timestamp"]) is None:
            return None
        known = set(fields.values()) | {"trace_id", "correlation_id", "version", "environment"}
        return LogEvent(
            timestamp=parse_ts(raw[fields["timestamp"]]),
            service=str(raw.get(fields["service"]) or self.default_service),
            environment=str(raw.get("environment", self.environment)),
            severity=str(raw.get(fields["severity"], "info")),
            message=str(raw[fields["message"]]),
            attributes={k: v for k, v in raw.items() if k not in known},
            trace_id=raw.get("trace_id"),
            correlation_id=raw.get("correlation_id"),
            version=raw.get("version"),
        )

    def _parse_text(self, line: str) -> LogEvent | None:
        match = self._pattern.match(line)
        if not match:
            return None
        groups = match.groupdict()
        return LogEvent(
            timestamp=parse_ts(groups["timestamp"]),
            service=groups.get("service") or self.default_service,
            environment=self.environment,
            severity=groups.get("severity") or "info",
            message=groups.get("message") or "",
        )


@register("file_metrics")
class FileMetricConnector(MetricConnector):
    """config: ``path`` (glob) of JSONL ``{timestamp, name, value, labels?, service?}``;
    ``baseline_seconds`` look-back (default 6h) is included for anomaly baselines."""

    def fetch(self, start: datetime, end: datetime, cursor: dict[str, Any]) -> FetchResult:
        lower = start.timestamp() - self.seconds("baseline_seconds", 6 * 3600)
        environment = self.environment
        points: list[MetricPoint] = []
        skipped = 0
        for path in confined_files(self.config.get("path"), self.local_root, self.local_deny):
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        raw = json.loads(line)
                        point = MetricPoint(
                            timestamp=parse_ts(raw["timestamp"]),
                            service=str(raw.get("service") or self.default_service),
                            environment=str(raw.get("environment", environment)),
                            name=str(raw["name"]),
                            value=float(raw["value"]),
                            labels={str(k): str(v) for k, v in (raw.get("labels") or {}).items()},
                        )
                    except _BAD_RECORD:
                        skipped += 1
                        continue
                    if lower <= point.timestamp.timestamp() < end.timestamp():
                        points.append(point)
        return FetchResult(metrics=points, cursor={"last_end": end.isoformat()}, skipped=skipped)
