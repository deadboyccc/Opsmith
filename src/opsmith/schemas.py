"""Pydantic schemas: normalized signals, project settings, and LLM stage contracts.

Every LLM stage returns JSON validated against one of the ``*Output`` models below;
anything that fails validation is rejected (and retried once with the error).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Severity = Literal["info", "low", "medium", "high", "critical"]
Category = Literal[
    "fix", "security", "performance", "compliance", "refactor", "observability", "dependency"
]
SEVERITY_RANK: dict[str, int] = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
CATEGORIES: tuple[str, ...] = (
    "fix",
    "security",
    "performance",
    "compliance",
    "refactor",
    "observability",
    "dependency",
)


# --------------------------------------------------------------------------- signals


def _as_utc(value: datetime) -> datetime:
    """Signals are stored and compared in aware UTC; a naive timestamp is taken to be UTC."""
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC)


class LogEvent(BaseModel):
    """Common event schema (spec 5.1) for log records."""

    timestamp: datetime
    service: str
    environment: str = "production"
    severity: str = "info"
    message: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None
    correlation_id: str | None = None
    version: str | None = None

    _utc = field_validator("timestamp")(_as_utc)

    @field_validator("trace_id", "correlation_id", "version", mode="before")
    @classmethod
    def _id_as_text(cls, value: Any) -> Any:
        # Shippers send numeric ids and versions; one such record must not fail the batch.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        return value


class MetricPoint(BaseModel):
    timestamp: datetime
    service: str
    environment: str = "production"
    name: str
    value: float
    labels: dict[str, str] = Field(default_factory=dict)

    _utc = field_validator("timestamp")(_as_utc)


# --------------------------------------------------------------------------- settings


def _valid_regex(pattern: str) -> str:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid regular expression {pattern!r}: {exc}") from None
    return pattern


class ProtectedPath(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(min_length=1)  # fnmatch-style glob, e.g. ".github/**" or "src/auth/*"
    mode: Literal["elevated", "excluded"] = "elevated"


class Budgets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tokens_per_run: int = Field(default=400_000, gt=0)
    max_cost_per_run_usd: float = Field(default=5.0, ge=0)
    max_cost_per_day_usd: float = Field(default=50.0, ge=0)
    max_execution_seconds: int = Field(default=1800, gt=0)
    max_agent_iterations: int = Field(default=8, ge=1)
    max_concurrent_executions: int = Field(default=2, ge=1)


class StageModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "heuristic"
    model: str = "heuristic-v1"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    max_tokens: int = Field(default=16000, gt=0)
    # USD per million tokens, for cost accounting and the cost budgets. Optional: Opsmith has
    # list prices for the Claude models, and costs any other model at the most expensive rate
    # it knows, so a budget is never bypassed by an unpriced model. Set these to the real
    # rates of your model; use 0 for a local model.
    input_price_per_mtok: float | None = Field(default=None, ge=0)
    output_price_per_mtok: float | None = Field(default=None, ge=0)

    @property
    def prices(self) -> tuple[float, float] | None:
        if self.input_price_per_mtok is None or self.output_price_per_mtok is None:
            return None
        return self.input_price_per_mtok, self.output_price_per_mtok

    @model_validator(mode="after")
    def _both_prices(self) -> StageModel:
        if (self.input_price_per_mtok is None) != (self.output_price_per_mtok is None):
            raise ValueError("set both input_price_per_mtok and output_price_per_mtok, or neither")
        return self


class LLMSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: StageModel = Field(default_factory=StageModel)
    stages: dict[str, StageModel] = Field(default_factory=dict)
    provider_allowlist: list[str] | None = None  # None -> process-level allowlist

    def for_stage(self, stage: str) -> StageModel:
        return self.stages.get(stage, self.default)


class PolicyRule(BaseModel):
    """Policy-as-code rule evaluated by the compliance analyzer."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    description: str
    path_glob: str = "**/*"
    forbid_regex: str | None = None
    require_regex: str | None = None
    severity: Severity = "medium"

    @field_validator("forbid_regex", "require_regex")
    @classmethod
    def _regex(cls, value: str | None) -> str | None:
        return None if value is None else _valid_regex(value)


class NotificationChannel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["log", "webhook"] = "log"
    url: str | None = None
    events: list[str] = Field(default_factory=lambda: ["*"])

    @model_validator(mode="after")
    def _webhook_needs_url(self) -> NotificationChannel:
        if self.type == "webhook" and not (self.url or "").startswith(("http://", "https://")):
            raise ValueError("a webhook channel needs an http(s) url")
        return self


