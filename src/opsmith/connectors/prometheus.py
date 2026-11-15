"""Prometheus connector (``/api/v1/query_range``)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from opsmith.connectors.base import ConnectorError, FetchResult, MetricConnector, register
from opsmith.schemas import MetricPoint


@register("prometheus")
class PrometheusConnector(MetricConnector):
    """config: ``url``, ``queries``: ``[{name, promql}]``, ``step`` (default ``60s``),
    ``baseline_seconds`` (default 6h), ``service_label`` (default ``service``)."""

    def fetch(self, start: datetime, end: datetime, cursor: dict[str, Any]) -> FetchResult:
        lookback = timedelta(seconds=float(self.config.get("baseline_seconds", 6 * 3600)))
        url = self.config["url"].rstrip("/") + "/api/v1/query_range"
        service_label = self.config.get("service_label", "service")
        points: list[MetricPoint] = []
        for query in self.config.get("queries", []):
            params = {
                "query": query["promql"],
                "start": f"{(start - lookback).timestamp():.3f}",
                "end": f"{end.timestamp():.3f}",
                "step": str(self.config.get("step", "60s")),
            }
            try:
                resp = self.http.get(url, params=params)
                resp.raise_for_status()
                body = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise ConnectorError(f"prometheus query {query['name']!r} failed: {exc}") from exc
            if body.get("status") != "success":
                raise ConnectorError(f"prometheus returned {body.get('error', 'error')!r}")
            for series in body.get("data", {}).get("result", []):
                metric_labels: dict[str, str] = dict(series.get("metric", {}))
                metric_labels.pop("__name__", None)
                service = metric_labels.get(service_label) or self.default_service
                for ts, value in series.get("values", []):
                    try:
                        val = float(value)
                    except ValueError:
                        continue
                    if math.isnan(val):
                        continue
                    points.append(
                        MetricPoint(
                            timestamp=datetime.fromtimestamp(float(ts), tz=UTC),
                            service=service,
                            name=query["name"],
                            value=val,
                            labels=metric_labels,
                        )
                    )
        return FetchResult(metrics=points, cursor={"last_end": end.isoformat()})
