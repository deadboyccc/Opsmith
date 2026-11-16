"""Prometheus connector (``/api/v1/query_range``)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from opsmith.connectors.base import ConnectorError, FetchResult, MetricConnector, register
from opsmith.schemas import MetricPoint


@register("prometheus")
class PrometheusConnector(MetricConnector):
    """config: ``url``, ``queries``: ``[{name, promql}]``, ``step`` (default ``60s``),
    ``baseline_seconds`` (default 6h), ``service_label`` (default ``service``)."""

    uses_url = True

    def fetch(self, start: datetime, end: datetime, cursor: dict[str, Any]) -> FetchResult:
        url = self.base_url() + "/api/v1/query_range"
        lookback = timedelta(seconds=float(self.config.get("baseline_seconds", 6 * 3600)))
        service_label = self.config.get("service_label", "service")
        queries = self.config.get("queries", [])
        if not isinstance(queries, list) or not all(
            isinstance(q, dict)
            and isinstance(q.get("name"), str)
            and isinstance(q.get("promql"), str)
            for q in queries
        ):
            raise ConnectorError("prometheus 'queries' must be a list of {name, promql}")
        points: list[MetricPoint] = []
        skipped = 0
        for query in queries:
            body = self.get_json(
                url,
                {
                    "query": query["promql"],
                    "start": f"{(start - lookback).timestamp():.3f}",
                    "end": f"{end.timestamp():.3f}",
                    "step": str(self.config.get("step", "60s")),
                },
            )
            if body.get("status") != "success":
                raise ConnectorError(f"prometheus query {query['name']!r} did not succeed")
            data = body.get("data")
            result = data.get("result") if isinstance(data, dict) else None
            if not isinstance(result, list):
                raise ConnectorError(f"prometheus query {query['name']!r} returned no result list")
            for series in result:
                if not isinstance(series, dict):
                    continue
                raw_labels = series.get("metric")
                metric_labels = (
                    {str(k): str(v) for k, v in raw_labels.items()}
                    if isinstance(raw_labels, dict)
                    else {}
                )
                metric_labels.pop("__name__", None)
                service = metric_labels.get(service_label) or self.default_service
                values = series.get("values")
                for item in values if isinstance(values, list) else []:
                    try:
                        moment = datetime.fromtimestamp(float(item[0]), tz=UTC)
                        val = float(item[1])
                    except (ValueError, TypeError, IndexError, KeyError, OverflowError, OSError):
                        skipped += 1
                        continue
                    if math.isnan(val):  # Prometheus' "no value at this step"
                        continue
                    points.append(
                        MetricPoint(
                            timestamp=moment,
                            service=service,
                            name=query["name"],
                            value=val,
                            labels=metric_labels,
                        )
                    )
        return FetchResult(metrics=points, cursor={"last_end": end.isoformat()}, skipped=skipped)
