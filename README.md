# Opsmith — Autonomous Engineering Ops Platform Specification

Project name: **Opsmith**.

## 1. Purpose

A platform that continuously ingests operational signals (logs, metrics) and source code, uses LLM-based analysis to detect problems and improvement opportunities, converts findings into prioritized engineering tasks, routes those tasks through human approval, and then dispatches approved tasks to autonomous coding subagents that implement them and open pull requests.

Core loop: **observe → analyze → propose → human approve → execute → PR**.

## 2. Goals

- Turn raw logs, metrics, and repository state into actionable, deduplicated, ranked tasks.
- Preserve analytical continuity across batch runs: each inference run starts from the unresolved leads of the previous run.
- Combine runtime evidence (logs/metrics) with static evidence (repo audits) so tasks reference both where possible.
- Require explicit human approval before any code is changed.
- Produce reviewable PRs through the repository's normal CI and review process.
- Full traceability: every PR links back to a task, its evidence, and the pipeline runs that produced it.

## 3. Non-Goals

- No autonomous merging or deployment. Merge is always a human action.
- No direct production access for AI components. Read-only ingestion only.
- No replacement of existing observability or CI tooling; the platform consumes them.

## 4. Inputs

| Input | Examples | Access |
|---|---|---|
| Repo code | Git repositories (GitHub/GitLab/Bitbucket) | Read for analysis; write access limited to branch + PR creation by executor |
| Logs | Application logs, access logs, error logs | Pull or push via collectors (Loki, Elasticsearch/OpenSearch, CloudWatch, files, Kafka) |
| Metrics | Latency, error rate, saturation, throughput, business KPIs | Prometheus, CloudWatch, Datadog, OpenTelemetry |

Each input source is configured through a pluggable **connector** interface. Connectors must be independently addable without changing pipeline code.

## 5. Pipeline Stages

### 5.1 Ingestion + ETL (logs and metrics)

- Scheduled or streaming collection per connector.
- Normalize to a common event schema (timestamp, service, environment, severity, message/metric name, value, attributes, trace/correlation IDs).
- Steps: parse, normalize, enrich (service ownership, deployment/version, repo mapping), cluster/deduplicate similar log lines (template extraction), compute metric aggregates and anomaly features (baselines, deltas vs prior window, percentile shifts).
- Output: compact, windowed **analysis bundles** per service/time window, sized to fit LLM context. Raw data stays in the data store; bundles reference it by ID.
- PII/secrets redaction occurs here, before any data reaches an LLM.

### 5.2 Batch AI Inference (every X hours, chained)

- Runs on a configurable interval X (per project/service).
- Input per run: the new analysis bundles **plus the open leads from the previous run**.
- Output per run:
  - New findings (issue, evidence references, confidence, suspected component/repo/path, suggested next investigation).
  - Updated lead set: leads that are confirmed (promoted to findings), refuted (closed), still uncertain (carried forward with updated context), or new.
- Lead lifecycle: `open → carried → confirmed | refuted | expired`. Each lead has an age counter and a max carry count; leads exceeding it expire or are escalated for human attention.
- Each run is stored as an immutable record: inputs reference, prompt/version, model, outputs, token cost.
- Chain depth must be bounded: each run receives a summarized lead state, not the full history of prior runs.

### 5.3 Scheduled Repository Audits

- Independent schedule (e.g., nightly/weekly), per repository.
- Audit types (pluggable analyzers): security (SAST, dependency/CVE scanning, secrets detection, IaC misconfig), bugs (static analysis + LLM review of risky areas), compliance (license checks, policy-as-code rules, coding standards), performance (hot-path review, N+1 patterns, inefficient queries), dependency health, test coverage gaps.
- Combine deterministic tools (e.g., Semgrep, Trivy, OSV-Scanner, gitleaks, SpotBugs, ruff/ESLint as applicable) with LLM triage that filters false positives and adds context.
- Output: normalized findings in the same schema as inference findings.
- Incremental mode: audit only changed files/paths between full runs where possible.

### 5.4 Aggregation and Prioritization

