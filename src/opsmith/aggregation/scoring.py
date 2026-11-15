"""Task scoring and ranking (spec 5.4): severity x confidence x evidence strength x blast
radius x recency / effort, mapped onto P0-P3."""

from __future__ import annotations

import math
from datetime import datetime

from opsmith.db import utcnow

SEVERITY_WEIGHT = {"info": 1.0, "low": 2.0, "medium": 4.0, "high": 7.0, "critical": 10.0}
EFFORT_FACTOR = {"S": 1.0, "M": 1.4, "L": 2.0}
CATEGORY_BOOST = {
    "security": 1.3,
    "fix": 1.1,
    "performance": 1.0,
    "dependency": 1.0,
    "compliance": 0.9,
    "observability": 0.8,
    "refactor": 0.7,
}


def noisy_or(confidences: list[float]) -> float:
    """Independent pieces of evidence combine: 1 - prod(1 - c)."""
    p = 1.0
    for c in confidences:
        p *= 1.0 - max(0.0, min(c, 0.99))
    return round(1.0 - p, 4)


def score(
    *,
    severity: str,
    confidence: float,
    evidence_strength: float,
    occurrences: int,
    services_affected: int,
    source_kinds: int,
    effort: str,
    category: str,
    last_seen: datetime | None = None,
) -> float:
    sev = SEVERITY_WEIGHT.get(severity, 4.0)
    strength = 1.0 + math.log2(1.0 + max(evidence_strength, 0.0))
    frequency = 1.0 + math.log10(max(occurrences, 1))
    blast = 1.0 + 0.25 * max(services_affected - 1, 0)
    diversity = 1.25 if source_kinds > 1 else 1.0  # runtime + static evidence agree
    recency = 1.0
    if last_seen is not None:
        age_days = max((utcnow() - last_seen).total_seconds() / 86400, 0.0)
        recency = 0.5 + 0.5 * math.exp(-age_days / 7)
    raw = (
        sev
        * confidence
        * strength
        * frequency
        * blast
        * diversity
        * recency
        * CATEGORY_BOOST.get(category, 1.0)
        / EFFORT_FACTOR.get(effort, 1.4)
    )
    return round(raw, 3)


def priority_for(value: float) -> str:
    if value >= 25:
        return "P0"
    if value >= 12:
        return "P1"
    if value >= 5:
        return "P2"
    return "P3"
