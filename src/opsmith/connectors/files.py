"""File-based connectors: JSONL / plain-text logs and JSONL metrics.

Useful for local development, batch backfills, and log shippers that write to disk.
"""

from __future__ import annotations

import glob
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opsmith.connectors.base import FetchResult, LogConnector, MetricConnector, register
from opsmith.schemas import LogEvent, MetricPoint

DEFAULT_TEXT_PATTERN = (
    r"^(?P<timestamp>\S+)\s+(?P<severity>[A-Za-z]+)\s+"
    r"(?:\[(?P<service>[^\]]+)\]\s+)?(?P<message>.*)$"
)


def parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        dt = datetime.fromtimestamp(seconds, tz=UTC)
    else:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _files(pattern: str) -> list[Path]:
    return [Path(p) for p in sorted(glob.glob(pattern, recursive=True)) if Path(p).is_file()]


@register("file_logs")
class FileLogConnector(LogConnector):
    """config: ``path`` (glob), ``format`` (jsonl|text), optional field names / ``pattern``."""

    def fetch(self, start: datetime, end: datetime, cursor: dict[str, Any]) -> FetchResult:
        fmt = self.config.get("format", "jsonl")
        fields = {
            "timestamp": self.config.get("timestamp_field", "timestamp"),
            "message": self.config.get("message_field", "message"),
            "severity": self.config.get("severity_field", "level"),
            "service": self.config.get("service_field", "service"),
        }
        pattern = re.compile(self.config.get("pattern", DEFAULT_TEXT_PATTERN))
        events: list[LogEvent] = []
        for path in _files(self.config["path"]):
            with path.open(encoding="utf-8", errors="replace") as fh:
                for raw_line in fh:
                    line = raw_line.rstrip("\n")
                    if not line.strip():
                        continue
                    record = (
                        self._parse_json(line, fields)
                        if fmt == "jsonl"
                        else self._parse_text(line, pattern)
                    )
                    if record is None:
                        continue
                    if start <= record.timestamp < end:
                        events.append(record)
        return FetchResult(logs=events, cursor={"last_end": end.isoformat()})

    def _parse_json(self, line: str, fields: dict[str, str]) -> LogEvent | None:
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, dict) or fields["message"] not in raw:
            return None
        known = set(fields.values()) | {"trace_id", "correlation_id", "version", "environment"}
        return LogEvent(
            timestamp=parse_ts(raw.get(fields["timestamp"], datetime.now(UTC))),
            service=str(raw.get(fields["service"]) or self.default_service),
            environment=str(raw.get("environment", self.config.get("environment", "production"))),
            severity=str(raw.get(fields["severity"], "info")),
            message=str(raw[fields["message"]]),
            attributes={k: v for k, v in raw.items() if k not in known},
            trace_id=raw.get("trace_id"),
            correlation_id=raw.get("correlation_id"),
            version=raw.get("version"),
        )

    def _parse_text(self, line: str, pattern: re.Pattern[str]) -> LogEvent | None:
        match = pattern.match(line)
        if not match:
            return None
        groups = match.groupdict()
        try:
            ts = parse_ts(groups["timestamp"])
        except ValueError:
            return None
        return LogEvent(
            timestamp=ts,
            service=groups.get("service") or self.default_service,
            environment=self.config.get("environment", "production"),
            severity=groups.get("severity") or "info",
            message=groups.get("message") or "",
        )


@register("file_metrics")
class FileMetricConnector(MetricConnector):
    """config: ``path`` (glob) of JSONL ``{timestamp, name, value, labels?, service?}``;
    ``baseline_seconds`` look-back (default 6h) is included for anomaly baselines."""

    def fetch(self, start: datetime, end: datetime, cursor: dict[str, Any]) -> FetchResult:
        lookback = float(self.config.get("baseline_seconds", 6 * 3600))
        lower = start.timestamp() - lookback
        points: list[MetricPoint] = []
        for path in _files(self.config["path"]):
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        raw = json.loads(line)
                        ts = parse_ts(raw["timestamp"])
                        value = float(raw["value"])
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                        continue
                    if lower <= ts.timestamp() < end.timestamp():
                        points.append(
                            MetricPoint(
                                timestamp=ts,
                                service=str(raw.get("service") or self.default_service),
                                environment=str(raw.get("environment", "production")),
                                name=str(raw["name"]),
                                value=value,
                                labels={str(k): str(v) for k, v in raw.get("labels", {}).items()},
                            )
                        )
        return FetchResult(metrics=points, cursor={"last_end": end.isoformat()})
