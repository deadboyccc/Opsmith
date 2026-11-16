"""Push-only connectors.

A collector (a log shipper, an OpenTelemetry exporter, a script) sends records to
``POST /api/v1/connectors/{id}/push``. These types give such a source a connector to push to
without pretending to be a pull source: their scheduled fetch has nothing to read.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from opsmith.connectors.base import FetchResult, LogConnector, MetricConnector, register


@register("push_logs")
class PushLogConnector(LogConnector):
    """Receives log records through the push API. Takes no configuration."""

    def fetch(self, start: datetime, end: datetime, cursor: dict[str, Any]) -> FetchResult:
        return FetchResult(cursor=dict(cursor))


@register("push_metrics")
class PushMetricConnector(MetricConnector):
    """Receives metric points through the push API. Takes no configuration."""

    def fetch(self, start: datetime, end: datetime, cursor: dict[str, Any]) -> FetchResult:
        return FetchResult(cursor=dict(cursor))
