# Operations

Running Opsmith day to day: what to watch, how to upgrade, and what to do when something goes
wrong.

- [Health and metrics](#health-and-metrics)
- [Jobs and workers](#jobs-and-workers)
- [Notifications](#notifications)
- [Audit trail](#audit-trail)
- [Backups](#backups)
- [Upgrading](#upgrading)
- [Troubleshooting](#troubleshooting)

## Health and metrics

- `GET /healthz` checks that the API can reach the database.
- `GET /metrics` is Prometheus text: stage latency, model calls with their cost and latency,
  ingested records, executions and HTTP timing.

Both are unauthenticated. Keep the API port private, or restrict those two paths at your proxy.

The *Dashboard* in the UI shows the same picture per project: what is waiting for review, open
and escalated leads, dead jobs, cost in the last 24 hours, and cost and latency per stage.

## Jobs and workers

Every stage is a queued job.

- A job that fails is retried with backoff. When its attempts are used up it becomes `dead`,
  which is audited and sent as a `job.dead` notification.
- See jobs in the UI under *Jobs*, or `GET /api/v1/jobs?status=dead`. An admin can retry a dead
  job from either place.
- Run as many workers as you need. A running job's lease is renewed while its worker is alive,
  so a slow job is never handed to a second worker. If a worker dies, its job is picked up
  again after `OPSMITH_WORKER_LEASE_SECONDS`.
- Stop a worker with `SIGTERM`. It finishes the job it is running first.
- More than one scheduler is safe: a schedule slot is enqueued once however many ask.

Things worth an alert:

| Signal | Means |
|---|---|
| A `job.dead` notification | A stage gave up after its retries. Read the job's error, fix the cause, retry it |
| `task.execution_failed` | An approved task did not become a pull request. The task is back in review with a report |
| `inference.failed` | A model call failed or returned unusable output. See [Models](models.md#what-a-model-needs-to-be-able-to-do) |
| `budget.exceeded` | A token or cost cap stopped a stage. Raise the cap or wait for the next day |
| `lead.escalated` | The analysis could not decide about something for several runs. A person should look |
| An audit run that shows an error on the dashboard | An analyzer or scanner failed. The run still records what the others found, and the next audit re-checks everything |

## Notifications

Configure a webhook per project to hear about all of the above
([Configuration](configuration.md#notifications)). Delivery is itself a queued job, so a failed
delivery is retried.

## Audit trail

Every state change and every decision is in the audit log, with who made it:
`GET /api/v1/audit-log`, or the bottom of any task page in the UI.

## Backups

Back up the PostgreSQL database and the data directory (`OPSMITH_DATA_DIR`) together. The
database is the source of truth; the data directory holds the raw signals that evidence refers
to.

## Upgrading

Opsmith creates its tables at start and has **no schema migrations yet**
([ADR-002](../../ARCHITECTURE_DECISIONS.md)). A release that changes the schema says so under
*Upgrade notes* in the [changelog](../../CHANGELOG.md); until migrations exist, such an upgrade
needs a fresh database. Treat current versions as pre-1.0: fine for evaluation and pilots, not
yet for data you cannot recreate.

To upgrade a Compose installation without a schema change:

```bash
git pull
docker compose up -d --build
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `required variable POSTGRES_PASSWORD is missing a value` | Set it in `.env`. This is deliberate |
| Task returns to review with `sandbox unavailable` | The worker cannot use Docker. See [Execution sandbox](deployment.md#execution-sandbox) |
| Task returns to review with `sandbox policy` | `OPSMITH_SANDBOX_BACKEND=local` with a non-local repository. Use `docker` |
| `docker could not start the sandbox` | The image is missing or cannot be pulled, or `OPSMITH_WORK_DIR` is not shared with the host at the same path |
| Checks fail with download or DNS errors | The sandbox has no network. Bake dependencies into `OPSMITH_SANDBOX_IMAGE` |
| `task changed after it was approved` | The task's scope changed between approval and execution. Review it again |
| `... is outside OPSMITH_LOCAL_ROOT` or `reads the worker's filesystem, which is disabled` | Set `OPSMITH_LOCAL_ROOT` to the directory holding your local repositories and signal files |
| `... names a variable reserved for the platform` | A `credentials_ref` pointed at one of Opsmith's own secrets. Use a dedicated variable |
| An analysis run is `failed` with a validation error | The model did not return JSON matching the stage's schema, twice. Use a more capable model for that stage |
| `model endpoint unreachable` or `HTTP 4xx` from the model endpoint | Check `OPSMITH_OPENAI_BASE_URL`, the key, and the model name in the project settings |
| Permission denied on `/var/run/docker.sock` | `DOCKER_GID` does not match the socket's group |
| UI form posts are rejected with 403 behind a proxy | The proxy must pass the browser's `Host` header through unchanged |