- Merge findings from batch inference and repo audits.
- Correlate runtime evidence with static findings (e.g., an error-log cluster pointing to a file flagged in an audit → single merged finding with higher confidence).
- Deduplicate against existing open tasks, recently closed tasks, and previously rejected tasks (rejections suppress re-proposal unless evidence materially changes).
- Score and rank: severity, confidence, blast radius, evidence strength, effort estimate, recency/frequency.

### 5.5 Task Generation

Each aggregated finding group becomes a **task** with:

- Title, category (`fix`, `security`, `performance`, `compliance`, `refactor`, `observability`, `dependency`), priority, confidence.
- Problem statement and evidence links (log clusters, metric charts/queries, audit findings, code locations).
- Proposed approach and acceptance criteria.
- Target repository, paths, suggested branch name.
- Risk assessment and estimated effort.
- Required verification (tests to add/run, metrics to watch after deploy).
- Provenance: originating pipeline run IDs and lead IDs.

### 5.6 Human Review Gate

- Tasks appear in a review queue (UI + notifications).
- Reviewer actions: approve, approve with edits (edit scope/approach/priority), reject (with reason code), defer, request more investigation (creates a lead for the next inference run).
- Nothing is executed without approval. Approval records reviewer identity and timestamp.
- Rejection reasons feed back into suppression rules and prompt context.

### 5.7 Subagent Execution

- Approved tasks enter an execution queue.
- Each task is executed by an isolated subagent in a sandbox (ephemeral container/workspace with the repository checked out; no production credentials; network restricted to what is required).
- Subagent workflow: read task → plan → implement on a new branch → run build/tests/linters → iterate on failures within a bounded budget → open a PR.
- PR contents: description derived from task (problem, evidence, approach, verification), link back to the task, list of tests run and results, risk notes.
- Failure handling: bounded retries and token/time budget; on failure, task returns to the review queue with an execution report.
- Parallelism: multiple subagents may run concurrently on different tasks; tasks touching the same files are serialized or flagged for conflict.
- After PR creation, the platform tracks PR status (CI result, review comments, merged/closed) and updates the task. Optional: subagent responds to review comments within a bounded number of iterations.
- Optional post-merge verification: watch relevant metrics/log signatures after deployment and mark the task `verified` or `regressed`.

## 6. Core Data Model (logical)

- `Project` / `Repository` / `Service` (ownership and mapping between services, repos, and signal sources)
- `Connector` (type, config, credentials reference, schedule)
- `AnalysisBundle` (service, window, references to raw data, derived features)
- `InferenceRun` (schedule slot, inputs, model, prompt version, outputs, cost, status)
- `Lead` (state, age, evidence refs, originating run, last run touched)
- `AuditRun` (repository, type, commit SHA, tool versions, status)
- `Finding` (source, category, severity, confidence, evidence refs, location)
- `Task` (see 5.5; status: `proposed → in_review → approved | rejected | deferred → executing → pr_open → merged | closed | failed`)
- `Review` (task, reviewer, decision, edits, reason)
- `ExecutionRun` (task, subagent, sandbox ID, logs, branch, PR URL, cost, outcome)
- `AuditLog` (all state transitions and actor identities)

## 7. Architecture Requirements

- Modular services/components: `connectors`, `etl`, `inference-orchestrator`, `audit-runner`, `aggregator`, `task-service`, `review-api/UI`, `executor`, `scm-integration`, `scheduler`.
- Asynchronous, queue-driven execution between stages; every stage idempotent and retryable.
- Pluggable LLM provider layer (model selection per stage; structured JSON output validated against schemas; prompt versioning).
- Scheduler supporting per-project cron/interval configuration.
- Storage: relational DB for entities and state; object storage for raw/derived artifacts; optional vector store for similarity (log templates, duplicate detection).
- Multi-tenant ready: tenant isolation on all data and credentials.
- Observability of the platform itself: metrics, structured logs, tracing, per-stage cost and latency dashboards.

## 8. Safety, Security, and Governance

