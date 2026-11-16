# Architecture Decisions

This file records the chosen stack and every significant deviation from the reference design
in [the specification](docs/development/specification.md) (spec §14–15). Each entry gives the decision,
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
  target. All SQL is portable, and the whole test suite runs on both (CI job `postgres`; each
  test gets its own database).
- **Alternatives.** PostgreSQL only; a document store for bundles and runs.
- **Reason.** Relational integrity matters for the provenance chain and the state machines.
  JSON columns cover semi-structured payloads.
- **Deviation and follow-up.** Tables are created with `metadata.create_all`. Alembic
  migrations must be introduced before the first production schema change. Until then a
  release that changes the schema needs a fresh database and says so in `CHANGELOG.md`.

## ADR-003 Queue: durable DB-backed job queue instead of a message broker

- **Decision.** A `jobs` table provides idempotency keys (unique), atomic compare-and-set claims,
  leases, exponential backoff, a bounded attempt count, `RetryLater` deferrals that don't
  consume attempts, and a `dead` state that is audited and notified. Stages hand off by
  enqueueing in the same transaction that writes their results, which gives
  transactional-outbox semantics.
- **Leases.** A worker renews its job's lease from a heartbeat thread for as long as it is
  alive, so the lease bounds how long a *dead* worker's job waits, not how long a job may run.
  Without the heartbeat, an execution longer than the lease was handed to a second worker
  while the first was still pushing. A worker that finds its lease gone does not overwrite the
  new owner's bookkeeping. A job whose worker keeps dying is dead-lettered once its attempts
  are used up, because the normal failure path never runs for it.
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
- **Robustness.** Schedules for every tenant are processed in one tick, so one schedule must
  never be able to stop the rest: a schedule whose next run cannot be computed is disabled and
  audited, and settings that would produce one (an impossible cron date, a zero interval) are
  rejected when they are saved. A timing change takes effect immediately rather than after
  one more run on the old cadence. Timing set directly on a schedule through the API is marked
  `custom` and is not overwritten when project settings change later.
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
  direction-aware anomaly flag). Bundles are trimmed to a token budget, dropping
  unremarkable metric rows before any log template.
- **Input is hostile or broken until proven otherwise.** A record that cannot be parsed is
  skipped and counted instead of failing the fetch, because a connector whose fetch fails
  never advances and re-reads the same poison forever. Timestamps are normalized to UTC,
  non-finite metric values are dropped, message sizes are capped before any regular
  expression runs over them, and bundle building is isolated per service. Pull connectors
  read up to a short lag behind the present so late-arriving records are not skipped, cap how
  far one run catches up, and page through results instead of truncating them. Pending
  batches are claimed atomically so two builders cannot bundle the same data.
- **Alternatives.** An external log-template service, or a vector store for similarity.
- **Reason.** Template fingerprints are stable enough to drive leads, dedup and verification
  in the MVP. The spec marks the vector store as optional, and it is deferred.

## ADR-007 LLM layer: one gateway, a small provider interface, no vendor lock-in

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
  - `openai`: any OpenAI-compatible Chat Completions endpoint (OpenAI, Azure OpenAI, Ollama,
    vLLM, LM Studio, LiteLLM, OpenRouter, ...) over plain HTTP, with no vendor SDK. It asks
    for `json_schema` output and falls back to plain JSON mode with the schema in the prompt,
    and adapts to servers that want `max_completion_tokens`. The gateway validates the result
    either way.
  - Others can be added as plugins (the `opsmith.llm_providers` entry point).
- **Coding agents** follow the same split: `heuristic`, `claude` and `openai` all drive one
  fixed tool set through one dispatcher (ADR-011).
- **Where the endpoint is configured.** The base URL and key of the `openai` provider are
  operator settings. A project chooses a provider name and a model, never a URL, so project
  configuration cannot redirect model traffic (compare ADR-018).
- **Prices.** A stage may carry `input_price_per_mtok` / `output_price_per_mtok`; they take
  precedence over the built-in list. Without them, a model that is not in the list is costed
  at the most expensive known rate rather than at zero.
- **Accounting.** Every billed call is recorded, including ones that fail: a refusal, a
  truncated response, or output that never validates. An analysis run whose model call fails
  ends as `failed` and commits, so its cost counts against the caps; otherwise a persistently
  failing stage could spend without limit. A model id with no listed price is costed as the
  configured model, or at the most expensive known rate, never at zero. The per-run token cap
  reserves the full output allowance and also applies to calls outside a run.
- **Framing.** Untrusted payloads are serialized so they cannot contain the closing
  `</untrusted_data>` tag. The gateway redacts identifiers too, so callers match the model's
  citations against the redacted form the model actually saw.