class ProjectSettings(BaseModel):
    """Per-project behaviour. Every value is range-checked here, because a bad one (a zero
    interval, an impossible cron) would otherwise only fail later inside a scheduled job."""

    model_config = ConfigDict(extra="forbid")

    inference_interval_hours: float = Field(default=6.0, gt=0)
    lead_max_carry: int = Field(default=3, ge=1)
    audit_cron: str = "0 2 * * *"
    audit_full_every_days: int = Field(default=7, ge=1)
    ingest_interval_seconds: int = Field(default=900, ge=60)
    pr_poll_seconds: int = Field(default=600, ge=60)
    recently_closed_days: int = Field(default=14, ge=0)
    min_task_confidence: float = Field(default=0.3, ge=0.0, le=1.0)
    max_tasks_per_aggregation: int = Field(default=25, ge=1)
    auto_submit_for_review: bool = True
    post_merge_verification: bool = True
    verification_delay_hours: float = Field(default=6.0, ge=0)
    protected_paths: list[ProtectedPath] = Field(
        default_factory=lambda: [
            ProtectedPath(pattern=".github/**", mode="elevated"),
            ProtectedPath(pattern="**/auth/**", mode="elevated"),
            ProtectedPath(pattern="**/*.pem", mode="excluded"),
        ]
    )
    budgets: Budgets = Field(default_factory=Budgets)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    executor_agent: str = "heuristic"
    policy_rules: list[PolicyRule] = Field(default_factory=list)
    license_allowlist: list[str] = Field(
        default_factory=lambda: ["MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC"]
    )
    osv_online: bool = False
    notifications: list[NotificationChannel] = Field(
        default_factory=lambda: [NotificationChannel(type="log")]
    )
    redaction_extra_patterns: list[str] = Field(default_factory=list)

    @field_validator("audit_cron")
    @classmethod
    def _cron(cls, value: str) -> str:
        from opsmith.scheduler import CronExpr  # noqa: PLC0415 - scheduler imports this module

        CronExpr.parse(value)
        return value

    @field_validator("redaction_extra_patterns")
    @classmethod
    def _patterns(cls, value: list[str]) -> list[str]:
        return [_valid_regex(p) for p in value]


# --------------------------------------------------------------------------- LLM stage I/O

# Model output is untrusted. Identifiers are bounded to the columns that store them
# (``models.Finding`` / ``models.Lead``), so one oversized or nonsensical value is contained
# here instead of failing the whole run at flush time.
MAX_SIGNAL_CHARS = 200
MAX_PATH_CHARS = 500
MAX_SOURCE_LINE = 10_000_000


def _clamp_signal(value: Any) -> Any:
    """An over-long signal is still an identity: keep a stable prefix of it."""
    return value[:MAX_SIGNAL_CHARS] if isinstance(value, str) else value


def _bounded_path(value: Any) -> Any:
    """A cut-off path would name a different file, so an over-long one is dropped instead."""
    return None if isinstance(value, str) and len(value) > MAX_PATH_CHARS else value


class EvidenceRefModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str


class InferenceFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=300)
    description: str
    category: Category
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_refs: list[str] = Field(default_factory=list)
    signal: str | None = None
    suspected_path: str | None = None
    suspected_line: int | None = None
    suggested_next_investigation: str = ""
    from_lead_id: str | None = None

    _signal = field_validator("signal", mode="before")(_clamp_signal)
    _path = field_validator("suspected_path", mode="before")(_bounded_path)

    @field_validator("suspected_line")
    @classmethod
    def _plausible_line(cls, value: int | None) -> int | None:
        """A line that cannot exist (zero, negative, absurdly large) is no location at all."""
        return value if value is not None and 1 <= value <= MAX_SOURCE_LINE else None


class LeadUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lead_id: str
    action: Literal["confirm", "refute", "carry"]
    context_update: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    finding_index: int | None = None


class NewLead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=300)
    hypothesis: str
    evidence_refs: list[str] = Field(default_factory=list)
    signal: str | None = None
    suspected_path: str | None = None

    _signal = field_validator("signal", mode="before")(_clamp_signal)
    _path = field_validator("suspected_path", mode="before")(_bounded_path)


class InferenceOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = ""
    findings: list[InferenceFinding] = Field(default_factory=list)
    lead_updates: list[LeadUpdate] = Field(default_factory=list)
    new_leads: list[NewLead] = Field(default_factory=list)


class TriageDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    keep: bool
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""
    severity: Severity | None = None


class TriageOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: list[TriageDecision] = Field(default_factory=list)


class TaskDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=300)
    problem_statement: str
    proposed_approach: str
    acceptance_criteria: list[str]
    risk_assessment: str
    effort_estimate: Literal["S", "M", "L"]
    tests_to_add: list[str] = Field(default_factory=list)
    metrics_to_watch: list[str] = Field(default_factory=list)

    @field_validator("acceptance_criteria")
    @classmethod
    def _non_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("at least one acceptance criterion is required")
        return value
