# Architecture Decisions

This file records the chosen stack and every significant deviation from the reference design
in [`docs/SPECIFICATION.md`](docs/SPECIFICATION.md) (spec §14–15). Each entry gives the decision,
the alternatives considered, and the reason. The end goal and the §8 hard constraints (human
approval before any code change, no autonomous merge, least privilege, redaction before LLM
calls) are preserved by every decision below.

## ADR-001 Language and core stack: Python 3.12, FastAPI, SQLAlchemy 2, Pydantic 2, uv

- **Decision.** One Python codebase for every component (connectors, ETL, inference, audits,
  aggregation, API/UI, executor). The API uses FastAPI with Pydantic v2, persistence uses
  SQLAlchemy 2.0, and dependencies are managed with `uv` and a committed `uv.lock`.
- **Alternatives.** Go for ETL and executor with TypeScript for the UI; JVM (Kotlin/Spring);
  a polyglot microservice split.
- **Reason.** The hard parts of the MVP are LLM orchestration, static analysis, git and
  sandbox handling. Python has the strongest ecosystem for all of them, including the official
  Anthropic SDK, ruff/Semgrep integrations and `ast`. A single language keeps the MVP
  operable by a small team. The components are modules with narrow interfaces, so a hot path
  (for example high-volume ETL) can later move to another language behind the same queue
  contracts.

## ADR-002 Storage: PostgreSQL in production, SQLite for dev/tests; schema via `create_all`

- **Decision.** Every entity lives in one relational schema with `tenant_id` on every row.
  SQLite is the zero-ops default, and PostgreSQL (`postgresql+psycopg://`) is the production
  target. All SQL is portable, and the acceptance suite runs on both (CI job `postgres`).
- **Alternatives.** PostgreSQL only; a document store for bundles and runs.
- **Reason.** Relational integrity matters for the provenance chain and the state machines.
  JSON columns cover semi-structured payloads.
- **Deviation and follow-up.** Tables are created with `metadata.create_all`. Alembic
  migrations must be introduced before the first production schema change.

## ADR-003 Queue: durable DB-backed job queue instead of a message broker

- **Decision.** A `jobs` table provides idempotency keys (unique), atomic compare-and-set claims,
  leases with expiry recovery, exponential backoff, a bounded attempt count, `RetryLater`
  deferrals that don't consume attempts, and a `dead` state that is audited and notified.
  Stages hand off by enqueueing in the same transaction that writes their results, which
  gives transactional-outbox semantics.