- **Alternatives.** LangChain or LiteLLM as the abstraction (LiteLLM remains usable *behind*
  the `openai` provider as a gateway); calling only remote models; one SDK per vendor.
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
  This works within a batch and across runs, in both orders, and never across repositories.
  Groups matching an open task enrich it; re-detection of the same static finding does not
  inflate evidence, whether it arrives in a later batch or several times in one. When a group
  matches both an open task and a newer closed one, the open task wins.
- **Approved tasks are frozen.** A group matching a task that is `approved` or `executing` is
  left untouched (its findings stay `new`) and attaches once the execution is over. Evidence
  carries model-chosen paths and severities, and letting it rewrite the scope of a task a
  human has already approved would defeat the approval. When evidence grows the paths of a
  task still in review, its protection level is re-evaluated and its version is bumped, so a
  reviewer acting on a stale copy is refused. Groups
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
  added lines only, empty diffs), commits as the bot, pushes, and opens the PR. Concurrency is
  capped per project, and tasks touching the same files are serialized through `RetryLater`.
- **Sandbox backends and policy.** `docker` is the default: one container per command with
  `--network none`, a read-only root, `--cap-drop ALL`, `no-new-privileges`, a non-root user,
  CPU/memory/pid limits and only the checkout mounted. `local` is an unconfined subprocess for
  development, tests and the demo. The policy is enforced, not advised: `local` is refused for
  any repository outside the `local` SCM provider, and an unavailable Docker daemon refuses
  the execution rather than falling back.
- **The checks are untrusted code, and so is anything the agent can make them run.** The agent
  can write a test, and the test command executes it, so "no shell" does not mean "no code
  execution inside the sandbox". Two consequences are designed for:
  - *Git metadata is kept out of reach.* The checkout's `.git` is writable from the sandbox, and
    hooks, `core.fsmonitor` or filter drivers planted there would run in the worker on the
    next `git commit`. The platform therefore snapshots `.git` before the agent starts and
    runs every later git command against that snapshot only (`GIT_DIR`), with hooks disabled.
  - *Gates run last.* The protected-path and credential gates run after the platform's own
    check run, with nothing sandboxed in between, so what is gated is what is committed.
    Changed files and added lines are read from git's unambiguous (NUL-delimited) output:
    file names and file contents are attacker-controlled.
- **Compose.** A containerized worker has no sandbox by default. An explicit overlay mounts
  the host's Docker socket and a shared work directory. That makes the worker container
  privileged, which is stated where the overlay is enabled; it is opt-in so that no deployment
  executes repository code without someone having chosen how it is isolated.
- **Alternatives.** Claude Agent SDK; Anthropic Managed Agents; Firecracker microVMs; keeping
  `.git` outside the checkout altogether (rejected: test suites that call git would break).
- **Reason.** Spec §8 requires that subagents cannot expand their own permissions. Owning the
  tool surface and the sandbox makes that structural rather than a matter of policy.
  MicroVMs are a hardening step after the MVP.
- **Also.** A deterministic `heuristic` agent (codemods for bare `except`, missing HTTP
  timeouts, `yaml.load`, hardcoded secrets) lets the full loop run offline.

## ADR-012 SCM integration without merge

- **Decision.** The `SCMProvider` interface exposes only checkout, branch push, PR create
  (idempotent on an existing open PR) and PR/CI status (check runs, falling back to commit
  statuses). Pushes are refused for the default branch and for anything outside the
  `opsmith/` namespace, and branch names are validated. Within that namespace pushes are
  forced: the branch belongs to Opsmith, and a retry after a failed PR creation must be able
  to replace the commit an earlier attempt pushed. Tokens resolve from references and travel
  in a per-command `http.extraHeader`; they are never written to `.git/config`. A local
  provider (file-backed PRs plus local CI) supports demos and tests; its `human_merge` helper
  exists only to simulate a human and is not part of the interface.
- **Open decision §12 (review comments).** In the MVP, subagents do **not** respond to PR
  review comments. The comment count is tracked on the task.

## ADR-013 Authentication, authorization and multi-tenancy

- **Decision.** Random API keys (`osk_...`) are stored as SHA-256 hashes. There are four roles
  (`viewer`, `reviewer`, `admin`, `service`). Every row is tenant-scoped, and cross-tenant
  access returns 404. Read access requires a human role; a `service` key, which ships with log
  collectors, can only push signals. The UI keeps the key in an HttpOnly, SameSite=Strict
  cookie and also rejects cross-origin form posts. CSP is `default-src 'self'`.
