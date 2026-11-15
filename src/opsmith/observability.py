"""Platform self-observability: structured JSON logs and Prometheus-format metrics.

Durable per-stage cost/latency lives in ``UsageRecord`` and run rows (multi-process
safe, feeds the dashboard); the in-process registry below exposes live counters and
histograms on ``/metrics`` for scraping.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    handler = logging.StreamHandler()
    if json_logs:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300, 900)


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._hist: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = {}

    @staticmethod
    def _key(name: str, labels: dict[str, str]) -> tuple[str, tuple[tuple[str, str], ...]]:
        return name, tuple(sorted(labels.items()))

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        with self._lock:
            self._counters[self._key(name, labels)] += value

    def observe(self, name: str, value: float, **labels: str) -> None:
        with self._lock:
            key = self._key(name, labels)
            hist = self._hist.setdefault(key, [0.0] * (len(_BUCKETS) + 2))
            for i, bound in enumerate(_BUCKETS):
                if value <= bound:
                    hist[i] += 1
            hist[-2] += value  # sum
            hist[-1] += 1  # count

    @contextmanager
    def timer(self, name: str, **labels: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, time.perf_counter() - start, **labels)

    def render(self) -> str:
        def fmt(labels: tuple[tuple[str, str], ...], extra: str = "") -> str:
            items = [f'{k}="{v}"' for k, v in labels]
            if extra:
                items.append(extra)
            return "{" + ",".join(items) + "}" if items else ""

        lines: list[str] = []
        with self._lock:
            for (name, labels), value in sorted(self._counters.items()):
                lines.append(f"{name}{fmt(labels)} {value}")
            for (name, labels), hist in sorted(self._hist.items()):
                for i, bound in enumerate(_BUCKETS):
                    lines.append(f"{name}_bucket{fmt(labels, f'le="{bound}"')} {hist[i]}")
                lines.append(f"{name}_bucket{fmt(labels, 'le="+Inf"')} {hist[-1]}")
                lines.append(f"{name}_sum{fmt(labels)} {hist[-2]}")
                lines.append(f"{name}_count{fmt(labels)} {hist[-1]}")
        return "\n".join(lines) + "\n"


METRICS = MetricsRegistry()
