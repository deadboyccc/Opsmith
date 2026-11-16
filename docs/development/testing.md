# Testing

How the test suite is organized, how to run each part of it, and what a change is expected to
come with.

- [Running the tests](#running-the-tests)
- [What is where](#what-is-where)
- [Writing a test](#writing-a-test)
- [Continuous integration](#continuous-integration)
- [Keeping the docs true](#keeping-the-docs-true)
- [Regenerating the UI screenshots](#regenerating-the-ui-screenshots)

## Running the tests

```bash
uv sync --all-extras
make check                          # everything CI runs: ruff, format check, mypy, tests with coverage
uv run pytest -q                    # tests only, about a minute
uv run pytest tests/test_llm.py -q  # one area while you iterate
uv run pytest -k "budget" -q        # by name
```

Five parts of the suite depend on something outside Python. Each skips itself, with a reason,
when that thing is missing:

| What | How to run it | Needs |
|---|---|---|
| The default suite | `uv run pytest -q` | Nothing. SQLite, no network |
| **PostgreSQL** | `OPSMITH_TEST_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/db uv run pytest -q` | A server whose user may create databases. Every test gets its own database, dropped afterwards |
| **Real sandbox containers** | `uv run pytest -m docker` | A Docker daemon and the `busybox:stable` and `python:3.12-slim` images |
| **A live model endpoint** | `OPSMITH_TEST_OPENAI_BASE_URL=http://localhost:11434/v1 OPSMITH_TEST_OPENAI_MODEL=qwen2.5:1.5b uv run pytest tests/test_openai_provider.py -k live` | Any OpenAI-compatible server, for example Ollama with a small model |
| **Real Loki and Prometheus** | `OPSMITH_TEST_LOKI_URL=http://127.0.0.1:3100 OPSMITH_TEST_PROMETHEUS_URL=http://127.0.0.1:9090 uv run pytest tests/test_connectors_live.py` | A Loki and a Prometheus with default settings, for example `docker run -p 3100:3100 grafana/loki:3.7.10` and `docker run -p 9090:9090 prom/prometheus:v3.16.0`. Either URL alone runs that half |

A PostgreSQL to test against:

```bash
docker run -d --name opsmith-pg -e POSTGRES_USER=opsmith -e POSTGRES_PASSWORD=opsmith \
  -p 127.0.0.1:5432:5432 postgres:18.6-alpine3.24
OPSMITH_TEST_DATABASE_URL=postgresql+psycopg://opsmith:opsmith@127.0.0.1:5432/opsmith uv run pytest -q
```

Run the suite on PostgreSQL when you touch models or queries: it enforces column lengths and
types that SQLite ignores. Every test database is built by the migrations, and
`tests/test_migrations.py` fails when they and the models disagree, so a model change without a
migration fails on both backends.

A green run without Docker proves less than one with it, because the container tests were
skipped. `uv run pytest -q -rs` lists what was skipped and why.

## What is where

| File | Covers |
|---|---|
| `test_e2e.py` | The specification's acceptance criteria, on the offline provider |
| `test_provider_parity.py` | The same scenario through the OpenAI-compatible HTTP path; must end identically |
| `test_hardening.py` | Adversarial tests: approval binding, diff-gate bypasses, sandbox escapes, queue and scheduler failure modes, roles, cross-tenant access, credentials, budgets |
| `test_ingestion_hardening.py`, `test_ui_hardening.py` | The same for ingestion and for the UI |
| `test_executor.py`, `test_review.py`, `test_aggregation.py`, `test_inference.py`, `test_audits.py`, `test_etl.py`, `test_connectors.py`, `test_redaction.py` | One per pipeline area |
| `test_llm.py`, `test_openai_provider.py` | The gateway and the two remote providers, against fake clients and a scripted endpoint |
| `test_queue_scheduler.py`, `test_notifications.py`, `test_scm_tracking.py` | Infrastructure |
| `test_api.py`, `test_cli.py` | The REST API, the UI routes and the command line |
| `test_migrations.py` | The schema migrations match the models; adopting 1.0.0 databases; refusing older and newer ones; concurrent starts on PostgreSQL |
| `test_connectors_live.py` | The Loki and Prometheus connectors against real servers (skipped without them) |

`conftest.py` provides the fixtures nearly every test uses:

- `platform`: a fully wired `Platform` on a fresh database and data directory.
- `seed`: a tenant with one user per role, a project, a local git repository with known
  defects, a service and two file connectors.
- `drain()`: runs queued jobs to completion.
- External scanners are disabled unless a test puts a fake one on `PATH`, so results do not
  depend on what the host has installed.

## Writing a test

- **Stay offline.** `httpx.MockTransport` for HTTP; fake clients for the Anthropic SDK; a
  scripted endpoint (`Server` in `test_openai_provider.py`) for OpenAI-compatible calls.
- **Test the failure path too.** For anything that guards safety (approval, sandbox, redaction,
  tenancy, roles, budgets), write a test that tries the bad path and shows it is blocked. Add
  it to the table in [Security](../guide/security.md).
- **Prove the test can fail.** Check that it fails without your change. A timing-dependent test
  is a bug: control the clock or the order instead of hoping.
- **Use the fixtures** rather than building rows by hand, and `add_finding` / `aggregate`
  (`test_aggregation.py`) or `make_task` / `approve` (`test_executor.py`) to reach the state you
  need.

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request:

| Job | Does |
|---|---|
| `lint, types, tests` | ruff, format check, mypy, the suite with coverage. Docker is available, so the container tests run |
| `full suite on PostgreSQL` | The whole suite with `OPSMITH_TEST_DATABASE_URL` set |
| `loki …`, `prometheus …` | One job per server version in the [compatibility matrix](../guide/compatibility.md): starts that server and runs `tests/test_connectors_live.py` against it |
| `container image and compose files` | Builds both images, runs the suite inside the dev image, and checks that Compose refuses an empty database password |

`.github/workflows/live-models.yml` is separate, because it is slow: it starts Ollama with a
small open model and runs the live tests against it (structured output, a pipeline stage
through the gateway, the tool-calling agent loop). It runs once a week and on demand
(*Actions → Live models → Run workflow*, where you can pick the model). It needs no secrets.

## Keeping the docs true

```bash
make docs        # or: uv run python scripts/check_docs.py
```

The check fails when a relative link or heading anchor is broken, when the security pages cite
a test that does not exist, or when the code offers something the docs do not mention: an
environment variable, a project setting, an API endpoint, a notification event, a connector
type, an analyzer, a task or lead state, a model provider, a coding agent, a CLI command or a
job kind. So adding a setting or an endpoint without documenting it breaks the build.

## Regenerating the UI screenshots

The pictures in the [UI tour](../guide/ui-tour.md) come from the offline demo:

```bash
uvx --from playwright playwright install chromium     # once
uv run --with playwright python scripts/ui_screenshots.py
```
