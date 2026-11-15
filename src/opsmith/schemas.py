"""Pydantic schemas: normalized signals, project settings, and LLM stage contracts.

Every LLM stage returns JSON validated against one of the ``*Output`` models below;
anything that fails validation is rejected (and retried once with the error).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


class MetricPoint(BaseModel):
    timestamp: datetime
    service: str
    environment: str = "production"
    name: str
    value: float
    labels: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------- settings


class ProtectedPath(BaseModel):
    pattern: str  # fnmatch-style glob, e.g. ".github/**" or "src/auth/*"
    mode: Literal["elevated", "excluded"] = "elevated"


class Budgets(BaseModel):
    max_tokens_per_run: int = 400_000
    max_cost_per_run_usd: float = 5.0
    max_cost_per_day_usd: float = 50.0
    max_execution_seconds: int = 1800
    max_agent_iterations: int = 8
    max_concurrent_executions: int = 2


class StageModel(BaseModel):
    provider: str = "heuristic"
    model: str = "heuristic-v1"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    max_tokens: int = 16000


class LLMSettings(BaseModel):
    default: StageModel = Field(default_factory=StageModel)
    stages: dict[str, StageModel] = Field(default_factory=dict)
    provider_allowlist: list[str] | None = None  # None -> process-level allowlist

    def for_stage(self, stage: str) -> StageModel:
        return self.stages.get(stage, self.default)


class PolicyRule(BaseModel):
    """Policy-as-code rule evaluated by the compliance analyzer."""

    id: str
    description: str
    path_glob: str = "**/*"
    forbid_regex: str | None = None
    require_regex: str | None = None
    severity: Severity = "medium"


class NotificationChannel(BaseModel):
    type: Literal["log", "webhook"] = "log"
    url: str | None = None
    events: list[str] = Field(default_factory=lambda: ["*"])


class ProjectSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inference_interval_hours: float = 6.0
    lead_max_carry: int = 3
    audit_cron: str = "0 2 * * *"
    audit_full_every_days: int = 7
    ingest_interval_seconds: int = 900
    pr_poll_seconds: int = 600
    recently_closed_days: int = 14
    min_task_confidence: float = 0.3
    max_tasks_per_aggregation: int = 25
    auto_submit_for_review: bool = True
    post_merge_verification: bool = True
    verification_delay_hours: float = 6.0
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


# --------------------------------------------------------------------------- LLM stage I/O


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
