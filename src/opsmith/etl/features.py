"""Metric aggregates and anomaly features (baselines, deltas, percentile shifts)."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from statistics import fmean, pstdev
from typing import Any

from opsmith.schemas import MetricPoint

_HIGHER_IS_WORSE = re.compile(
    r"latency|duration|error|fail|5xx|4xx|timeout|saturation|cpu|memory|queue|lag|retry|p9\d",
    re.IGNORECASE,
)
_LOWER_IS_WORSE = re.compile(r"throughput|success|availability|rps|qps|conversion|uptime", re.I)


def percentile(values: list[float], q: float) -> float:
    if not values:
        return math.nan
    data = sorted(values)
    k = (len(data) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return data[int(k)]
    return data[lo] + (data[hi] - data[lo]) * (k - lo)


def direction(name: str) -> str:
    if _LOWER_IS_WORSE.search(name):
        return "lower_is_worse"
    if _HIGHER_IS_WORSE.search(name):
        return "higher_is_worse"
    return "either"


def series_key(point: MetricPoint) -> str:
    labels = ",".join(f"{k}={v}" for k, v in sorted(point.labels.items()) if k != "service")
    return f"{point.name}{{{labels}}}" if labels else point.name


def _r(value: float) -> float | None:
    return None if math.isnan(value) or math.isinf(value) else round(value, 4)


def compute_features(
    points: Iterable[MetricPoint],
    window_start: datetime,
    *,
    z_threshold: float = 3.0,
    delta_threshold: float = 0.5,
    min_delta: float = 0.1,
) -> list[dict[str, Any]]:
    """One feature row per series. Points before ``window_start`` form the baseline."""
    current: dict[str, list[float]] = defaultdict(list)
    baseline: dict[str, list[float]] = defaultdict(list)
    names: dict[str, str] = {}
    for p in points:
        key = series_key(p)
        names[key] = p.name
        (current if p.timestamp >= window_start else baseline)[key].append(p.value)

    features: list[dict[str, Any]] = []
    for key in sorted(set(current) | set(baseline)):
        cur, base = current.get(key, []), baseline.get(key, [])
        if not cur:
            continue
        mean = fmean(cur)
        b_mean = fmean(base) if base else math.nan
        b_std = pstdev(base) if len(base) > 1 else math.nan
        p95, b_p95 = percentile(cur, 0.95), percentile(base, 0.95)
        delta = (mean - b_mean) / abs(b_mean) if base and b_mean != 0 else math.nan
        if base and not math.isnan(b_std):
            z = (mean - b_mean) / b_std if b_std > 0 else (0.0 if mean == b_mean else math.inf)
        else:
            z = math.nan
        p95_shift = (p95 - b_p95) / abs(b_p95) if base and b_p95 != 0 else math.nan
        dirn = direction(names[key])
        signed = 1 if dirn != "lower_is_worse" else -1
        worse_z = (z * signed) if not math.isnan(z) else math.nan
        worse_delta = (delta * signed) if not math.isnan(delta) else math.nan
        if dirn == "either":
            worse_z = abs(z) if not math.isnan(z) else math.nan
            worse_delta = abs(delta) if not math.isnan(delta) else math.nan
        z_hit = not math.isnan(worse_z) and worse_z >= z_threshold
        delta_known = not math.isnan(worse_delta)
        material = not delta_known or worse_delta >= min_delta  # ignore tiny-but-stable shifts
        big_delta = delta_known and worse_delta >= delta_threshold and len(base) >= 3
        anomalous = bool((z_hit and material) or big_delta)
        features.append(
            {
                "series": key,
                "name": names[key],
                "signal": f"metric:{key}",
                "direction": dirn,
                "count": len(cur),
                "mean": _r(mean),
                "min": _r(min(cur)),
                "max": _r(max(cur)),
                "p50": _r(percentile(cur, 0.5)),
                "p95": _r(p95),
                "p99": _r(percentile(cur, 0.99)),
                "baseline_count": len(base),
                "baseline_mean": _r(b_mean),
                "baseline_std": _r(b_std),
                "baseline_p95": _r(b_p95),
                "delta_pct": _r(delta * 100) if not math.isnan(delta) else None,
                "z_score": _r(z) if not math.isinf(z) else 999.0,
                "p95_shift_pct": _r(p95_shift * 100) if not math.isnan(p95_shift) else None,
                "anomalous": anomalous,
            }
        )
    return features