- **Alternatives.** Celery+Redis, RabbitMQ, Kafka, Temporal.
- **Reason.** The pipeline runs in hours-scale batches, not at high message rates. A DB queue
  gives exactly the guarantees the spec asks for ("idempotent and retryable", "no silent
  drops") with zero extra infrastructure, and makes enqueue atomic with state changes.
  **Revisit** when throughput needs a broker, or when long multi-step workflows would benefit
  from Temporal.

## ADR-004 Scheduler: DB-stored schedules with slot-derived idempotency keys

- **Decision.** Each project, connector, service and repository gets a schedule (interval or
  5-field cron, parsed by a small built-in parser). `Scheduler.tick` enqueues due schedules
  with the key `sched:<id>:<slot>`, so several scheduler replicas or repeated ticks never
  double-enqueue. Missed slots are coalesced into one run.
- **Alternatives.** APScheduler, Kubernetes CronJobs, Celery beat.
- **Reason.** Schedules are per-tenant data editable from the UI and API, so they belong in the
  database. Idempotent slots make the scheduler safe to run redundantly.

## ADR-005 Object storage: filesystem backend behind an `ObjectStore` protocol

- **Decision.** Normalized, redacted signal batches are stored as JSONL objects. Bundles and
  evidence reference them by key and line (`obj:<key>#L<n>`). The MVP ships a local
  filesystem backend with atomic writes and path-escape protection.
- **Alternatives.** S3/MinIO from day one.
- **Reason.** Keeping the interface small (`put_jsonl`, `iter_jsonl`, ...) makes S3/GCS a
  drop-in backend without pulling cloud SDKs into the MVP.

## ADR-006 ETL and bundles

- **Decision.** Connectors return normalized `LogEvent`/`MetricPoint` records. The pipeline
  normalizes severity, enriches with service ownership and repo mapping (auto-discovering
  unknown services, each with its own inference schedule), redacts, stores, then builds a
  per-service bundle containing Drain-style log templates (masking, clustering, stable
  fingerprints, stack-trace locations, sample messages, raw event refs) and metric features
  (mean/p50/p95/p99, baseline from a look-back window, delta %, z-score, p95 shift,
  direction-aware anomaly flag). Bundles are trimmed to a token budget.
- **Alternatives.** An external log-template service, or a vector store for similarity.
- **Reason.** Template fingerprints are stable enough to drive leads, dedup and verification
  in the MVP. The spec marks the vector store as optional, and it is deferred.

## ADR-007 LLM layer: one gateway, deterministic offline provider, Claude via the official SDK

- **Decision.** Every LLM call goes through `LLMGateway`, which enforces the provider
  allowlist (process ∩ project), per-stage model selection, a second redaction pass,
  budgets, schema validation with one corrective retry, and usage/cost recording. Prompts
  are versioned (`prompt_id@version`, stored on each run). Providers:
  - `heuristic`: a deterministic rule-based implementation of every stage contract. It is the
    default, so installs work offline (CI, demos, air-gapped). It is not a stub: its output is
    validated exactly like a model's.
  - `anthropic`: the official `anthropic` SDK (2.x). `messages.parse(output_format=<schema>)`
    handles structured stages, effort is set per stage, the default model is
    `claude-opus-5-5`, and server-side refusal fallbacks are enabled by default.
- **Alternatives.** LangChain or LiteLLM abstractions; calling only remote models.
- **Reason.** A thin, explicit gateway is easier to audit for the §8 controls than a framework,
  and the deterministic provider makes the full pipeline testable without network access.

## ADR-008 Lead lifecycle and bounded chain depth

- **Decision.** Each run receives a *summary* of active leads (context truncated to 600 chars,
  at most 5 evidence refs), never earlier runs' outputs. Leads the model does not mention are
  carried rather than dropped. Exceeding `max_carry` expires the lead and escalates it to
  humans. Lead updates for unknown IDs are ignored and audited. Evidence refs not present in
  the run's input are discarded, and the finding's confidence is discounted.
- **Reason.** This satisfies spec 5.2 continuity while bounding prompt size and containing
  hallucinated or injected output.

## ADR-009 Correlation, dedup and rejection feedback

- **Decision.** Static fingerprints are `repo|rule|path|normalized snippet`, deliberately
  without the line number, so moved code keeps its identity. Runtime fingerprints are
  `service|signal|category`. A runtime and a static finding merge when they point at the same
  file and either share the category within ±25 lines or sit within ±2 lines of each other.
  This works within a batch and across runs, in both orders. Groups matching an open task
  enrich it; re-detection of the same static finding does not inflate evidence. Groups
  matching a task closed in the last 14 days are suppressed. Groups matching a rejected task
  are suppressed unless the evidence materially changes:
  - any severity increase re-proposes the task;
  - `false_positive` needs ≥3× evidence strength or a new evidence kind;
  - `insufficient_evidence`/`too_risky`/`other` need ≥2× or a new kind;
  - `wont_fix`/`duplicate`/`out_of_scope` re-propose only on a severity increase.

  Rejections also feed the inference prompt as context.
- **Reason.** This implements spec 5.4 and acceptance criterion 3 with explainable rules.

## ADR-010 Task state machine additions

- **Decision.** The spec's states are kept, with these additions: `investigating` (a reviewer
  requested more investigation; a review-request lead runs in the next inference and returns
  the task to `in_review` with the result), `verified` and `regressed` (post-merge
  verification), and `failed → in_review` (failed executions return to the queue with a
  report). Transitions are enforced centrally in `audit_log.transition_task` and always
  audited.
- **Reason.** These make the spec's reviewer actions and post-merge verification explicit
  states rather than side flags.

## ADR-011 Executor: own tool loop, platform-owned git, two sandbox backends

