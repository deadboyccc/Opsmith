# Opsmith

**Autonomous engineering ops: observe → analyze → propose → human approve → execute → PR.**

Opsmith continuously ingests logs, metrics and source code, runs chained LLM analysis that
carries open investigation leads from one batch run to the next, audits repositories on a
schedule, merges runtime and static evidence into deduplicated, ranked engineering tasks, and,
only after a human approves, has a sandboxed coding subagent implement the task and open a
pull request. Opsmith never merges. A human always does.

- Specification: [`docs/SPECIFICATION.md`](docs/SPECIFICATION.md)
- Design decisions and stack rationale: [`ARCHITECTURE_DECISIONS.md`](ARCHITECTURE_DECISIONS.md)
- Contributor and AI-agent guide: [`AGENTS.md`](AGENTS.md)

## How it works

```
 Loki / Prometheus / files / push API          Git repositories (GitHub, local)
              │                                            │
   ingest ─► normalize ─► enrich ─► REDACT        scheduled audits (full / incremental)
              │                                   secrets · AST bugs · N+1 · deps/OSV ·
   object store (raw JSONL)  ◄─ refs ─┐           licenses · policy-as-code · Semgrep ·
              │                       │           gitleaks · Trivy · OSV-Scanner · ruff
   analysis bundles (templates, metric features)            │
              │                                     LLM triage (false positives)
   chained inference every X hours  ◄── open leads ──┐      │
   (findings + lead updates, bounded context)   carried/    │
              │                                 expired     │
              └──────────────► aggregation ◄────────────────┘
                     correlate runtime ↔ static · dedup vs open/closed/rejected
                     score & rank (P0–P3) · draft task
                                  │
                       HUMAN REVIEW QUEUE (UI + API)
            approve · approve with edits · reject(reason) · defer · investigate
                                  │ (recorded approval required)
              sandboxed subagent: plan → edit → build/lint/test → diff gates
                                  │
                  branch push + PR (evidence, checks, provenance)
                                  │
             PR/CI tracking → merged by a human → post-merge verification
```

Every stage is a durable, idempotent, retried queue job. Failures end up as visible `dead`
jobs and notifications, never silent drops. Every PR links back through
task → findings → evidence (bundles, raw events, audit results) → pipeline runs → leads.

## Quickstart

### 1. Offline demo (no API key, about 5 seconds)

```bash
uv sync --all-extras
uv run opsmith demo --dir ./opsmith-demo
```

The demo creates a sample service with telemetry and a git repo. It runs three chained
inference windows (a lead opens, is carried, then is confirmed), a repository audit, correlation
and ranking. It then approves the top task as a reviewer, runs the sandboxed fix, opens a PR
with local CI, simulates a human merge, and verifies the fix post-merge. Browse the result:

```bash
OPSMITH_DATA_DIR=./opsmith-demo/data uv run opsmith api
# open http://127.0.0.1:8000/ui and sign in with the admin key printed by the demo
```

### 2. Run the stack with Docker Compose (PostgreSQL)

```bash
docker compose up -d --build
docker compose exec api opsmith init --tenant acme --admin "Your Name"   # prints an API key
docker compose cp opsmith.example.toml api:/tmp/opsmith.toml
docker compose exec api opsmith apply /tmp/opsmith.toml --tenant acme
```

The compose file runs `api`, `worker` and `scheduler` processes against PostgreSQL. The UI is
served at <http://localhost:8000/ui>.

### 3. Local development

```bash
uv sync --all-extras
make check          # ruff + format check + mypy + pytest with coverage
uv run opsmith init --tenant dev
uv run opsmith run  # API + scheduler + worker in one process (development only)
```

No local Python? `make docker-test` runs the full check suite inside the dev image.

## Configuration

Process settings are `OPSMITH_*` environment variables (see [`.env.example`](.env.example)).
Per-project behaviour (intervals, budgets, protected paths, models per stage, provider
allowlist, notification channels, policy rules) lives in project settings. Edit them in the
UI, through `PATCH /api/v1/projects/{id}/settings`, or declaratively:

```bash
opsmith apply opsmith.toml --tenant acme   # idempotent; see opsmith.example.toml
```

Credentials are never stored in the database, only references such as `env:GITHUB_TOKEN` or
`file:/run/secrets/token`.

### Using Claude

The default `heuristic` provider is deterministic and offline. To use Claude, install the
`anthropic` extra, provide `ANTHROPIC_API_KEY`, and set per-stage models in project settings:

```toml
[projects.settings]
executor_agent = "claude"
[projects.settings.llm.default]
provider = "anthropic"
model = "claude-opus-5-5"
effort = "medium"
[projects.settings.llm.stages.executor]
provider = "anthropic"
model = "claude-opus-5-5"
effort = "high"
```

Structured stages use `messages.parse` with the stage's pydantic schema. The coding agent uses a
tool-use loop over a fixed tool set (file operations confined to the checkout, plus the
repository's configured `build`/`lint`/`test` checks). Server-side refusal fallbacks are on by
default (`OPSMITH_ANTHROPIC_FALLBACKS=off` disables them).

## Safety model (spec §8)

| Guarantee | Enforced by |
|---|---|
| No code change without recorded human approval | `executor.service.assert_approved`, checked before the run and again before the PR |
| No autonomous merge | the SCM interface has no merge operation |
| Redaction before any LLM call | ETL redaction plus a second pass in `LLMGateway` |
| Prompt-injection hardening | untrusted data delimited, unknown lead IDs and fabricated evidence refs discarded, fixed agent tools |
| Least privilege | read-only connectors; the agent holds no SCM token; branch push and PR only; default-branch push refused |
| Sandbox isolation | scrubbed env + timeouts (`local`), or `--network none`, `--cap-drop ALL` containers (`docker`) |
| Protected paths | `elevated` paths need an admin with `elevated_ack`; `excluded` paths are never changed |
| Budgets | token/cost caps per run, per project per day; execution time and iteration caps |
| Audit trail | append-only `audit_log`; immutable inference/audit runs with prompt and model versions |

## API

OpenAPI docs are served at `/docs`. Authenticate with `Authorization: Bearer <api key>`. The main
endpoints are:

- `POST /api/v1/projects`, `/projects/{id}/repositories|services|connectors`
- `PATCH /api/v1/projects/{id}/settings`, `PATCH /api/v1/schedules/{id}`
- `POST /api/v1/connectors/{id}/push` (push-mode ingestion, `service` role)
- `GET /api/v1/projects/{id}/tasks|leads|findings|inference-runs|audit-runs|executions|summary`
- `POST /api/v1/tasks/{id}/review`, `GET /api/v1/tasks/{id}/provenance`
- `POST /api/v1/projects/{id}/trigger`, `GET /api/v1/jobs`, `POST /api/v1/jobs/{id}/retry`
- `GET /healthz`, `GET /metrics` (Prometheus)

Roles: `viewer` (read), `reviewer` (review tasks), `admin` (configure, retry jobs, elevated
approvals), `service` (push signals).

## Project layout

```
src/opsmith/
  connectors/   pluggable signal sources (file, Loki, Prometheus; entry points for more)
  etl/          normalization, template mining, metric features, bundles
  llm/          gateway, prompts, pricing, Claude + heuristic providers
  inference/    chained batch inference with lead carry-over
  audits/       analyzers (built-in + external tools) and the audit runner
  aggregation/  correlation, dedup, scoring, task generation
  executor/     sandbox, coding agents, PR rendering, execution service
  scm/          GitHub + local providers (no merge capability)
  api/, ui/     FastAPI JSON API and server-rendered review UI
  review.py, tracking.py, scheduler.py, queue.py, platform.py, cli.py
tests/          unit, integration and spec §11 acceptance tests (tests/test_e2e.py)
```

## License

[MIT](LICENSE)
