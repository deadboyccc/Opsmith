"""Baseline: the schema of Opsmith 1.0.0.

Revision ID: 0001
Revises:
Create Date: 2026-11-16

Opsmith 1.0.0 created its tables with ``metadata.create_all``. A database created that way is
stamped with this revision when it is first opened (``opsmith.migrations.upgrade``), so this
revision must keep creating exactly that schema. Never edit it; add a new revision instead.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("entity_type", sa.String(length=40), nullable=False),
        sa.Column("entity_id", sa.String(length=40), nullable=False),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("from_state", sa.String(length=30), nullable=True),
        sa.Column("to_state", sa.String(length=30), nullable=True),
        sa.Column("actor_type", sa.String(length=20), nullable=False),
        sa.Column("actor_id", sa.String(length=80), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_log_created_at"), "audit_log", ["created_at"], unique=False)
    op.create_index(op.f("ix_audit_log_entity_id"), "audit_log", ["entity_id"], unique=False)
    op.create_index(op.f("ix_audit_log_entity_type"), "audit_log", ["entity_type"], unique=False)
    op.create_index(op.f("ix_audit_log_tenant_id"), "audit_log", ["tenant_id"], unique=False)
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("deferrals", sa.Integer(), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_by", sa.String(length=100), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=300), nullable=False),
        sa.Column("trace_id", sa.String(length=40), nullable=True),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_jobs_claim", "jobs", ["status", "run_after"], unique=False)
    op.create_index(op.f("ix_jobs_kind"), "jobs", ["kind"], unique=False)
    op.create_index(op.f("ix_jobs_tenant_id"), "jobs", ["tenant_id"], unique=False)
    op.create_table(
        "notifications",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("project_id", sa.String(length=40), nullable=True),
        sa.Column("event", sa.String(length=60), nullable=False),
        sa.Column("subject", sa.String(length=300), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("link", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("deliveries", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_notifications_project_id"), "notifications", ["project_id"], unique=False
    )
    op.create_index(
        op.f("ix_notifications_tenant_id"), "notifications", ["tenant_id"], unique=False
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name"),
    )
    op.create_index(op.f("ix_projects_tenant_id"), "projects", ["tenant_id"], unique=False)
    op.create_table(
        "suppression_rules",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("project_id", sa.String(length=40), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("reason_code", sa.String(length=50), nullable=False),
        sa.Column("severity_at_rejection", sa.String(length=10), nullable=False),
        sa.Column("evidence_strength_at_rejection", sa.Float(), nullable=False),
        sa.Column("sources_at_rejection", sa.JSON(), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_suppression_rules_fingerprint"), "suppression_rules", ["fingerprint"], unique=False
    )
    op.create_index(
        op.f("ix_suppression_rules_project_id"), "suppression_rules", ["project_id"], unique=False
    )
    op.create_index(
        op.f("ix_suppression_rules_tenant_id"), "suppression_rules", ["tenant_id"], unique=False
    )
    op.create_table(
        "usage_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("project_id", sa.String(length=40), nullable=False),
        sa.Column("stage", sa.String(length=40), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=True),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_usage_records_created_at"), "usage_records", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_usage_records_project_id"), "usage_records", ["project_id"], unique=False
    )
    op.create_index(op.f("ix_usage_records_run_id"), "usage_records", ["run_id"], unique=False)
    op.create_index(
        op.f("ix_usage_records_tenant_id"), "usage_records", ["tenant_id"], unique=False
    )
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("api_key_hash", sa.String(length=128), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("api_key_hash"),
    )
    op.create_index(op.f("ix_users_tenant_id"), "users", ["tenant_id"], unique=False)
    op.create_table(
        "findings",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("project_id", sa.String(length=40), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("source_run_id", sa.String(length=40), nullable=True),
        sa.Column("lead_id", sa.String(length=40), nullable=True),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("rule_id", sa.String(length=120), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("severity", sa.String(length=10), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("repository_id", sa.String(length=40), nullable=True),
        sa.Column("service_id", sa.String(length=40), nullable=True),
        sa.Column("path", sa.String(length=500), nullable=True),
        sa.Column("line", sa.Integer(), nullable=True),
        sa.Column("signal", sa.String(length=200), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("evidence_strength", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("status_reason", sa.String(length=100), nullable=True),
        sa.Column("task_id", sa.String(length=40), nullable=True),
        sa.Column("extra", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_findings_fingerprint"), "findings", ["fingerprint"], unique=False)
    op.create_index(op.f("ix_findings_project_id"), "findings", ["project_id"], unique=False)
    op.create_index(op.f("ix_findings_source_run_id"), "findings", ["source_run_id"], unique=False)
    op.create_index(op.f("ix_findings_status"), "findings", ["status"], unique=False)
    op.create_index(op.f("ix_findings_task_id"), "findings", ["task_id"], unique=False)
    op.create_index(op.f("ix_findings_tenant_id"), "findings", ["tenant_id"], unique=False)
    op.create_table(
        "repositories",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("project_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("scm_provider", sa.String(length=20), nullable=False),
        sa.Column("url", sa.String(length=500), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=True),
        sa.Column("default_branch", sa.String(length=100), nullable=False),
        sa.Column("credentials_ref", sa.String(length=200), nullable=True),
        sa.Column("test_command", sa.String(length=500), nullable=True),
        sa.Column("lint_command", sa.String(length=500), nullable=True),
        sa.Column("build_command", sa.String(length=500), nullable=True),
        sa.Column("audit_types", sa.JSON(), nullable=False),
        sa.Column("last_audited_sha", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "name"),
    )
    op.create_index(
        op.f("ix_repositories_project_id"), "repositories", ["project_id"], unique=False
    )
    op.create_index(op.f("ix_repositories_tenant_id"), "repositories", ["tenant_id"], unique=False)
    op.create_table(
        "schedules",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("project_id", sa.String(length=40), nullable=False),
        sa.Column("job_kind", sa.String(length=50), nullable=False),
        sa.Column("target_id", sa.String(length=40), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=True),
        sa.Column("cron", sa.String(length=100), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_enqueued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("custom", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_kind", "target_id"),
    )
    op.create_index(op.f("ix_schedules_project_id"), "schedules", ["project_id"], unique=False)
    op.create_index(op.f("ix_schedules_tenant_id"), "schedules", ["tenant_id"], unique=False)
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("project_id", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("priority", sa.String(length=5), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("severity", sa.String(length=10), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("problem_statement", sa.Text(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("proposed_approach", sa.Text(), nullable=False),
        sa.Column("acceptance_criteria", sa.JSON(), nullable=False),
        sa.Column("repository_id", sa.String(length=40), nullable=True),
        sa.Column("paths", sa.JSON(), nullable=False),
        sa.Column("branch_name", sa.String(length=200), nullable=False),
        sa.Column("risk_assessment", sa.Text(), nullable=False),
        sa.Column("effort_estimate", sa.String(length=5), nullable=False),
        sa.Column("verification", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("evidence_strength", sa.Float(), nullable=False),
        sa.Column("occurrences", sa.Integer(), nullable=False),
        sa.Column("protection", sa.String(length=20), nullable=False),
        sa.Column("rejection_reason", sa.String(length=50), nullable=True),
        sa.Column("execution_report", sa.JSON(), nullable=False),
        sa.Column("pr_url", sa.String(length=500), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("ci_status", sa.String(length=20), nullable=True),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tasks_project_id"), "tasks", ["project_id"], unique=False)
    op.create_index(op.f("ix_tasks_score"), "tasks", ["score"], unique=False)
    op.create_index(op.f("ix_tasks_status"), "tasks", ["status"], unique=False)
    op.create_index(op.f("ix_tasks_tenant_id"), "tasks", ["tenant_id"], unique=False)
    op.create_table(
        "audit_runs",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("project_id", sa.String(length=40), nullable=False),
        sa.Column("repository_id", sa.String(length=40), nullable=False),
        sa.Column("audit_types", sa.JSON(), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=True),
        sa.Column("base_sha", sa.String(length=64), nullable=True),
        sa.Column("files_analyzed", sa.Integer(), nullable=False),
        sa.Column("tool_versions", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("findings_count", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_runs_project_id"), "audit_runs", ["project_id"], unique=False)
    op.create_index(
        op.f("ix_audit_runs_repository_id"), "audit_runs", ["repository_id"], unique=False
    )
    op.create_index(op.f("ix_audit_runs_tenant_id"), "audit_runs", ["tenant_id"], unique=False)
    op.create_table(
        "execution_runs",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("project_id", sa.String(length=40), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("review_id", sa.String(length=40), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("agent", sa.String(length=50), nullable=False),
        sa.Column("sandbox_id", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("branch", sa.String(length=200), nullable=True),
        sa.Column("commit_sha", sa.String(length=64), nullable=True),
        sa.Column("pr_url", sa.String(length=500), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("checks", sa.JSON(), nullable=False),
        sa.Column("changed_files", sa.JSON(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_execution_runs_project_id"), "execution_runs", ["project_id"], unique=False
    )
    op.create_index(op.f("ix_execution_runs_task_id"), "execution_runs", ["task_id"], unique=False)
    op.create_index(
        op.f("ix_execution_runs_tenant_id"), "execution_runs", ["tenant_id"], unique=False
    )
    op.create_table(
        "reviews",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("reviewer_id", sa.String(length=40), nullable=False),
        sa.Column("reviewer_name", sa.String(length=200), nullable=False),
        sa.Column("reviewer_role", sa.String(length=20), nullable=False),
        sa.Column("decision", sa.String(length=30), nullable=False),
        sa.Column("edits", sa.JSON(), nullable=False),
        sa.Column("reason_code", sa.String(length=50), nullable=True),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("elevated_ack", sa.Boolean(), nullable=False),
        sa.Column("task_version", sa.Integer(), nullable=False),
        sa.Column("scope_digest", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["reviewer_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_reviews_task_id"), "reviews", ["task_id"], unique=False)
    op.create_index(op.f("ix_reviews_tenant_id"), "reviews", ["tenant_id"], unique=False)
    op.create_table(
        "services",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("project_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("environment", sa.String(length=50), nullable=False),
        sa.Column("owner", sa.String(length=200), nullable=True),
        sa.Column("repository_id", sa.String(length=40), nullable=True),
        sa.Column("path_prefixes", sa.JSON(), nullable=False),
        sa.Column("inference_interval_hours", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "name", "environment"),
    )
    op.create_index(op.f("ix_services_project_id"), "services", ["project_id"], unique=False)
    op.create_index(op.f("ix_services_tenant_id"), "services", ["tenant_id"], unique=False)
    op.create_table(
        "task_fingerprints",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("project_id", sa.String(length=40), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "fingerprint"),
    )
    op.create_index(
        op.f("ix_task_fingerprints_fingerprint"), "task_fingerprints", ["fingerprint"], unique=False
    )
    op.create_index(
        op.f("ix_task_fingerprints_project_id"), "task_fingerprints", ["project_id"], unique=False
    )
    op.create_index(
        op.f("ix_task_fingerprints_task_id"), "task_fingerprints", ["task_id"], unique=False
    )
    op.create_index(
        op.f("ix_task_fingerprints_tenant_id"), "task_fingerprints", ["tenant_id"], unique=False
    )
    op.create_table(
        "analysis_bundles",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("project_id", sa.String(length=40), nullable=False),
        sa.Column("service_id", sa.String(length=40), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("log_templates", sa.JSON(), nullable=False),
        sa.Column("metric_features", sa.JSON(), nullable=False),
        sa.Column("raw_refs", sa.JSON(), nullable=False),
        sa.Column("stats", sa.JSON(), nullable=False),
        sa.Column("token_estimate", sa.Integer(), nullable=False),
        sa.Column("consumed_by_run_id", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_analysis_bundles_consumed_by_run_id"),
        "analysis_bundles",
        ["consumed_by_run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_analysis_bundles_project_id"), "analysis_bundles", ["project_id"], unique=False
    )
    op.create_index(
        op.f("ix_analysis_bundles_service_id"), "analysis_bundles", ["service_id"], unique=False
    )
    op.create_index(
        op.f("ix_analysis_bundles_tenant_id"), "analysis_bundles", ["tenant_id"], unique=False
    )
    op.create_table(
        "connectors",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("project_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("type", sa.String(length=50), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("credentials_ref", sa.String(length=200), nullable=True),
        sa.Column("service_id", sa.String(length=40), nullable=True),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("cursor", sa.JSON(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "name"),
    )
    op.create_index(op.f("ix_connectors_project_id"), "connectors", ["project_id"], unique=False)
    op.create_index(op.f("ix_connectors_tenant_id"), "connectors", ["tenant_id"], unique=False)
    op.create_table(
        "inference_runs",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("project_id", sa.String(length=40), nullable=False),
        sa.Column("service_id", sa.String(length=40), nullable=False),
        sa.Column("slot", sa.String(length=40), nullable=False),
        sa.Column("previous_run_id", sa.String(length=40), nullable=True),
        sa.Column("input_bundle_ids", sa.JSON(), nullable=False),
        sa.Column("input_leads", sa.JSON(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("prompt_id", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=20), nullable=False),
        sa.Column("output", sa.JSON(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("service_id", "slot"),
    )
    op.create_index(
        op.f("ix_inference_runs_project_id"), "inference_runs", ["project_id"], unique=False
    )
    op.create_index(
        op.f("ix_inference_runs_service_id"), "inference_runs", ["service_id"], unique=False
    )
    op.create_index(
        op.f("ix_inference_runs_tenant_id"), "inference_runs", ["tenant_id"], unique=False
    )
    op.create_table(
        "leads",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("project_id", sa.String(length=40), nullable=False),
        sa.Column("service_id", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("age", sa.Integer(), nullable=False),
        sa.Column("max_carry", sa.Integer(), nullable=False),
        sa.Column("signal", sa.String(length=200), nullable=True),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("context", sa.Text(), nullable=False),
        sa.Column("suspected_path", sa.String(length=500), nullable=True),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=True),
        sa.Column("originating_run_id", sa.String(length=40), nullable=True),
        sa.Column("last_run_id", sa.String(length=40), nullable=True),
        sa.Column("finding_id", sa.String(length=40), nullable=True),
        sa.Column("escalated", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_leads_project_id"), "leads", ["project_id"], unique=False)
    op.create_index(op.f("ix_leads_service_id"), "leads", ["service_id"], unique=False)
    op.create_index(op.f("ix_leads_signal"), "leads", ["signal"], unique=False)
    op.create_index(op.f("ix_leads_state"), "leads", ["state"], unique=False)
    op.create_index(op.f("ix_leads_tenant_id"), "leads", ["tenant_id"], unique=False)
    op.create_table(
        "raw_batches",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("project_id", sa.String(length=40), nullable=False),
        sa.Column("connector_id", sa.String(length=40), nullable=False),
        sa.Column("service_id", sa.String(length=40), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("object_key", sa.String(length=500), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("redactions", sa.JSON(), nullable=False),
        sa.Column("bundle_id", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["connector_id"],
            ["connectors.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_raw_batches_bundle_id"), "raw_batches", ["bundle_id"], unique=False)
    op.create_index(
        op.f("ix_raw_batches_connector_id"), "raw_batches", ["connector_id"], unique=False
    )
    op.create_index(op.f("ix_raw_batches_project_id"), "raw_batches", ["project_id"], unique=False)
    op.create_index(op.f("ix_raw_batches_service_id"), "raw_batches", ["service_id"], unique=False)
    op.create_index(op.f("ix_raw_batches_tenant_id"), "raw_batches", ["tenant_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_raw_batches_tenant_id"), table_name="raw_batches")
    op.drop_index(op.f("ix_raw_batches_service_id"), table_name="raw_batches")
    op.drop_index(op.f("ix_raw_batches_project_id"), table_name="raw_batches")
    op.drop_index(op.f("ix_raw_batches_connector_id"), table_name="raw_batches")
    op.drop_index(op.f("ix_raw_batches_bundle_id"), table_name="raw_batches")
    op.drop_table("raw_batches")
    op.drop_index(op.f("ix_leads_tenant_id"), table_name="leads")
    op.drop_index(op.f("ix_leads_state"), table_name="leads")
    op.drop_index(op.f("ix_leads_signal"), table_name="leads")
    op.drop_index(op.f("ix_leads_service_id"), table_name="leads")
    op.drop_index(op.f("ix_leads_project_id"), table_name="leads")
    op.drop_table("leads")
    op.drop_index(op.f("ix_inference_runs_tenant_id"), table_name="inference_runs")
    op.drop_index(op.f("ix_inference_runs_service_id"), table_name="inference_runs")
    op.drop_index(op.f("ix_inference_runs_project_id"), table_name="inference_runs")
    op.drop_table("inference_runs")
    op.drop_index(op.f("ix_connectors_tenant_id"), table_name="connectors")
    op.drop_index(op.f("ix_connectors_project_id"), table_name="connectors")
    op.drop_table("connectors")
    op.drop_index(op.f("ix_analysis_bundles_tenant_id"), table_name="analysis_bundles")
    op.drop_index(op.f("ix_analysis_bundles_service_id"), table_name="analysis_bundles")
    op.drop_index(op.f("ix_analysis_bundles_project_id"), table_name="analysis_bundles")
    op.drop_index(op.f("ix_analysis_bundles_consumed_by_run_id"), table_name="analysis_bundles")
    op.drop_table("analysis_bundles")
    op.drop_index(op.f("ix_task_fingerprints_tenant_id"), table_name="task_fingerprints")
    op.drop_index(op.f("ix_task_fingerprints_task_id"), table_name="task_fingerprints")
    op.drop_index(op.f("ix_task_fingerprints_project_id"), table_name="task_fingerprints")
    op.drop_index(op.f("ix_task_fingerprints_fingerprint"), table_name="task_fingerprints")
    op.drop_table("task_fingerprints")
    op.drop_index(op.f("ix_services_tenant_id"), table_name="services")
    op.drop_index(op.f("ix_services_project_id"), table_name="services")
    op.drop_table("services")
    op.drop_index(op.f("ix_reviews_tenant_id"), table_name="reviews")
    op.drop_index(op.f("ix_reviews_task_id"), table_name="reviews")
    op.drop_table("reviews")
    op.drop_index(op.f("ix_execution_runs_tenant_id"), table_name="execution_runs")
    op.drop_index(op.f("ix_execution_runs_task_id"), table_name="execution_runs")
    op.drop_index(op.f("ix_execution_runs_project_id"), table_name="execution_runs")
    op.drop_table("execution_runs")
    op.drop_index(op.f("ix_audit_runs_tenant_id"), table_name="audit_runs")
    op.drop_index(op.f("ix_audit_runs_repository_id"), table_name="audit_runs")
    op.drop_index(op.f("ix_audit_runs_project_id"), table_name="audit_runs")
    op.drop_table("audit_runs")
    op.drop_index(op.f("ix_tasks_tenant_id"), table_name="tasks")
    op.drop_index(op.f("ix_tasks_status"), table_name="tasks")
    op.drop_index(op.f("ix_tasks_score"), table_name="tasks")
    op.drop_index(op.f("ix_tasks_project_id"), table_name="tasks")
    op.drop_table("tasks")
    op.drop_index(op.f("ix_schedules_tenant_id"), table_name="schedules")
    op.drop_index(op.f("ix_schedules_project_id"), table_name="schedules")
    op.drop_table("schedules")
    op.drop_index(op.f("ix_repositories_tenant_id"), table_name="repositories")
    op.drop_index(op.f("ix_repositories_project_id"), table_name="repositories")
    op.drop_table("repositories")
    op.drop_index(op.f("ix_findings_tenant_id"), table_name="findings")
    op.drop_index(op.f("ix_findings_task_id"), table_name="findings")
    op.drop_index(op.f("ix_findings_status"), table_name="findings")
    op.drop_index(op.f("ix_findings_source_run_id"), table_name="findings")
    op.drop_index(op.f("ix_findings_project_id"), table_name="findings")
    op.drop_index(op.f("ix_findings_fingerprint"), table_name="findings")
    op.drop_table("findings")
    op.drop_index(op.f("ix_users_tenant_id"), table_name="users")
    op.drop_table("users")
    op.drop_index(op.f("ix_usage_records_tenant_id"), table_name="usage_records")
    op.drop_index(op.f("ix_usage_records_run_id"), table_name="usage_records")
    op.drop_index(op.f("ix_usage_records_project_id"), table_name="usage_records")
    op.drop_index(op.f("ix_usage_records_created_at"), table_name="usage_records")
    op.drop_table("usage_records")
    op.drop_index(op.f("ix_suppression_rules_tenant_id"), table_name="suppression_rules")
    op.drop_index(op.f("ix_suppression_rules_project_id"), table_name="suppression_rules")
    op.drop_index(op.f("ix_suppression_rules_fingerprint"), table_name="suppression_rules")
    op.drop_table("suppression_rules")
    op.drop_index(op.f("ix_projects_tenant_id"), table_name="projects")
    op.drop_table("projects")
    op.drop_index(op.f("ix_notifications_tenant_id"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_project_id"), table_name="notifications")
    op.drop_table("notifications")
    op.drop_index(op.f("ix_jobs_tenant_id"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_kind"), table_name="jobs")
    op.drop_index("ix_jobs_claim", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index(op.f("ix_audit_log_tenant_id"), table_name="audit_log")
    op.drop_index(op.f("ix_audit_log_entity_type"), table_name="audit_log")
    op.drop_index(op.f("ix_audit_log_entity_id"), table_name="audit_log")
    op.drop_index(op.f("ix_audit_log_created_at"), table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("tenants")