- **Decision.** The coding agent runs in a manual Claude tool-use loop over a fixed tool set:
  list/read/write/replace files confined to the checkout (path escapes, `.git` and symlinks
  rejected) and named `build`/`lint`/`test` checks taken from repository config. The agent
  never gets a shell, the network, or SCM credentials. The platform clones, removes the
  remote, re-runs the checks authoritatively, applies diff gates (protected paths, secrets on
  added lines only, empty diffs), commits as the bot, pushes, and opens the PR. Sandbox
  backends are `local` (scrubbed environment, timeouts; for dev/CI) and `docker`
  (`--network none`, `--cap-drop ALL`, `no-new-privileges`, CPU/memory/pid limits).
  Concurrency is capped per project, and tasks touching the same files are serialized through
  `RetryLater`.
- **Alternatives.** Claude Agent SDK; Anthropic Managed Agents; Firecracker microVMs.
- **Reason.** Spec §8 requires that subagents cannot expand their own permissions. Owning the
  tool surface and the sandbox makes that structural rather than a matter of policy.
  MicroVMs are a hardening step after the MVP.
- **Also.** A deterministic `heuristic` agent (codemods for bare `except`, missing HTTP
  timeouts, `yaml.load`, hardcoded secrets) lets the full loop run offline.

## ADR-012 SCM integration without merge

- **Decision.** The `SCMProvider` interface exposes only checkout, branch push (the default
  branch is refused), PR create (idempotent on an existing open PR) and PR/CI status
  (check runs, falling back to commit statuses). Tokens resolve from references and travel
  in a per-command `http.extraHeader`; they are never written to `.git/config`. A local
  provider (file-backed PRs plus local CI) supports demos and tests; its `human_merge` helper
  exists only to simulate a human and is not part of the interface.
- **Open decision §12 (review comments).** In the MVP, subagents do **not** respond to PR
  review comments. The comment count is tracked on the task.

## ADR-013 Authentication, authorization and multi-tenancy

- **Decision.** Random API keys (`osk_...`) are stored as SHA-256 hashes. There are four roles
  (`viewer`, `reviewer`, `admin`, `service`). Every row is tenant-scoped, and cross-tenant
  access returns 404. The UI keeps the key in an HttpOnly, SameSite=Strict cookie and also
  rejects cross-origin form posts. CSP is `default-src 'self'`.
- **Follow-up.** OIDC/SSO for human users.

## ADR-014 UI: server-rendered Jinja2

- **Decision.** Dashboard, review queue, task detail with the full provenance chain, leads,
  executions (auto-refreshing while a run is live), configuration (JSON settings editor) and
  jobs, as server-rendered pages with no JavaScript build.
- **Reason.** This is the simplest UI that covers spec §9, and it is easy to secure with a
  strict CSP.

## ADR-015 Platform observability

- **Decision.** Structured JSON logs, a Prometheus `/metrics` endpoint (stage latency,
  LLM calls/cost/latency, ingested records, executions, HTTP), and durable per-call
  `UsageRecord`s that feed the per-stage cost and latency dashboard. Jobs carry a `trace_id`.
- **Follow-up.** OpenTelemetry tracing export.

## ADR-016 Resolution of the spec §12 open decisions

| Decision | MVP choice |
|---|---|
| Interval X | 6h default per project (`inference_interval_hours`), with a per-service override |
| Connectors / SCM | file logs, file metrics, Loki, Prometheus, push API; GitHub (+ local); more via entry points (`opsmith.connectors`, `opsmith.analyzers`) |
| LLM per stage / cost ceilings | `heuristic` by default; `claude-opus-5-5` for every stage when enabled. Defaults: 400k tokens/run, $5/run, $50/day per project |
| Stack | ADR-001 |
| PR review comments | Not in MVP (ADR-012) |

## Known limitations (MVP)

- `create_all` instead of migrations (ADR-002).
- With SQLite, a long LLM call holds the writer lock; use PostgreSQL for concurrent workers.
- The `local` sandbox does not restrict network access; use `OPSMITH_SANDBOX_BACKEND=docker`
  for untrusted repositories.
- The heuristic provider is rule-based. Real analysis quality comes from the Claude provider.
