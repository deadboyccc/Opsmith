# Changelog

Notable changes to Opsmith. The format follows [Keep a Changelog](https://keepachangelog.com/);
versions follow [Semantic Versioning](https://semver.org/) from 1.0.0. A release that changes
the database schema says so under *Upgrade notes*.

## 1.0.0 - 2026-11-16

The first stable release. A hardening release that follows the
[independent project review](docs/development/independent-project-review.md). It adds no product features.
[The review response](docs/development/review-response.md) maps the review's items to this work.

### Upgrade notes

- **The database schema changed** (`reviews.scope_digest`, `schedules.custom`). There are no
  migrations yet, so start from a fresh database.
- **`POSTGRES_PASSWORD` is now required** by `docker-compose.yml`. Set it in `.env`.
- **The sandbox default is now `docker`.** For development with local repositories set
  `OPSMITH_SANDBOX_BACKEND=local`. The `local` sandbox is refused for GitHub repositories.
- **File connectors and local repositories are disabled until you set `OPSMITH_LOCAL_ROOT`** to
  the directory they may read.
- **`file:` credential references must live under `OPSMITH_SECRETS_DIR`** (default
  `/run/secrets`), and `env:` references may not name `OPSMITH_*`, database or LLM-key variables.
- **`service` API keys can no longer read anything.** Use a `viewer` key for read access.
- **Approvals recorded before this release are not honoured:** they carry no scope digest.
  Approve the task again.
- **Sandbox images must contain the project's dependencies.** The sandbox has no network, so a
  check command that installs packages fails.
- `OPSMITH_LLM_PROVIDER` was never read and has been removed from `.env.example`.

### Security

- **Approvals are bound to the work.** An approval stores a digest of the task's scope and is
  re-checked before the run and before the push (the second check used to come after the push).
  A changed task voids the approval and returns to review.
- **The aggregator no longer rewrites approved tasks.** New evidence, including model-chosen
  paths and severities, could change the paths, severity and priority of a task a human had
  already approved. It now waits until the execution is over.
- **Protected-path gate bypass fixed.** A file named like `dir/x -> y` was reported as `y`.
  Changed files are now read from git's NUL-delimited output.
- **Credential gate bypass fixed.** An added line starting with `++ ` was mistaken for a diff
  header and never scanned.
- **Files written by the platform's check run are now gated.** They used to be committed after
  the gates had already run.
- **Sandbox escape through git closed.** Sandboxed code could plant hooks or configuration in the
  checkout's `.git`, which the worker executed when committing. Git metadata is now snapshotted
  before the agent starts and only the snapshot is used afterwards, with hooks disabled.
- **Sandbox policy is enforced.** `docker` is the default; `local` is refused for non-local
  repositories; a missing Docker daemon refuses the execution instead of downgrading.
- **Docker sandbox hardened:** read-only root filesystem, size-limited `/tmp`, no swap, an init
  process, kill-by-name on timeout. The local sandbox kills the whole process group on timeout.
- **Compose fails closed on the database password,** and `opsmith init` no longer prints it.
- **Credential references are restricted** so a connector or repository cannot send the
  platform's own secrets, or arbitrary files, to a URL its admin chose.
- **Host filesystem access is confined** to `OPSMITH_LOCAL_ROOT` for file connectors and local
  repositories. A tenant could previously ingest another tenant's stored signals by pointing a
  file connector at the data directory.
- **Connector URLs are validated;** link-local and cloud-metadata addresses are refused, response
  sizes and timeouts are bounded, and upstream response bodies no longer appear in error text.
- **`service` keys are limited to pushing signals,** as documented. They could read tasks,
  configuration and the audit log.
- **Prompt framing cannot be closed by its content.** A log line containing the closing
  `</untrusted_data>` tag ended the untrusted block early.
- **External scanners:** file names can no longer be read as options by `ruff` or `semgrep`;
  `ruff` ignores configuration shipped in the repository; tools run with a scrubbed environment.
- **The secrets analyzer masks every secret on a line.** A second secret on the same line was
  stored in clear text in the finding.
- **Redaction covers more shapes:** quoted JSON keys, prefixed keys such as `DB_PASSWORD`, URL
  credentials without a username, `Authorization: Basic`, values of sensitive-named keys in
  structured attributes, and card numbers next to other digits.
- **UI:** logout is a POST with an origin check; stored URLs are only rendered as links when
  they are http(s); login errors are fixed messages; the origin check works behind a
  TLS-terminating proxy.
- Reviews of the same task are serialized, every editable field is validated, branch names are
  restricted to plain `opsmith/...` paths, and pushes outside that namespace are refused.

### Fixed

- **The coding agent's spending was outside the cost budgets.** It was recorded on the
  execution run only, so the daily cap and the per-run cost cap ignored the most expensive
  stage. It is now written to the same ledger as every other stage; an execution does not
  start a model-driven agent when the day's budget is spent, and the agent stops at the per-run
  cap or what is left of the day. A model call that fails part-way keeps what was already
  spent.
- **The demo failed when run twice within one second** against the same database (this made CI
  fail intermittently).
- **Costs were lost when a model call failed.** Failed, refused and truncated calls were billed
  but not recorded, so budget caps could not trip. They are now recorded and the run is marked
  `failed`.
- **An unlisted model id was priced at $0,** disabling cost caps. It is now priced as the
  configured model, or at the most expensive known rate.
- **The per-run token cap** now holds for calls outside a run and reserves the full output
  allowance.
- **A slow job could be run twice.** Leases were not renewed, so a job longer than the lease was
  handed to a second worker. Workers now renew the lease while they are alive.
- **A job that kept crashing its worker was retried forever.** It is now dead-lettered.
- **One unusable schedule stopped every schedule** for every tenant. It is now disabled and
  audited. Impossible cron expressions and zero intervals are rejected when settings are saved.
- **Schedule changes took effect one run late,** and timing set through the API was overwritten
  by the next settings change.
- **A refused execution left its task parked in `approved`.** It now returns to review.
- **A retry after a failed PR creation could never push** to GitHub, because the branch already
  existed. Opsmith's own branches are now force-pushed.
- **A rejected task was re-proposed on every audit** after a severity re-rating, with nothing
  changed on it.
- Repeated detections of the same static finding no longer inflate evidence strength.
- Runtime evidence is no longer attached to a task in a different repository with the same path.
- An open task now wins over a newer closed one when both match new evidence.
- Blast radius and confidence are computed on attach the same way as at creation.
- Model output is bounded (line numbers, signal and path lengths), an out-of-range
  `finding_index` no longer binds a lead to another lead's finding, `from_lead_id` is honoured,
  and repeated evidence references no longer add strength.
- Redaction in the gateway no longer makes the model's valid citations look fabricated.
- **Ingestion:** one malformed record no longer wedges a connector; non-finite metric values and
  naive timestamps no longer break bundling; Loki results beyond the page limit are no longer
  dropped; overlapping jobs no longer bundle or ingest the same data twice; late-arriving
  records are picked up through an ingestion lag; catch-up windows are capped; metric series
  whose labels are redacted stay distinct; a flat baseline no longer hides drops.
- **Audits:** one malformed file no longer discards an analyzer's findings for the whole
  repository; a failed analyzer no longer advances the audit baseline; scanner failures are no
  longer recorded as clean runs; PEM private keys are detected; the default policy glob matches
  root-level files; colliding findings are all kept; large repositories no longer overflow the
  scanners' argument list.
- **UI:** the tasks and jobs pages no longer show a macro object in the title; validation errors
  say what is wrong; a plain Approve from a browser is recorded as `approve`.
- The API returns `409` for duplicates instead of `500`, bounds `limit`, and refuses links
  across projects. Validation errors with custom messages no longer cause a `500`.
- `opsmith apply` prints a clear error for an invalid file and writes nothing.
- Workers and the scheduler survive a temporary database outage, and a worker stops after its
  current job on `SIGTERM`.
- The demo can be run twice against the same database.

### Added

- `docker-compose.sandbox.yml`: an explicit overlay that lets a containerized worker use the
  Docker sandbox. The runtime image now includes the Docker CLI.
- **No model vendor lock-in:** an `openai` provider and coding agent for any OpenAI-compatible
  endpoint (OpenAI, Azure OpenAI, Ollama, vLLM, LiteLLM, ...), per-stage prices
  (`input_price_per_mtok`, `output_price_per_mtok`), and provider plugins through the
  `opsmith.llm_providers` entry point. Verified live against Ollama.
- `push_logs` and `push_metrics` connector types for sources that push instead of being polled.
- Settings: `OPSMITH_WORK_DIR`, `OPSMITH_LOCAL_ROOT`, `OPSMITH_SECRETS_DIR`, `OPSMITH_BASE_URL`,
  `OPSMITH_OPENAI_BASE_URL`, `OPSMITH_OPENAI_API_KEY_REF`, `OPSMITH_OPENAI_JSON_MODE`.
- Adversarial tests (`tests/test_hardening.py` and others), tests that start real containers,
  and the whole suite on PostgreSQL in CI.
- A provider parity test: the acceptance scenario runs with every model stage and the coding
  agent going through the OpenAI-compatible HTTP path and must end exactly as the offline run.
- A [UI tour](docs/guide/ui-tour.md) with screenshots generated from the offline demo
  (`scripts/ui_screenshots.py`).
- Documentation, split by reader ([index](docs/README.md)): a user guide under `docs/guide/`
  (getting started, UI tour, concepts, configuration, models, deployment, operations, API,
  security) and contributor pages under `docs/development/` (architecture, testing), plus
  `CONTRIBUTING.md` and this file. `scripts/check_docs.py` runs in CI and compares the docs with
  the code: links, anchors, cited tests, environment variables, project settings, API
  endpoints, notification events, connector types, states, providers and commands.
- A weekly and on-demand workflow that runs the OpenAI-compatible provider and coding agent
  against a real Ollama server.

## 0.1.0

The initial MVP: ingestion and ETL, chained batch analysis with carried-over leads, repository
audits, aggregation and ranking, the human review gate, sandboxed execution, pull requests with
CI tracking, post-merge verification, the REST API and UI, and the offline demo.