- **What a tenant is.** Tenancy isolates data. It is not a security boundary between hostile
  parties, because all tenants share the workers, and a tenant's admins configure what those
  workers fetch and run. See ADR-018 for the limits placed on that, and `docs/guide/security.md`
  for what remains.
- **Follow-up.** OIDC/SSO for human users; key expiry and rotation.

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
| Connectors / SCM | file logs, file metrics, Loki, Prometheus, push; GitHub (+ local); more via entry points (`opsmith.connectors`, `opsmith.analyzers`, `opsmith.llm_providers`) |
| LLM per stage / cost ceilings | `heuristic` by default; any OpenAI-compatible model or Claude per stage when configured. Defaults: 400k tokens/run, $5/run, $50/day per project, covering every stage including the coding agent |
| Stack | ADR-001 |
| PR review comments | Not in MVP (ADR-012) |

## ADR-017 An approval is bound to the scope that was reviewed

- **Decision.** When a reviewer approves, the review stores a SHA-256 digest of everything that
  determines what the agent will do and where: task id, repository, branch, title, category,
  problem statement, approach, acceptance criteria, paths, protection level and the ids of the
  attached findings. `assert_approved` recomputes it before the execution starts and again
  immediately before the push. Any difference voids the approval; the task returns to review
  with the reason. Decisions on one task are serialized with a row lock, so a double click or
  an approve racing a reject cannot both be recorded as current.
- **Alternatives.** Compare the task `version` counter (rejected: it also changes on harmless
  transitions, such as `approved → executing`); snapshot the whole task into the review and
  execute from the snapshot (more state, same guarantee); bind to the base commit as well
  (needs a checkout at review time).
- **Reason.** "A human approved this task" is only meaningful if the task cannot change
  between the approval and the push. Before this, later evidence could add paths to an
  approved task, and the second approval check ran after the branch had been pushed.
- **Limit.** The digest covers the plan, not the diff, which does not exist yet. The diff is
  constrained by the gates (ADR-011) and reviewed by a human in the pull request, whose body
  records the base commit.

## ADR-018 Limits on what project configuration can make a worker touch

- **Decision.** Project configuration is written by tenant admins and consumed by shared
  workers, so three things it can name are restricted:
  - *Host files.* File connectors and `local` repositories must resolve inside
    `OPSMITH_LOCAL_ROOT` and outside the data directory. Unset, both are disabled.
  - *Credentials.* A `credentials_ref` may be `env:NAME` or `file:/path`. `env:` may not name
    `OPSMITH_*` variables, the database password or the LLM key; `file:` must be inside
    `OPSMITH_SECRETS_DIR`. The resolved secret is sent to a URL the same admin chose, so an
    unrestricted reference would let them exfiltrate the platform's own secrets.
  - *URLs.* Connector URLs must be plain http(s) without credentials, query or fragment, and
    may not resolve to link-local addresses (cloud metadata). Responses are size-bounded and
    upstream bodies are kept out of error messages.
- **Not done, deliberately.** Private and loopback addresses stay allowed, because that is
  where self-hosted Loki and Prometheus live. Credential references are one namespace shared
  by all tenants. Both are why tenancy is described as a data boundary only (ADR-013).
- **Alternatives.** Per-tenant credential namespaces and an egress allowlist. Both are the
  right design for hostile multi-tenancy and more than the MVP needs.

## Known limitations (MVP)

- `create_all` instead of migrations (ADR-002). A schema change needs a fresh database.
- With SQLite, a long LLM call or audit holds the writer lock; use PostgreSQL for concurrent
  workers.
- The Docker sandbox is a container, not a VM, and one sandbox image serves every repository a
  worker handles. The image must contain each project's dependencies, because the sandbox has
  no network.
- A containerized worker needs the Docker socket to start sandboxes (ADR-011).
- The `local` sandbox isolates nothing. It is refused for real repositories.
- Tenants share workers, credential references and network reach (ADR-013, ADR-018).
- Regular expressions supplied by admins (redaction patterns, policy rules, file-connector
  patterns) are trusted; a pathological one can stall a worker.
- Log templates are mined per window, so the same message can get a different template, and
  therefore a different signal, when its neighbours change.
- Leads age on every analysis run, including runs with no new data, so a lead can expire
  during an ingestion outage. Rejection thresholds compare each new batch of evidence with
  the evidence at rejection; they do not accumulate across batches.
- The heuristic provider is rule-based. Real analysis quality comes from the Claude provider,
  and has not been measured: there is no evaluation set yet.
- GitHub, Loki and Prometheus integrations are tested against mocked HTTP only.
