# API and roles

Everything the UI does goes through the same services as the REST API under `/api/v1`.
Interactive OpenAPI documentation is served by the running API at `/docs`.

- [Authentication](#authentication)
- [Roles](#roles)
- [A first session with curl](#a-first-session-with-curl)
- [Endpoints](#endpoints)
- [Pushing signals](#pushing-signals)
- [Reviewing a task](#reviewing-a-task)
- [Errors](#errors)

## Authentication

Every request carries an API key. Keys start with `osk_`, are shown once when created, and are
stored only as a hash.

```http
Authorization: Bearer osk_...
```

The header `X-Opsmith-Key: osk_...` works too.

Create keys with the CLI:

```bash
opsmith init --tenant acme --admin "Ada"                              # the first admin
opsmith create-user --tenant acme --name "Riley" --role reviewer
opsmith create-user --tenant acme --name "collector" --role service
```

The UI signs in with the same key and keeps it in an HttpOnly, SameSite=Strict cookie. The API
accepts that cookie for `GET` requests only; anything that changes state needs the key in a header.

## Roles

| Role | Can do |
|---|---|
| `viewer` | Read everything in their tenant |
| `reviewer` | Everything a viewer can, plus decide on tasks (approve, edit, reject, defer, investigate) and resubmit deferred ones |
| `admin` | Everything a reviewer can, plus configure projects, trigger stages, retry dead jobs, and approve changes to protected paths |
| `service` | Push signals, and nothing else. It cannot read tasks, configuration or the audit log, and cannot use the UI. Give this role to log collectors |

All data is scoped to the caller's tenant. An object that belongs to another tenant answers
`404`, exactly like one that does not exist.

## A first session with curl

```bash
export OPSMITH=http://127.0.0.1:8000
export KEY=osk_...                      # an admin key

# Who am I?
curl -s -H "Authorization: Bearer $KEY" $OPSMITH/api/v1/me

# Create a project (settings are optional; see the Configuration page)
curl -s -X POST $OPSMITH/api/v1/projects -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -d '{"name": "payments"}'
# export PROJECT_ID=<the "id" in the response>

# The review queue, highest score first
curl -s -H "Authorization: Bearer $KEY" \
  "$OPSMITH/api/v1/projects/$PROJECT_ID/tasks?status=in_review"

# Approve a task (export TASK_ID=<an "id" from the queue above>)
curl -s -X POST $OPSMITH/api/v1/tasks/$TASK_ID/review -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -d '{"decision": "approve"}'

# Trace a pull request back to its evidence
curl -s -H "Authorization: Bearer $KEY" $OPSMITH/api/v1/tasks/$TASK_ID/provenance
```

Most people configure projects with a TOML file and `opsmith apply` instead of these calls;
see [Configuration](configuration.md).

## Endpoints

Paths are relative to `/api/v1`. "Read" means `viewer`, `reviewer` or `admin`.

### Identity and catalog

| Method and path | Role | Purpose |
|---|---|---|
| `GET /me` | any | The caller's id, name, role and tenant |
| `GET /catalog` | read | Available connector types, analyzers and audit types |

### Configuration

| Method and path | Role | Purpose |
|---|---|---|
| `POST /projects` | admin | Create a project. `409` if the name exists |
| `GET /projects`, `GET /projects/{id}` | read | List or fetch projects |
| `PATCH /projects/{id}/settings` | admin | Change settings. The body is `{"settings": {...}}`. Top-level keys you send replace the stored ones; a nested object such as `budgets` is replaced as a whole |
| `POST /projects/{id}/repositories` | admin | Add a repository |
| `POST /projects/{id}/services` | admin | Add a service |
| `POST /projects/{id}/connectors` | admin | Add a connector |
| `GET /projects/{id}/repositories` · `/services` · `/connectors` · `/schedules` | read | List them |
| `PATCH /schedules/{id}` | admin | Enable or disable a schedule, or set its timing (`cron` **or** `interval_seconds`). Timing set here is kept when settings change later |
| `POST /projects/{id}/trigger` | admin | Run a stage now: `{"job": "ingest" \| "inference" \| "audit" \| "check" \| "aggregate" \| "track_prs", "target_id": "...", "mode": "auto" \| "full" \| "incremental", "check": "..."}`. `target_id` is the connector, service or repository for the first four; `check` names the [check](checks.md) to run, which may be disabled |

### Pipeline state

| Method and path | Role | Purpose |
|---|---|---|
| `GET /projects/{id}/summary` | read | Dashboard numbers: task funnel, open leads, cost, job health |
| `GET /projects/{id}/tasks?status=` | read | Tasks, highest score first |
| `GET /tasks/{id}` | read | One task |
| `GET /tasks/{id}/provenance` | read | The full chain from PR back to raw evidence |
| `GET /projects/{id}/findings?status=` | read | Findings |
| `GET /projects/{id}/leads?state=` | read | Leads |
| `GET /projects/{id}/inference-runs` · `/audit-runs` · `/executions` | read | Run history |
| `GET /jobs?status=` | read | Queue jobs for the tenant |
| `GET /audit-log?entity_id=` | read | The audit trail, newest first |

The run, execution, lead, finding, task, job and audit-log lists take `limit` (at least 1;
each has a maximum between 500 and 2000). The lists of projects, repositories, services,
connectors and schedules return everything.

### Decisions and operations

| Method and path | Role | Purpose |
|---|---|---|
| `POST /tasks/{id}/review` | reviewer, admin | Record a decision. See [Reviewing a task](#reviewing-a-task) |
| `POST /tasks/{id}/resubmit` | reviewer, admin | Put a deferred task back in the queue |
| `POST /jobs/{id}/retry` | admin | Retry a dead job |
| `POST /connectors/{id}/push` | service, admin | Push signals. See below |

### Outside `/api/v1`

| Path | Purpose |
|---|---|
| `GET /healthz` | Liveness and database check. Unauthenticated |
| `GET /metrics` | Prometheus metrics. Unauthenticated |
| `GET /docs` | OpenAPI documentation |
| `/ui` | The review UI |

## Pushing signals

Send a JSON array of at most 10,000 records to a connector. Logs go to a connector of a log
type, metrics to one of a metric type.

```bash
curl -s -X POST $OPSMITH/api/v1/connectors/$CONNECTOR_ID/push \
  -H "Authorization: Bearer $SERVICE_KEY" -H 'Content-Type: application/json' -d '[
    {"timestamp": "2026-03-01T10:00:00Z", "service": "payments-api", "severity": "error",
     "message": "upstream timeout after 30000ms",
     "attributes": {"stack": "File \"app/payments.py\", line 10, in charge"}}
  ]'
```

| Log field | Required | Notes |
|---|---|---|
| `timestamp` | yes | ISO 8601. Without a time zone it is taken as UTC |
| `service` | yes | Unknown services are created automatically |
| `message` | yes | |
| `severity` | no | Default `info`. Common level names and numeric levels are understood |
| `environment` | no | Default `production` |
| `attributes` | no | Free-form. A `stack` or similar field with file and line lets Opsmith tie the event to code |
| `trace_id`, `correlation_id`, `version` | no | |

| Metric field | Required | Notes |
|---|---|---|
| `timestamp`, `service`, `name`, `value` | yes | A value that is not a finite number (`NaN`, `Infinity`), or is beyond 1e150, is dropped and counted |
| `environment`, `labels` | no | |

Records are redacted before they are stored. A disabled connector refuses pushes with `409`. A
timestamp within a day of year 1 or year 9999 is refused with `422`. A record whose service
or environment name cannot be stored (too long, or holding a NUL) is left out of the batch;
the response's `records` says how many were stored.

## Reviewing a task

```json
{
  "decision": "approve_with_edits",
  "comment": "Scope it to the gateway client only.",
  "edits": {"paths": ["app/payments.py"], "priority": "P1"},
  "expected_version": 4
}
```

| Field | Meaning |
|---|---|
| `decision` | `approve`, `approve_with_edits`, `reject`, `defer` or `request_investigation` |
| `edits` | With `approve_with_edits`. Editable: `title`, `proposed_approach`, `acceptance_criteria`, `priority` (P0 to P3), `paths` (relative to the repository), `branch_name` (must start with `opsmith/`), `category`, `risk_assessment`, `effort_estimate` (S, M, L) |
| `reason_code` | Required with `reject`: `false_positive`, `wont_fix`, `duplicate`, `insufficient_evidence`, `out_of_scope`, `too_risky` or `other` |
| `investigation` | With `request_investigation`: the question for the next analysis run |
| `elevated_ack` | `true` to approve a task that touches `elevated` protected paths. Admins only |
| `expected_version` | The task `version` you loaded. If the task changed since (for example new evidence arrived), the review is refused so you can look again |

A `comment` or `investigation` may be up to 20,000 characters; an edited list (paths,
acceptance criteria) up to 200 items of 5,000 characters.

Only tasks in `in_review` can be reviewed. An approval covers the task exactly as it is at that
moment; if its scope changes before or during execution, the execution is refused and the task
comes back to the queue.

## Errors

| Status | Meaning |
|---|---|
| `401` | Missing or invalid key |
| `403` | The key's role may not do this |
| `404` | No such object in your tenant |
| `409` | Conflict: a duplicate name, a task that is not in a reviewable state, a stale `expected_version`, an approval its protected paths do not allow, a push to a disabled connector, a job that is not dead |
| `400` | A `Content-Length` that is not a number |
| `413` | A request body over 1 MiB (32 MiB for a push), or more than 10,000 records in one push |
| `422` | Invalid input. The body lists what is wrong |