- Secrets and PII redaction before LLM calls; configurable data-residency and LLM-provider allowlist.
- Least-privilege credentials: read-only for ingestion; SCM token scoped to branch push + PR creation; no merge permission.
- Executor sandbox isolation; no access to production systems or secrets.
- Prompt-injection hardening: log lines, code comments, and issue text are untrusted data; never treated as instructions; subagents cannot expand their own permissions or tool access.
- Budget controls: token/cost caps per run, per project, per day; execution time limits.
- Full audit trail for all AI outputs and human decisions; reproducibility via stored inputs and prompt/model versions.
- Protected paths configuration (e.g., CI configs, auth modules) that require elevated review or are excluded from autonomous changes.

## 9. User Interface (minimum)

- Dashboard: pipeline health, run history, cost, open leads, task funnel.
- Task review queue with evidence viewer (logs, metric charts, code diff context) and approve/edit/reject actions.
- Task detail: provenance chain from raw signal to PR.
- Configuration screens: connectors, schedules, audit types, protected paths, budgets, notification channels.
- Execution view: live subagent progress, results, PR status.

## 10. Suggested Delivery Phases

1. **Foundation**: data model, scheduler, connector interface, one log source, one metrics source, one SCM (GitHub), audit log.
2. **Analysis**: ETL + bundles, batch inference with lead carry-over, structured findings.
3. **Audits**: repo audit runner with security + dependency + one LLM-triaged bug audit.
4. **Tasks and review**: aggregation, dedup, ranking, task generation, review UI.
5. **Execution**: sandboxed subagents, PR creation, CI result tracking, failure/retry handling.
6. **Hardening**: budgets, injection defenses, multi-tenancy, post-merge verification, feedback loops from rejections.

## 11. Acceptance Criteria (MVP)

- Given logs, metrics, and one repo, the system produces ranked tasks without manual intervention.
- A lead flagged in run N is visible as input to run N+1 and is resolved or carried according to lifecycle rules.
- Duplicate and previously rejected findings do not reappear as new tasks unless evidence changes.
- No code change occurs without a recorded human approval.
- An approved task results in a PR on a new branch with description, linked evidence, and CI executed.
- Every PR traces back through task → finding → evidence → pipeline runs.
- Failures in any stage are retried or surfaced; no silent drops.

## 12. Open Decisions for the Implementer to Confirm

- Interval X default and per-service override model.
- Initial supported connectors and SCM providers.
- LLM providers/models per stage and cost ceilings.
- Language/framework for each component (the implementer selects the stack; see Section 15).
- Whether subagents may respond to PR review comments in the MVP.

## 13. Project Name

The project is named **Opsmith**. Use it for the repository, packages/namespaces, documentation, and UI branding.

## 14. Implementer Latitude

This specification describes intent and a reference design, not a rigid contract. The implementing agent is not required to follow every detail. Where it identifies a better design, approach, decomposition, sequencing, data model, or technique than the one described here, it should use that instead.

The constraint is the end goal, which must remain intact: a platform that ingests repo code, logs, and metrics; runs chained batch AI analysis with carried-over leads plus scheduled repo audits; aggregates results into prioritized tasks; requires human review and approval; and executes approved tasks through subagents that open PRs.

Rules for deviation:

- Preserve the end goal and the hard safety constraints in Section 8 (human approval before any code change, no autonomous merge, least-privilege access, redaction before LLM calls).
- Record each significant deviation and its rationale in an `ARCHITECTURE_DECISIONS.md` file (decision, alternatives considered, reason).
- Prefer the simplest design that satisfies the end goal and acceptance criteria in Section 11.

## 15. Technology Stack Selection

No stack is mandated. The implementing agent selects the languages, frameworks, datastores, queueing/messaging, scheduler, sandboxing approach, LLM integration layer, and UI technology that are most suitable for this system's requirements (reliability of chained scheduled jobs, high-volume log/metric ETL, sandboxed code execution, multi-tenancy, and maintainability).

Requirements for the selection:

- Choose each component's technology on technical merit for its role; mixing technologies is acceptable where justified.
- Document the chosen stack and the reasoning for each choice in `ARCHITECTURE_DECISIONS.md`.
- Favor mature, well-supported, open-source or widely adopted tooling, and avoid unnecessary operational complexity for the MVP.
