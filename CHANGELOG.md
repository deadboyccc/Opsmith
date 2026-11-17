# Changelog

Notable changes to Opsmith. The format follows [Keep a Changelog](https://keepachangelog.com/);
versions follow [Semantic Versioning](https://semver.org/) from 1.0.0. A release that changes
the database schema says so under *Upgrade notes*.

## 1.2.4 - 2026-11-17

Documentation only. Every page was compared with the code as it now is.

### Changed

- The README states what is now tested (coverage and its floor; Claude through the real SDK
  over a mocked transport) and links the security audit and the decision log. It no longer
  lists a connector compatibility matrix as open work: the matrix exists.
- Compatibility: how Claude is tested, and the FastAPI versions supported.
- Deployment: the values `OPSMITH_LOG_LEVEL` accepts.
- Operations: troubleshooting entries for the refusals added in 1.2.1 and 1.2.2 (a
  repository saved with an address or branch git must not be given, build output that trips
  the credential gate, a deferred audit, an oversized request), and the retries of a busy
  model endpoint.

## 1.2.3 - 2026-11-17

Tests and continuous integration. No change to what Opsmith does, apart from the one fix below.

### Changed

- **CI fails when test coverage drops below 93%** (`fail_under` in `pyproject.toml`). Coverage
  is 94.5% (it was 92% at 1.2.0 and 93% at 1.2.2, measured the same way).
- **CI audits the locked dependencies on every push** with `pip-audit` (new `make audit`), so a
  newly published advisory turns the next build red.
- **Workflow actions are pinned to a commit**, and the live-model workflow to an Ollama
  release instead of `latest`, so a moved tag cannot change what CI runs.
- Coverage is measured by path, so `src/opsmith/migrations/env.py` (which alembic loads as a
  file) is counted; it was reported as never run.

### Added

- `tests/test_runtime.py`: the worker and scheduler loops, including their recovery after an
  error and their stop signal; JSON logging; the object store; the Docker sandbox's failure
  paths; provider plugins; notifications without a project; the `alembic` command.
- Tests for the GitHub provider's git commands, token handling, API failures and the
  commit-status fallback for CI.

### Fixed

- **A write to the object store that failed half-way left its `.tmp` file behind** for good.
  It is now removed, and a complete file is synced to disk before it is published.
- A test of the lease heartbeat tolerated less than a second of scheduling delay; it now
  tolerates nearly three, and still fails when the heartbeat stops.

## 1.2.2 - 2026-11-17

A security release: the result of an audit of the whole code base
([Security audit](docs/SECURITY-AUDIT.md)). Upgrade if tenant admins other than yourself
configure projects, or if the API port is reachable by anyone you do not trust.

### Upgrade notes

- **A `github` repository's `url` must be an `https://` URL without credentials, and every
  repository's `default_branch` a plain branch name.** A repository that does not meet this
  is refused when it is next used (its audits and executions fail with a message naming the
  field) until it is corrected.
- **Request bodies are limited** to 1 MiB, and to 32 MiB for pushed signals. A larger request
  gets `413`.
- **A file connector's `path` may not contain `..`.**
- **Protected-path patterns cover more than before**: a pattern now also covers everything
  below a directory it names. Nothing that was protected stops being protected. A pattern that
  can never match is refused when settings are saved.
- **A change that adds a file containing something credential-like is now refused even when
  the file is binary.** A check command that leaves build output behind (compiled bytecode
  holding a string from the source, for example) can trip this; ignore such files in the
  repository's `.gitignore`.
- A review's `comment` and `investigation` are limited to 20,000 characters. A redaction
  pattern must match at least one character.

### Security

- **A repository's address and branch were handed to git unchecked.** A `github` repository
  could name a directory on the worker, which made Opsmith clone any repository the worker
  can read, another tenant's included, past `OPSMITH_LOCAL_ROOT`. A branch such as
  `--upload-pack=<command>` ran that command on the worker, outside the sandbox, at the
  repository's second audit. Both values are now checked when saved (API and `opsmith apply`)
  and again on every use; git is told where its options end and, for GitHub, may use https
  only.
- **A credential placed after a form feed passed the push-time credential scan.** The diff
  was split at form feeds, bare carriage returns and the Unicode separators as well as at
  newlines, and the rest of the line was then never scanned.
- **Protected-path patterns such as `infra/`, `/infra/**` or `./infra/**` matched nothing**,
  so a directory its admin believed protected was not.
- **A file-connector pattern could leave the local root** with a `..` after a wildcard, and
  the refusal then named the file it had found: file names outside the root could be read one
  pattern at a time. Contents were not exposed.
- **Request bodies had no size limit** and are read before the key is checked.
- **`/metrics` grew without bound**: the HTTP method was a label as the client sent it.
- **Redaction missed credentials in common spellings**: JSON carried inside a JSON string,
  `*_PASS` and `--pass`, `Authorization` with schemes other than Bearer and Basic, `Cookie`
  headers, OpenAI project keys, PGP private key blocks, and fields named in the plural.
- **A connector request had no total deadline**, only a timeout for each read.
- The validation report quoted on a corrective retry could close its own data block. Review
  text was unbounded. A redaction pattern that matched the empty string was accepted.

### Fixed

- **Epoch timestamps in milliseconds were masked as credit cards** one time in ten, which
  inflated the redaction counts and changed log templates from one line to the next.

### Added

- [`docs/SECURITY-AUDIT.md`](docs/SECURITY-AUDIT.md): what the audit looked for, what it
  found, what was fixed and what is still open.

## 1.2.1 - 2026-11-17

A bug-fix release. The whole code base was read for defects; each fix below has a test that
failed before it. No database change.

### Upgrade notes

- **Some settings are now limited to the size of the column they are stored in**:
  `audit_cron` and a schedule's `cron` to 100 characters, `executor_agent` and a stage's
  `provider` to 50, a stage's `model` to 100. PostgreSQL already refused longer values, later
  and with a server error. A project whose stored settings exceed a limit (possible on SQLite
  only) must be corrected, through `opsmith apply` or `PATCH /projects/{id}/settings`.
- **`OPSMITH_LOG_LEVEL` must be a level name** (`DEBUG`, `INFO`, `WARNING`, `ERROR`,
  `CRITICAL`, in either case). Anything else now stops the process at start with a
  configuration message instead of a traceback.
- **FastAPI 0.121 or newer is required** (the lock file has 0.163).
- A connector created without `interval_seconds` now takes the project's
  `ingest_interval_seconds` instead of 900. With the default settings nothing changes.

### Added

- `service_environment` on a connector in `opsmith.toml`, to link it to a service that
  exists in more than one environment.
- `environment` in the `config` of the `prometheus` and `loki` connectors.

### Fixed

Execution and review:

- **With the real Claude SDK, every turn of the Claude coding agent failed before a request
  was sent.** The SDK refuses a non-streaming call above about 21,000 `max_tokens` unless the
  caller sets the timeout itself, and the agent's default is 32,000. A stage configured above
  that limit failed the same way. The timeout is now passed explicitly. The tests used a
  hand-written client, which is why this was not seen; one now runs the real SDK.
- **A tool call cut off by `max_tokens` broke the Claude agent's conversation.** The
  half-written `tool_use` block was answered with "Continue.", which the API rejects because
  the block has no `tool_result`. It is now answered as an error, and never run. A tool input
  that is not an object crashed the loop and lost the record of what the run had spent.
- **An agent edit rewrote every line ending of a CRLF file**, so a one-line fix became a diff
  of the whole file, and the credential scan then treated every old line as newly added.
  Edits now keep the file's line endings.
- **A work directory that could not be created left the task in `executing` for good.** It now
  fails the attempt, and the task returns to review with a report.
- **The `execution.serialized` audit entry was never stored**: it was written in the
  transaction that the deferral then rolled back.
- **A list or object as the value of an edited field was a server error** (`priority`,
  `effort_estimate`). A review's `reason_code` was only checked for rejections; any other
  decision stored it unchecked, which PostgreSQL refuses above 50 characters.
- **A plain Approve in the UI was recorded as an approval with edits** when the task's title
  held a line break, and the title was rewritten.

Jobs and notifications:

- **A dead job only notified its project for three of the nine job kinds.** The others
  (ingest, analysis, audit, execute, verify) carry no project id, so their `job.dead`
  notification was logged and never reached the project's webhook.
- **A job dead-lettered for being deferred too often kept its lease.**
- **Two audits of one repository could run on the same checkout** (a manual trigger during a
  scheduled run), the second resetting the tree under the first. The second now waits.
- **The per-project and per-service locks blocked everything that references the locked
  row** on PostgreSQL. While a model drafted tasks, nothing could be ingested, analysed or
  reviewed in that project, and a review that opens a lead could deadlock with the
  aggregation. The locks no longer block inserts.

Ingestion. One record that an earlier stage accepted and a later one could not process used
to stop a connector, or a service's bundles, for good: every retry and every later job met
it again.

- A message holding half a surrogate pair or a NUL; a `file:line` reference with thousands
  of digits; a severity such as `²` (a digit to Python, not a number); a service or
  environment name with a NUL; two metric points of 1e308; `lag_seconds` or
  `baseline_seconds` too large for a duration. Each is now cleaned, or dropped and counted in
  `skipped_records`.
- **A pushed timestamp at the edge of the calendar was a server error** (which log shippers
  retry forever). It is a `422`.
- **About one in two hundred quoted epoch-millisecond timestamps was read as a date in the
  year 17xx** and the record lost without being counted: Python reads `1795011232359` as the
  ISO basic format.
- **A connector linked to a service passed on only the service's name**, so records without
  an environment went to a new `production` service of that name. The Prometheus connector
  could not produce metrics for any other environment at all.
- **`ingest_interval_seconds` had no effect**: every connector was created with 900 seconds
  of its own.
- **A few error lines with very long file paths removed every log template from a bundle.**
- Each connector run opened an HTTP client and never closed it. A connector registered
  through an entry point had no `type_name`, so its error messages raised `AttributeError`.

Models:

- **An SDK's own exception (rate limit, overload, connection) passed the stages that handle
  model errors.** The job was rolled back together with the usage of the calls already
  billed, and task drafting's fallback never ran. Every provider failure now reaches the
  stages as a model error.
- **The built-in rules' own output could fail validation**: a metric series with a few labels
  made a title longer than 300 characters, the bundle stayed unconsumed, and every later
  analysis of that service failed on it.
- **A NUL in model output failed the run on PostgreSQL**, after the call had been billed.
- **One 429 or 5xx from an OpenAI-compatible endpoint failed the call at once**, which for
  analysis is the whole slot. The client now tries twice more.
- **A reply without token usage was recorded as free**, so the budgets never saw it. Content
  given as a list of parts was read as the text of a Python list.
- Input that Claude reads from or writes to its prompt cache was not counted (no request
  uses the cache yet, so nothing was mis-billed).

Audits and source control:

- **A file name that is not UTF-8 made its repository impossible to audit.** The audit failed
  after every analyzer had run, on every attempt.
- **Line numbers were wrong in a file that contains a form feed** (or a Unicode line
  separator): findings pointed at the wrong line, carried another line's snippet, and the
  built-in fixes could not find their line.
- **GitHub CI status read only the first page of check runs**, so a commit whose failing or
  unfinished run was listed after the first 30 was reported as passing.

API, UI and command line:

- **A write was acknowledged before it was committed.** A commit that failed (a dropped
  connection, a constraint) still answered `201` with the id of a row that was never stored.
- **`/docs` was blank**: the strict Content-Security-Policy blocked the assets it loads.
  `/docs` and `/redoc` now get a policy that lets them; every other path keeps the strict one.
- **`opsmith apply` checked far less than the API.** A value of the wrong type ended in a
  traceback or was stored as it was; services were looked up by name alone, so with one name
  in two environments every connector was linked to the last one in the file.
- An id containing a NUL was a server error on PostgreSQL instead of `404`. Deeply nested
  settings JSON in the UI was a server error.

## 1.2.0 - 2026-11-16

### Security

- **Code planted in a nested repository could run on the worker.** If the repository has a
  submodule, sandboxed code could fill it with a repository whose configuration defines a
  filter driver; the platform's `git status` after the checks descended into it and ran the
  driver outside the sandbox. A nested `.git` anywhere in the workspace now refuses the
  execution before the platform runs git on it, and the agent cannot write one.
- **A `-diff` or `binary` git attribute hid a file from the credential scan.** The staged diff
  is now read as text.
- **A tenant could send the platform's model API key to its own URL** when the operator stored
  that key as a file in the secrets directory (Docker secrets): a connector's
  `credentials_ref` could name the same file. That file, and links to it, are now reserved.
- **The API accepted the UI's session cookie for state-changing calls** without the UI's
  `Origin` check, so a form on a sibling subdomain could, for example, resubmit a task. The
  cookie now works for reads only; changes need the key in a header.
- **Values nested under a credential-named field were stored unredacted**, for example
  `{"password": {"new": "..."}}`. Everything under such a field is now masked.
- **An audited repository could reconfigure gitleaks and trivy.** A committed `.gitleaks.toml`
  with an allowlist of `.*` hid every leaked secret; a committed `trivy.yaml` with `output:`
  made trivy write its report to a path of the repository's choosing on the worker and leave
  nothing to parse, which read as "no findings". gitleaks now gets the platform's own
  configuration and trivy runs from an empty directory. Verified against gitleaks 8.30.1 and
  trivy 0.76.2.
- **Files whose name starts with `.opsmith-home` skipped the protected-path gate**, and a
  sandbox home replaced by a link or file could be committed. The home is now matched by exact
  path and always removed before the gates.

### Added

- A [compatibility matrix](docs/guide/compatibility.md): what Opsmith talks to, which versions
  are verified and how. The Loki and Prometheus connectors now run against real servers in CI
  on every push (Loki 2.9.17 and 3.7.10; Prometheus v2.53.5, v3.5.5 and v3.16.0), through
  `tests/test_connectors_live.py`.
- A one-minute demonstration video (`docs/images/demo.mp4`, with a GIF preview in the README),
  recorded from the offline demo by `scripts/demo_video.py`: a reviewer approves a task in the
  UI and it goes through to `verified`.
- A mascot and a social preview image (`docs/images/brand/`).

### Fixed

- **A worker that had lost its lease could hand its job to a third worker.** When its handler
  then failed or asked to be retried later, it requeued the job although another worker was
  already running it. Every write after the handler now checks, under a row lock, that the
  worker still owns the job, and lease recovery skips a lease that is being renewed.
- **Two workers aggregating the same project at once created duplicate tasks** (and so
  duplicate pull requests) on PostgreSQL: both read the same new findings and both drafted a
  task. Aggregation now locks the project, and an analysis run locks its service, so they run
  one at a time per project and per service.
- **A task merged a second time was never verified.** The verify job was keyed by task, so
  after a regression and a new pull request the second merge reused the first, finished job.
- **Post-merge verification gave up silently** after four hours without data, which with the
  default daily audit left most code-only tasks `merged` for good. It now waits up to 48
  hours and then fails the job (dead, audited, notified) instead of succeeding quietly.
- **With the real Claude SDK, refusals, truncated replies and out-of-bound values bypassed the
  budgets.** `messages.parse` validates the reply inside the SDK and raised a pydantic error
  before the provider could read the stop reason, so the billed usage was never recorded, the
  gateway's corrective retry never ran, and the whole job was retried (and billed) again. The
  provider now sends the same schema through `messages.create` and the gateway validates the
  reply. The tests run the real SDK over a mock transport instead of a hand-written fake.
- **Settings too large to use were accepted** and then failed inside every later job, for
  example `recently_closed_days = 1000000000` (a `timedelta` overflow in aggregation) or a
  schedule interval beyond PostgreSQL's `INTEGER`. Durations, intervals, counts and budgets now
  have upper bounds, checked when saved through the API or `opsmith apply`
  ([Configuration](docs/guide/configuration.md#projects-and-settings)).
- **The gitleaks analyzer never produced a finding with current gitleaks.** It asked for the
  report on `/dev/stdout`, which gitleaks 8.30 does not write to; every audit recorded a
  gitleaks failure. It now uses `-`, gitleaks' documented name for stdout.
- **One unstorable record stopped a connector for good.** A record nested more than 255
  levels deep, or with a service name or environment longer than its column (on PostgreSQL),
  failed the whole window, which was then retried until the job died, and nothing after it
  was ingested. Such records are now dropped and counted in the run's `skipped_records`.
- **Loki JSON lines lost fields.** A line with both `message` and `msg` (or `level` and
  `severity`) dropped the second one; it is now kept in the attributes.
- **The Prometheus connector could read past the end of its window.** It rounded the window's
  times to the millisecond, so an end such as `…36.1766` became `…36.177` and Prometheus
  returned a point that also belonged to the next window. Times are now truncated. Found by
  running the connector against real Prometheus servers.

## 1.1.0 - 2026-11-16

### Upgrade notes

- **Opsmith now migrates its database.** Every process brings the schema to the newest
  revision when it starts; there is no separate step. A database created by 1.0.0 is adopted
  as it is, with its data. Stop all processes and take a backup before upgrading
  ([Operations](docs/guide/operations.md#upgrading)).
- A process now refuses to start, with a `database schema:` message, on a database from before
  1.0.0 or one migrated by a newer release.

### Added

- Schema migrations with Alembic (`src/opsmith/migrations/`; ADR-002). Revision `0001` is the
  1.0.0 schema. On PostgreSQL, processes that start together are serialized by an advisory
  lock. `tests/test_migrations.py` fails when the models and the revisions disagree, on SQLite
  and PostgreSQL. New dependency: `alembic`.

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
