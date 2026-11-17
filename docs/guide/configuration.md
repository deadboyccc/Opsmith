# Configuration

Opsmith has two layers of configuration:

| Layer | Set by | Where | Examples |
|---|---|---|---|
| **Process settings** | The operator | `OPSMITH_*` environment variables | Database, sandbox, data directory |
| **Project configuration** | Tenant admins | A TOML file, the API, or the UI | Repositories, connectors, intervals, budgets, protected paths |

This page covers project configuration. Process settings are listed in
[Deployment](deployment.md#environment-variables).

- [The configuration file](#the-configuration-file)
- [Projects and settings](#projects-and-settings)
- [Repositories](#repositories)
- [Services](#services)
- [Connectors](#connectors)
- [Protected paths](#protected-paths)
- [Budgets](#budgets)
- [Models](#models)
- [Policy rules and licenses](#policy-rules-and-licenses)
- [Notifications](#notifications)
- [Redaction](#redaction)
- [Credentials](#credentials)
- [Schedules](#schedules)

## The configuration file

Describe your projects in one TOML file and apply it. Applying is safe to repeat: records are
matched by name and updated in place.

```bash
opsmith apply opsmith.toml --tenant acme
```

A minimal file:

```toml
[[projects]]
name = "payments"

[[projects.repositories]]
name = "payments-api"
scm_provider = "github"
url = "https://github.com/acme/payments-api.git"
full_name = "acme/payments-api"
credentials_ref = "env:GITHUB_TOKEN"     # a reference, never the secret itself
test_command = "pytest -q"

[[projects.services]]
name = "payments-api"
repository = "payments-api"
path_prefixes = ["app/"]

[[projects.connectors]]
name = "loki"
type = "loki"
service = "payments-api"
config = { url = "http://loki:3100", query = '{service_name="payments-api"}' }
```

[`opsmith.example.toml`](../../opsmith.example.toml) is a complete, annotated example.

If anything in the file is invalid, `opsmith apply` prints what is wrong and writes nothing.
Unknown keys are errors, so a typo cannot be silently ignored.

Nothing removes records: deleting an entry from the file does not delete it from Opsmith.
Disable a connector with `enabled = false` instead.

## Projects and settings

A project groups repositories, services and connectors that belong together, and carries the
settings below. All are optional; the defaults are shown.

```toml
[projects.settings]
inference_interval_hours = 6        # how often each service is analyzed
lead_max_carry = 3                  # runs a lead may stay undecided before it is escalated
audit_cron = "0 2 * * *"            # when repositories are audited (UTC)
audit_full_every_days = 7           # a full audit at least this often; changed files only in between
ingest_interval_seconds = 900       # for connectors that set no interval_seconds of their own
pr_poll_seconds = 600               # how often open pull requests are checked
recently_closed_days = 14           # suppress findings for a problem fixed this recently
min_task_confidence = 0.3           # findings below this do not become tasks
max_tasks_per_aggregation = 25      # the rest wait for the next aggregation
auto_submit_for_review = true       # put new tasks straight into the review queue
post_merge_verification = true      # watch the signal after a merge
verification_delay_hours = 6        # how long to wait after the merge before judging
executor_agent = "heuristic"        # or "claude"
osv_online = false                  # look up dependency vulnerabilities at osv.dev
```

| Setting | Allowed values |
|---|---|
| `inference_interval_hours` | Greater than 0, at most 8760 (a year) |
| `lead_max_carry` | 1 to 100 |
| `audit_full_every_days` | 1 to 3650 |
| `max_tasks_per_aggregation` | 1 to 1000 |
| `audit_cron` | Five fields: minute, hour, day of month, month, day of week. Lists, ranges and steps are supported; names such as `MON` are not. An expression that can never fire (31 February) is rejected |
| `ingest_interval_seconds` | 60 to 31536000 (a year) |
| `pr_poll_seconds` | 60 to 604800 (a week) |
| `recently_closed_days` | 0 to 3650 |
| `verification_delay_hours` | 0 to 8760 |
| `budgets.*` | Positive; at most 10^8 tokens, $100,000 per run, $1,000,000 per day, 86400 execution seconds, 1000 agent iterations, 100 concurrent executions |
| `min_task_confidence` | 0 to 1 |

Values are checked when you save them. A bad value is refused with a message; it cannot be
stored and fail later inside a job.

`osv_online = true` sends the names and versions of your dependencies to `api.osv.dev`. Leave
it off if that must not leave your network.

`checks` holds the project's checks: standing instructions a model carries out against the
code on a schedule, such as a weekly security review. See [Checks](checks.md).

## Repositories

```toml
[[projects.repositories]]
name = "payments-api"
scm_provider = "github"               # "github" or "local"
url = "https://github.com/acme/payments-api.git"
full_name = "acme/payments-api"       # owner/repo; derived from the URL when omitted
default_branch = "main"
credentials_ref = "env:GITHUB_TOKEN"
build_command = "python -m compileall -q app"
lint_command = "ruff check ."
test_command = "pytest -q"
audit_types = ["security", "bugs", "dependency", "compliance", "performance"]
```

| Key | Meaning |
|---|---|
| `scm_provider` | `github`, or `local` for a git repository on the worker's disk (development and the demo). A `local` repository's `url` is a path and must be inside `OPSMITH_LOCAL_ROOT` |
| `url` | For `github`: an `https://` clone URL, without credentials in it. Nothing else is accepted (no path, `file://`, `ssh://` or `http://`), because the value is handed to git on the worker |
| `default_branch` | A plain branch name: letters, digits, `.`, `_`, `-` and `/`, starting with a letter or digit, at most 100 characters |
| `credentials_ref` | Where the token comes from. See [Credentials](#credentials) and the [token permissions](deployment.md#connecting-a-github-repository) |
| `build_command`, `lint_command`, `test_command` | The checks. Each is optional. They run **only inside the sandbox**, where there is **no network**: they must work with what the sandbox image already contains. Do not put `pip install` or `npm ci` in them |
| `audit_types` | Which audits run: `security`, `bugs`, `compliance`, `performance`, `dependency`, `coverage` |

The agent runs these checks while it works, and Opsmith runs them again itself before it opens
a pull request. A pull request is only opened when every configured check passes. A repository
with no checks configured gets pull requests that nothing verified, so configure at least
`test_command`.

## Services

A service is a running thing that emits signals. Mapping it to a repository is what lets
Opsmith connect a production symptom to code.

```toml
[[projects.services]]
name = "payments-api"
environment = "production"            # default
repository = "payments-api"           # the repository it is built from
owner = "team-payments"
path_prefixes = ["app/"]              # the parts of the repository this service owns
inference_interval_hours = 1          # overrides the project's interval for this service
```

A signal that names a service Opsmith has not seen creates that service automatically, with its
own analysis schedule. Map it to a repository afterwards to get code-level correlation.

## Connectors

A connector is a source of signals. Each belongs to a project and usually to a service.

```toml
[[projects.connectors]]
name = "loki"
type = "loki"                         # loki | victorialogs | prometheus | file_logs | file_metrics | push_logs | push_metrics
service = "payments-api"              # default service for records that do not name one
service_environment = "production"    # which "payments-api", when it exists in several environments
interval_seconds = 900                # at least 60; left out, the project's ingest_interval_seconds
credentials_ref = "env:LOKI_TOKEN"    # optional; sent as a Bearer token
enabled = true
config = { url = "http://loki:3100", query = '{service_name="payments-api"}' }
```

Common behaviour of the pull connectors:

- Each run reads from where the last one stopped up to **60 seconds before now**
  (`lag_seconds` in `config`), so records that arrive slightly late are not skipped.
- After an outage a run catches up at most 24 hours at a time and continues on the next run.
- A record that cannot be parsed is skipped and counted in the job result. It does not stop
  the rest. The same goes for a record that cannot be stored: a service or environment name
  that is too long or holds a NUL, a metric value that is not finite or is beyond 1e150, or
  attributes nested more than 255 levels deep.
- Very long messages are truncated to 16 KiB and marked as truncated. A NUL or half a
  surrogate pair in a text is replaced by U+FFFD.
- A record without an environment gets the connector's `environment` (in `config`). When
  that is not set and the connector is linked to a service, it gets that service's.
- The connector's HTTP client is closed after every run.

### `loki`

| `config` key | Default | Meaning |
|---|---|---|
| `url` | required | Base URL of Loki |
| `query` | required | LogQL selector |
| `limit` | 5000 | Entries per page. Results are paged, up to 20 pages per run; a window with more than that is continued on the next run rather than cut off |
| `service_label` | `service_name` | The stream label that names the service |
| `environment` | the linked service's, else `production` | Environment for streams without an `env` or `environment` label |
| `timeout` | 30 | Seconds for each read, at most 120. A whole request may take four times as long |
| `lag_seconds` | 60 | See above |

A line that is JSON is read for its level, message and other fields.

### `prometheus`

| `config` key | Default | Meaning |
|---|---|---|
| `url` | required | Base URL of Prometheus |
| `queries` | required | A list of `{ name = "...", promql = "..." }`. `name` becomes the metric name |
| `step` | `60s` | Query resolution |
| `baseline_seconds` | 21600 | How far back to read for the baseline each run, at most 30 days |
| `service_label` | `service` | The label that names the service |
| `environment` | the linked service's, else `production` | Environment of the metrics this connector reads |
| `timeout`, `lag_seconds` | 30, 60 | As above |

Name metrics so that the direction is clear. Names containing words like `error`, `latency` or
`duration` are treated as "higher is worse"; `success`, `availability` or `throughput` as
"lower is worse".

### `file_logs` and `file_metrics`

Read files on the worker. For development, tests and simple setups. The path must be inside
`OPSMITH_LOCAL_ROOT`; with that variable unset, these connectors are disabled.

| `config` key | Default | Meaning |
|---|---|---|
| `path` | required | A file or glob, for example `/var/log/app/*.jsonl`. It may not contain `..` |
| `format` (logs) | `jsonl` | `jsonl`, or `text` for plain lines |
| `timestamp_field`, `message_field`, `severity_field`, `service_field` (logs, jsonl) | `timestamp`, `message`, `level`, `service` | Field names in your JSON |
| `pattern` (logs, text) | `timestamp LEVEL [service] message` | A regular expression with named groups. `timestamp` is required; `severity`, `service` and `message` are optional |
| `environment` | the linked service's, else `production` | Environment for records that do not carry one |
| `baseline_seconds` (metrics) | 21600 | Look-back for the baseline, at most 30 days |

Metric files are JSON lines with `timestamp`, `name`, `value` and optionally `service` and
`labels`. A record without a timestamp is skipped and counted.

### `victorialogs`

Reads [VictoriaLogs](https://docs.victoriametrics.com/victorialogs/) through its LogsQL query
API (`/select/logsql/query`), read-only.

| `config` key | Default | Meaning |
|---|---|---|
| `url` | required | Base URL of VictoriaLogs, for example `http://victorialogs:9428` |
| `query` | `*` (everything) | A LogsQL filter, for example `app_name:(backend OR caddy)`. Opsmith adds the time window and the ordering itself |
| `service_field` | `app_name` | The field that names the service. `app_name` is the syslog tag, which Docker's syslog driver sets to the container or compose service name |
| `limit` | 5000 | Entries per page; up to 20 pages per run, the rest on the next run |
| `environment` | the linked service's, else `production` | Environment for entries without an `environment` field |
| `timeout`, `lag_seconds` | 30, 60 | As above |

The level of each entry is, in order: the `level` of a JSON message; a level word such as
`ERROR:`, `WARN`, `FATAL` or nginx's `[notice]` near the start of a text message; the entry's own `level` field.
Docker's syslog driver marks everything a container writes to stderr as an error, so the
level the message states is the more accurate one.

### `push_logs` and `push_metrics`

For sources that send data to Opsmith instead of being polled: a log shipper, an OpenTelemetry
pipeline, a script. They take no `config`.

```toml
[[projects.connectors]]
name = "collector"
type = "push_logs"
service = "payments-api"
```

Records are sent to `POST /api/v1/connectors/{id}/push` with a `service` key; see
[API](api.md#pushing-signals). The other connector types accept pushed records as well.

### Where connectors may connect

Connector URLs must be plain `http://` or `https://` URLs without credentials, query string or
fragment. Addresses in the link-local range, which includes cloud metadata endpoints, are
refused. Private addresses are allowed, because that is where Loki and Prometheus normally run.

## Protected paths

Protected paths decide which changes need more than a normal approval.

```toml
[projects.settings]
protected_paths = [
  { pattern = ".github/**", mode = "elevated" },
  { pattern = "**/auth/**", mode = "elevated" },
  { pattern = "**/*.pem",   mode = "excluded" },
]
```

| Mode | Effect |
|---|---|
| `elevated` | Only an **admin** can approve, and must tick an explicit acknowledgement |
| `excluded` | Opsmith never changes these. A task that needs to cannot be approved; handle it by hand |

The three rules above are the defaults. Setting `protected_paths` replaces them, so include the
ones you want to keep.

Patterns are shell-style globs matched against the path from the repository root; `*` also
matches `/`. A leading `**/` also matches at the root. A pattern also covers everything
below a directory it names, and a leading `/` or `./` is ignored: `infra`, `infra/`,
`/infra/**` and `infra/**` all protect the `infra` directory. A pattern that could never
match (empty, or containing `..`) is refused when you save it.

The rules are applied twice: to the paths a task names when it is created or edited, and to
the files the agent **actually changed**. The second check is the one that counts: a task that
did not mention CI configuration cannot produce a pull request that touches it without an
elevated approval.

## Budgets

```toml
[projects.settings.budgets]
max_tokens_per_run = 400000         # one analysis run, audit triage or execution
max_cost_per_run_usd = 5.0
max_cost_per_day_usd = 50.0         # the whole project, per UTC day
max_execution_seconds = 1800        # one execution attempt
max_agent_iterations = 8            # how long the coding agent may keep trying
max_concurrent_executions = 2       # executions running at once in this project
```

The caps cover every stage that calls a model, including the coding agent.

When a cap is reached the stage stops, the run is recorded as `budget_exceeded`, and a
`budget.exceeded` notification is sent. An execution that would start with the day's budget
already spent is refused and its task returns to review; a coding agent that reaches a cap
mid-run stops there. Calls that fail, are refused or are cut off still count
against the caps. Before each model call Opsmith reserves the worst case (the prompt plus the
full output allowance), so set `max_tokens_per_run` comfortably above the stage's `max_tokens`.

## Models

Which model each stage uses is part of the project settings (`executor_agent` and
`[projects.settings.llm]`). It has its own page, with the provider options, per-stage
examples, prices and what a model must support: **[Models](models.md)**.

## Policy rules and licenses

Your own rules, checked by the `compliance` audit:

```toml
[[projects.settings.policy_rules]]
id = "no-print"
description = "Use the logger, not print()"
path_glob = "app/**/*.py"
forbid_regex = 'print\('
severity = "low"

[[projects.settings.policy_rules]]
id = "license-header"
description = "Every source file carries the license header"
path_glob = "**/*.py"
require_regex = "SPDX-License-Identifier"
severity = "info"
```

`forbid_regex` reports each line that matches; `require_regex` reports each file where nothing
matches. Severity is one of `info`, `low`, `medium`, `high`, `critical`.

```toml
[projects.settings]
license_allowlist = ["MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC"]
```

Regular expressions you supply here are trusted. Avoid nested quantifiers such as `(a+)+`: a
pathological expression can stall a worker.

## Notifications

```toml
[projects.settings]
notifications = [
  { type = "log" },
  { type = "webhook", url = "https://hooks.slack.com/services/...", events = ["task.*", "job.dead"] },
]
```

A webhook receives `{"text": "...", "event": "..."}`, which Slack-compatible endpoints accept.
`events` are glob patterns; the default is everything.

| Event | When |
|---|---|
| `task.in_review` | A new task is waiting for review |
| `task.reproposed` | A rejected task came back because the evidence materially changed |
| `task.pr_open` | A pull request was opened |
| `task.execution_failed` | An execution failed or was refused; the task is back in review |
| `task.ci_failed` | CI failed on an Opsmith pull request |
| `task.regressed` | The problem came back after the merge |
| `lead.escalated` | A lead stayed undecided for too long |
| `inference.failed` | An analysis run failed (the model call failed or its output was unusable) |
| `check.failed` | A [check](checks.md) run failed or did nothing (no model can follow it, or no file matches) |
| `budget.exceeded` | A cost or token cap stopped a stage |
| `job.dead` | A job used up its retries |

Delivery is a queued job: a failed delivery is retried, then dead-lettered.

## Redaction

Opsmith redacts common secrets and personal data on its own: private keys, cloud and API
tokens, `Authorization` and `Cookie` headers, credentials in URLs, `password=`-style
assignments (also inside JSON carried in a string), values of sensitive-looking keys, email
addresses, card numbers, phone numbers and IP addresses. Add patterns for formats specific to you:

```toml
[projects.settings]
redaction_extra_patterns = ['ACME-[0-9]{8}', 'session=[A-Za-z0-9]+']
```

Each match is replaced by a marker such as `[REDACTED:custom_0]`. Patterns are validated when
saved: each must be a valid regular expression that matches at least one character. See the limits of pattern-based redaction in
[Security](security.md#what-is-not-protected).

## Credentials

Configuration never contains a secret, only a reference to one:

| Form | Resolves to | Restrictions |
|---|---|---|
| `env:NAME` | The worker's environment variable `NAME` | `NAME` may not start with `OPSMITH_`, and may not be the database password or the LLM API key variable |
| `file:/path/to/secret` | The contents of that file, trimmed | The file must be inside `OPSMITH_SECRETS_DIR` (default `/run/secrets`) |

The restrictions exist because the secret is sent to a URL chosen in the same configuration.
Give each integration its own variable or file.

## Schedules

Opsmith creates schedules from the settings above: one per connector (ingest), per service
(analysis), per repository (audit) and per project (pull request tracking). You normally change
them by changing the settings.

To set one schedule's timing directly, or to pause it, use the API:

```bash
curl -X PATCH $OPSMITH/api/v1/schedules/$SCHEDULE_ID -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -d '{"cron": "0 4 * * 1"}'      # or {"interval_seconds": 3600}, {"enabled": false}
```

Timing set this way is kept when project settings change later. To run a stage once, right
now, use `POST /api/v1/projects/{id}/trigger` ([API](api.md#endpoints)).

Settings can also be edited as JSON in the UI under *Config* (admins only). The UI replaces the
whole settings object; the API's `PATCH` replaces only the top-level keys you send.
