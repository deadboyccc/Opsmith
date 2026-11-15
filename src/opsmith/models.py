"""Core logical data model (spec section 6), multi-tenant throughout.

Every row carries ``tenant_id``; every query in services and the API filters on it.
JSON columns are *replaced*, never mutated in place (SQLAlchemy does not track in-place
mutation of plain JSON values).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from opsmith.db import Base, new_id, utcnow


def _id(prefix: str) -> Any:
    return mapped_column(String(40), primary_key=True, default=lambda: new_id(prefix))


def _tenant() -> Any:
    return mapped_column(String(40), ForeignKey("tenants.id"), index=True)


def _created() -> Any:
    return mapped_column(default=utcnow)


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = _id("ten")
    name: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = _created()


class User(Base):
    """A human (viewer/reviewer/admin) or a machine principal (service)."""

    __tablename__ = "users"
    id: Mapped[str] = _id("usr")
    tenant_id: Mapped[str] = _tenant()
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320), default=None)
    role: Mapped[str] = mapped_column(String(20), default="viewer")
    api_key_hash: Mapped[str] = mapped_column(String(128), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _created()


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)
    id: Mapped[str] = _id("prj")
    tenant_id: Mapped[str] = _tenant()
    name: Mapped[str] = mapped_column(String(200))
    # Validated through opsmith.schemas.ProjectSettings.
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _created()


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("project_id", "name"),)
    id: Mapped[str] = _id("repo")
    tenant_id: Mapped[str] = _tenant()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    scm_provider: Mapped[str] = mapped_column(String(20), default="local")  # local | github
    url: Mapped[str] = mapped_column(String(500))  # local path or https clone URL
    full_name: Mapped[str | None] = mapped_column(String(200), default=None)  # owner/repo
    default_branch: Mapped[str] = mapped_column(String(100), default="main")
    credentials_ref: Mapped[str | None] = mapped_column(String(200), default=None)
    test_command: Mapped[str | None] = mapped_column(String(500), default=None)
    lint_command: Mapped[str | None] = mapped_column(String(500), default=None)
    build_command: Mapped[str | None] = mapped_column(String(500), default=None)
    audit_types: Mapped[list[Any]] = mapped_column(
        default=lambda: ["security", "bugs", "dependency", "compliance", "performance"]
    )
    last_audited_sha: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = _created()


class Service(Base):
    __tablename__ = "services"
    __table_args__ = (UniqueConstraint("project_id", "name", "environment"),)
    id: Mapped[str] = _id("svc")
    tenant_id: Mapped[str] = _tenant()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    environment: Mapped[str] = mapped_column(String(50), default="production")
    owner: Mapped[str | None] = mapped_column(String(200), default=None)
    repository_id: Mapped[str | None] = mapped_column(ForeignKey("repositories.id"), default=None)
    # Repo path prefixes owned by the service (used to map runtime signals to code).
    path_prefixes: Mapped[list[Any]] = mapped_column(default=list)
    inference_interval_hours: Mapped[float | None] = mapped_column(Float, default=None)
    created_at: Mapped[datetime] = _created()


class Connector(Base):
    __tablename__ = "connectors"
    __table_args__ = (UniqueConstraint("project_id", "name"),)
    id: Mapped[str] = _id("con")
    tenant_id: Mapped[str] = _tenant()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(50))
    kind: Mapped[str] = mapped_column(String(20))  # logs | metrics
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    credentials_ref: Mapped[str | None] = mapped_column(String(200), default=None)
    service_id: Mapped[str | None] = mapped_column(ForeignKey("services.id"), default=None)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=900)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    cursor: Mapped[dict[str, Any]] = mapped_column(default=dict)
    last_run_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = _created()


class Schedule(Base):
    __tablename__ = "schedules"
    __table_args__ = (UniqueConstraint("job_kind", "target_id"),)
    id: Mapped[str] = _id("sch")
    tenant_id: Mapped[str] = _tenant()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    job_kind: Mapped[str] = mapped_column(String(50))
    target_id: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    interval_seconds: Mapped[int | None] = mapped_column(Integer, default=None)
    cron: Mapped[str | None] = mapped_column(String(100), default=None)
    next_run_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_enqueued_at: Mapped[datetime | None] = mapped_column(default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Job(Base):
    """Durable queue entry. ``idempotency_key`` makes every enqueue safe to repeat."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_claim", "status", "run_after"),)
    id: Mapped[str] = _id("job")
    tenant_id: Mapped[str] = _tenant()
    kind: Mapped[str] = mapped_column(String(50), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    deferrals: Mapped[int] = mapped_column(Integer, default=0)
    run_after: Mapped[datetime] = mapped_column(default=utcnow)
    locked_by: Mapped[str | None] = mapped_column(String(100), default=None)
    lease_expires_at: Mapped[datetime | None] = mapped_column(default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    idempotency_key: Mapped[str] = mapped_column(String(300), unique=True)
    trace_id: Mapped[str | None] = mapped_column(String(40), default=None)
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _created()
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)


class RawBatch(Base):
    """One connector fetch: normalized + redacted records stored in object storage."""

    __tablename__ = "raw_batches"
    id: Mapped[str] = _id("rb")
    tenant_id: Mapped[str] = _tenant()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    connector_id: Mapped[str] = mapped_column(ForeignKey("connectors.id"), index=True)
    service_id: Mapped[str] = mapped_column(ForeignKey("services.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    window_start: Mapped[datetime] = mapped_column()
    window_end: Mapped[datetime] = mapped_column()
    object_key: Mapped[str] = mapped_column(String(500))
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    redactions: Mapped[dict[str, Any]] = mapped_column(default=dict)
    bundle_id: Mapped[str | None] = mapped_column(String(40), default=None, index=True)
    created_at: Mapped[datetime] = _created()


class AnalysisBundle(Base):
    __tablename__ = "analysis_bundles"
    id: Mapped[str] = _id("bnd")
    tenant_id: Mapped[str] = _tenant()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    service_id: Mapped[str] = mapped_column(ForeignKey("services.id"), index=True)
    window_start: Mapped[datetime] = mapped_column()
    window_end: Mapped[datetime] = mapped_column()
    log_templates: Mapped[list[Any]] = mapped_column(default=list)
    metric_features: Mapped[list[Any]] = mapped_column(default=list)
    raw_refs: Mapped[list[Any]] = mapped_column(default=list)
    stats: Mapped[dict[str, Any]] = mapped_column(default=dict)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0)
    consumed_by_run_id: Mapped[str | None] = mapped_column(String(40), default=None, index=True)
    created_at: Mapped[datetime] = _created()


class InferenceRun(Base):
    """Immutable once finished (enforced in opsmith.immutability)."""

    __tablename__ = "inference_runs"
    __table_args__ = (UniqueConstraint("service_id", "slot"),)
    id: Mapped[str] = _id("inf")
    tenant_id: Mapped[str] = _tenant()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    service_id: Mapped[str] = mapped_column(ForeignKey("services.id"), index=True)
    slot: Mapped[str] = mapped_column(String(40))
    previous_run_id: Mapped[str | None] = mapped_column(String(40), default=None)
    input_bundle_ids: Mapped[list[Any]] = mapped_column(default=list)
    input_leads: Mapped[list[Any]] = mapped_column(default=list)
    provider: Mapped[str] = mapped_column(String(50), default="")
    model: Mapped[str] = mapped_column(String(100), default="")
    prompt_id: Mapped[str] = mapped_column(String(100), default="")
    prompt_version: Mapped[str] = mapped_column(String(20), default="")
    output: Mapped[dict[str, Any]] = mapped_column(default=dict)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="running")
    error: Mapped[str | None] = mapped_column(Text, default=None)
    started_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column(default=None)


class Lead(Base):
    __tablename__ = "leads"
    id: Mapped[str] = _id("lead")
    tenant_id: Mapped[str] = _tenant()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    service_id: Mapped[str] = mapped_column(ForeignKey("services.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    hypothesis: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(20), default="open", index=True)
    age: Mapped[int] = mapped_column(Integer, default=0)
    max_carry: Mapped[int] = mapped_column(Integer, default=3)
    signal: Mapped[str | None] = mapped_column(String(200), default=None, index=True)
    evidence_refs: Mapped[list[Any]] = mapped_column(default=list)
    context: Mapped[str] = mapped_column(Text, default="")
    suspected_path: Mapped[str | None] = mapped_column(String(500), default=None)
    source: Mapped[str] = mapped_column(String(30), default="inference")
    task_id: Mapped[str | None] = mapped_column(String(40), default=None)
    originating_run_id: Mapped[str | None] = mapped_column(String(40), default=None)
    last_run_id: Mapped[str | None] = mapped_column(String(40), default=None)
    finding_id: Mapped[str | None] = mapped_column(String(40), default=None)
    escalated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class AuditRun(Base):
    __tablename__ = "audit_runs"
    id: Mapped[str] = _id("aud")
    tenant_id: Mapped[str] = _tenant()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id"), index=True)
    audit_types: Mapped[list[Any]] = mapped_column(default=list)
    mode: Mapped[str] = mapped_column(String(20), default="full")
    commit_sha: Mapped[str | None] = mapped_column(String(64), default=None)
    base_sha: Mapped[str | None] = mapped_column(String(64), default=None)
    files_analyzed: Mapped[int] = mapped_column(Integer, default=0)
    tool_versions: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(20), default="running")
    error: Mapped[str | None] = mapped_column(Text, default=None)
    findings_count: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    started_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column(default=None)


class Finding(Base):
    __tablename__ = "findings"
    id: Mapped[str] = _id("fnd")
    tenant_id: Mapped[str] = _tenant()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    source: Mapped[str] = mapped_column(String(80))  # inference | audit:<analyzer>
    source_run_id: Mapped[str | None] = mapped_column(String(40), default=None, index=True)
    lead_id: Mapped[str | None] = mapped_column(String(40), default=None)
    category: Mapped[str] = mapped_column(String(30))
    rule_id: Mapped[str | None] = mapped_column(String(120), default=None)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(10), default="medium")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    evidence_refs: Mapped[list[Any]] = mapped_column(default=list)
    repository_id: Mapped[str | None] = mapped_column(String(40), default=None)
    service_id: Mapped[str | None] = mapped_column(String(40), default=None)
    path: Mapped[str | None] = mapped_column(String(500), default=None)
    line: Mapped[int | None] = mapped_column(Integer, default=None)
    signal: Mapped[str | None] = mapped_column(String(200), default=None)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    evidence_strength: Mapped[float] = mapped_column(Float, default=1.0)
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    status_reason: Mapped[str | None] = mapped_column(String(100), default=None)
    task_id: Mapped[str | None] = mapped_column(String(40), default=None, index=True)
    extra: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _created()


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = _id("task")
    tenant_id: Mapped[str] = _tenant()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    category: Mapped[str] = mapped_column(String(30))
    priority: Mapped[str] = mapped_column(String(5), default="P3")
    score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    severity: Mapped[str] = mapped_column(String(10), default="medium")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    status: Mapped[str] = mapped_column(String(20), default="proposed", index=True)
    problem_statement: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[list[Any]] = mapped_column(default=list)
    proposed_approach: Mapped[str] = mapped_column(Text, default="")
    acceptance_criteria: Mapped[list[Any]] = mapped_column(default=list)
    repository_id: Mapped[str | None] = mapped_column(String(40), default=None)
    paths: Mapped[list[Any]] = mapped_column(default=list)
    branch_name: Mapped[str] = mapped_column(String(200), default="")
    risk_assessment: Mapped[str] = mapped_column(Text, default="")
    effort_estimate: Mapped[str] = mapped_column(String(5), default="M")
    verification: Mapped[dict[str, Any]] = mapped_column(default=dict)
    provenance: Mapped[dict[str, Any]] = mapped_column(default=dict)
    evidence_strength: Mapped[float] = mapped_column(Float, default=1.0)
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    protection: Mapped[str] = mapped_column(String(20), default="none")  # none|elevated|excluded
    rejection_reason: Mapped[str | None] = mapped_column(String(50), default=None)
    execution_report: Mapped[dict[str, Any]] = mapped_column(default=dict)
    pr_url: Mapped[str | None] = mapped_column(String(500), default=None)
    pr_number: Mapped[int | None] = mapped_column(Integer, default=None)
    ci_status: Mapped[str | None] = mapped_column(String(20), default=None)
    merged_at: Mapped[datetime | None] = mapped_column(default=None)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class TaskFingerprint(Base):
    """All finding fingerprints that a task covers - the dedup index."""

    __tablename__ = "task_fingerprints"
    __table_args__ = (UniqueConstraint("task_id", "fingerprint"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = _tenant()
    project_id: Mapped[str] = mapped_column(String(40), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)


class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[str] = _id("rev")
    tenant_id: Mapped[str] = _tenant()
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    reviewer_name: Mapped[str] = mapped_column(String(200))
    reviewer_role: Mapped[str] = mapped_column(String(20))
    decision: Mapped[str] = mapped_column(String(30))
    edits: Mapped[dict[str, Any]] = mapped_column(default=dict)
    reason_code: Mapped[str | None] = mapped_column(String(50), default=None)
    comment: Mapped[str] = mapped_column(Text, default="")
    elevated_ack: Mapped[bool] = mapped_column(Boolean, default=False)
    task_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = _created()


class SuppressionRule(Base):
    __tablename__ = "suppression_rules"
    id: Mapped[str] = _id("sup")
    tenant_id: Mapped[str] = _tenant()
    project_id: Mapped[str] = mapped_column(String(40), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    reason_code: Mapped[str] = mapped_column(String(50))
    severity_at_rejection: Mapped[str] = mapped_column(String(10))
    evidence_strength_at_rejection: Mapped[float] = mapped_column(Float)
    sources_at_rejection: Mapped[list[Any]] = mapped_column(default=list)
    task_id: Mapped[str] = mapped_column(String(40))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _created()


class ExecutionRun(Base):
    __tablename__ = "execution_runs"
    id: Mapped[str] = _id("exe")
    tenant_id: Mapped[str] = _tenant()
    project_id: Mapped[str] = mapped_column(String(40), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    review_id: Mapped[str] = mapped_column(String(40))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    agent: Mapped[str] = mapped_column(String(50), default="")
    sandbox_id: Mapped[str | None] = mapped_column(String(100), default=None)
    status: Mapped[str] = mapped_column(String(20), default="running")
    branch: Mapped[str | None] = mapped_column(String(200), default=None)
    commit_sha: Mapped[str | None] = mapped_column(String(64), default=None)
    pr_url: Mapped[str | None] = mapped_column(String(500), default=None)
    pr_number: Mapped[int | None] = mapped_column(Integer, default=None)
    steps: Mapped[list[Any]] = mapped_column(default=list)
    checks: Mapped[list[Any]] = mapped_column(default=list)
    changed_files: Mapped[list[Any]] = mapped_column(default=list)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    started_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column(default=None)


class UsageRecord(Base):
    """Per-call LLM usage; drives budgets and cost dashboards."""

    __tablename__ = "usage_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = _tenant()
    project_id: Mapped[str] = mapped_column(String(40), index=True)
    stage: Mapped[str] = mapped_column(String(40))
    run_id: Mapped[str | None] = mapped_column(String(40), default=None, index=True)
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[str] = _id("ntf")
    tenant_id: Mapped[str] = _tenant()
    project_id: Mapped[str | None] = mapped_column(String(40), default=None, index=True)
    event: Mapped[str] = mapped_column(String(60))
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str | None] = mapped_column(String(500), default=None)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    deliveries: Mapped[list[Any]] = mapped_column(default=list)
    created_at: Mapped[datetime] = _created()


class AuditLog(Base):
    """Append-only trail of every state transition, AI output and human decision."""

    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = _tenant()
    entity_type: Mapped[str] = mapped_column(String(40), index=True)
    entity_id: Mapped[str] = mapped_column(String(40), index=True)
    action: Mapped[str] = mapped_column(String(80))
    from_state: Mapped[str | None] = mapped_column(String(30), default=None)
    to_state: Mapped[str | None] = mapped_column(String(30), default=None)
    actor_type: Mapped[str] = mapped_column(String(20), default="system")  # user|system|agent
    actor_id: Mapped[str] = mapped_column(String(80), default="system")
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
