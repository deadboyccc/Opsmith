# AGENTS.md

Guidance for AI coding agents and human contributors working on this repository.
This file is canonical; tool-specific files (`CLAUDE.md`) import it rather than repeat it.

## What this is

Opsmith is an engineering-ops platform. It ingests logs, metrics and code, runs chained LLM
analysis with carried-over leads plus scheduled repo audits, turns the results into ranked tasks,
and, **only after a recorded human approval**, has a sandboxed subagent open a PR. Spec:
`docs/SPECIFICATION.md`. The rationale for every design choice is in `ARCHITECTURE_DECISIONS.md`;
read the relevant ADR before changing a subsystem.

## Commands

```bash
uv sync --all-extras                 # install (Python 3.12+, uv 0.9+)
make check                           # everything CI runs: lint, format check, mypy, tests+coverage
uv run pytest -q                     # tests (~90s); add -k <expr> to target, -m e2e for acceptance
uv run pytest tests/test_e2e.py      # spec §11 acceptance criteria, end to end
uv run ruff check src tests --fix && uv run ruff format src tests
uv run mypy                          # strict-ish typing on src/
uv run opsmith demo --dir /tmp/demo  # full offline loop: ingest -> ... -> PR -> verified
make docker-test                     # same checks inside the dev image (no local Python needed)
```

PostgreSQL parity: `OPSMITH_DATABASE_URL=postgresql+psycopg://... uv run pytest tests/test_e2e.py`.

## Repository map

| Path | Responsibility |
|---|---|
| `src/opsmith/platform.py` | Composition root and queue handler wiring. Start here. |
| `models.py`, `schemas.py` | ORM entities (all tenant-scoped) / pydantic contracts incl. LLM stage I/O |
| `queue.py`, `scheduler.py` | Durable idempotent jobs; cron/interval schedules |
| `audit_log.py` | Append-only audit trail, task/lead state machines, run immutability |
| `connectors/`, `etl/` | Signal sources; normalize → enrich → redact → store → bundle |
| `llm/` | `LLMGateway` (the only path to a model), prompts, pricing, providers |
| `inference/orchestrator.py` | Chained runs, lead carry/confirm/refute/expire |
| `audits/` | Analyzers (built-in + external tools) and the audit runner |
| `aggregation/` | Correlation, dedup/suppression, scoring, task drafting |
| `review.py` | Human review gate |
| `executor/` | Sandbox, workspace confinement, coding agents, execution service, PR body |
| `scm/` | GitHub + local providers. **No merge operation, by design.** |
| `tracking.py` | PR/CI tracking, post-merge verification |
| `api/`, `ui/` | FastAPI JSON API (`/api/v1`) and server-rendered UI (`/ui`) |
| `tests/` | Unit/integration tests; `conftest.py` seeds a tenant, users, project, git repo |

## Invariants: never break these

These are spec §8 hard constraints and are covered by tests. If a change seems to require
weakening one, stop and ask a human.

1. **No code change without a recorded human approval.** Execution goes through
   `executor.service.assert_approved` (latest review is an approval by role `reviewer|admin`),
   checked before the run and again right before the PR. Never add a bypass, flag or "auto
   approve" path.
2. **No merge capability.** Do not add merge/auto-merge to `SCMProvider` or call merge APIs.
   `LocalSCM.human_merge` exists only to simulate a human in tests and the demo.
3. **Redact before any LLM call.** Model calls go through `LLMGateway.structured` (or the
   executor agent, which only sees the confined workspace). Never call a provider SDK directly
   from pipeline code.
4. **Untrusted data stays data.** Logs, code and issue text go inside `<untrusted_data>`.
   Validate model output against schemas, and drop unknown IDs and fabricated evidence refs.
5. **Least privilege.** Agents get no shell, network or credentials. Tools are a fixed list.
   Tokens are resolved from `credentials_ref` (`env:`/`file:`) and never persisted.
6. **Tenant isolation.** Every query filters by `tenant_id`. Use `api.deps.owned()` to load
   by id, which returns 404 for other tenants.
7. **Auditability.** State changes go through `audit_log.transition_task` / `transition_lead`
   or `audit_log.record`. Finished `InferenceRun`/`AuditRun` rows are immutable (a flush guard
   raises).
8. **No silent drops.** Failures must surface: retry, `dead` job with notification, task back
   to `in_review` with an `execution_report`, or a finding marked `suppressed` with a
   `status_reason`.

## Conventions

- Python 3.12, full type hints, `mypy` clean, `ruff` clean (line length 100). No relative
  imports.
- Use `db.utcnow()` for time (aware UTC) and `db.new_id(prefix)` for ids.
- **JSON columns are replaced, never mutated in place**: `task.paths = [*task.paths, p]`, not
  `task.paths.append(p)`. SQLAlchemy does not track in-place changes.
- Enqueue follow-up work with `queue.enqueue(..., idempotency_key=...)` in the same transaction
  as the state it depends on. Keys must be deterministic for the logical unit of work.
- Handlers raise `queue.RetryLater` to wait (no attempt consumed) and `queue.PermanentFailure`
  when retrying cannot help.
- Keep long-running work (clones, agents, checks) outside DB transactions. See
  `ExecutionService`.
- Modules under `src/opsmith/api/` avoid `from __future__ import annotations` so FastAPI can
  resolve dependency annotations.
- Bump a prompt's `version` in `llm/prompts.py` whenever its text changes. Runs record it.
- Default Claude model is `claude-opus-5-5`. Use only model IDs listed in `llm/pricing.py` and
  update pricing when adding a model.

## Extending

- **Connector:** subclass `LogConnector`/`MetricConnector`, decorate with
  `@register("type")`, implement `fetch(start, end, cursor)`. Register out-of-tree plugins
  under the `opsmith.connectors` entry point. Add a test with `httpx.MockTransport`.
- **Analyzer:** subclass `audits.base.Analyzer` (or `ExternalTool` with a pure `parse()`),
  decorate with `@register`, and declare `audit_types`. Out-of-tree: the `opsmith.analyzers`
  entry point.
- **LLM stage:** add a pydantic output schema to `schemas.py`, a prompt to `llm/prompts.py`,
  and a matching branch in `HeuristicProvider` so the stage works offline. Call it through
  `LLMGateway.structured`.
- **Task state:** extend `TASK_TRANSITIONS`, the UI status chips (`ui/templates/macros.html`),
  `dashboard.FUNNEL`, and add an ADR entry.

## Testing

- Tests must not touch the network. Use `httpx.MockTransport` for HTTP and fake clients for
  the Anthropic SDK (see `tests/test_llm.py` and `tests/test_executor.py`).
- Use the `platform`, `seed` and `drain` fixtures. `drain()` runs queued jobs to completion.
- Any behaviour change to the pipeline needs a test at the module level, and the spec §11
  assertions in `tests/test_e2e.py` must keep passing.
- Security-relevant code (gates, confinement, redaction, tenancy) needs a negative test that
  proves the bad path is blocked.

## Definition of done

- [ ] `make check` passes (ruff, format, mypy, pytest).
- [ ] New behaviour is covered by tests, including failure and negative paths.
- [ ] Invariants above are untouched, or a human approved the change and an ADR records it.
- [ ] Significant design changes are recorded in `ARCHITECTURE_DECISIONS.md`.
- [ ] No secrets, tokens or real customer data in code, tests, fixtures or commits.

## Commits and PRs

Use imperative, scoped messages (`executor: block secrets on added lines only`). Keep PRs
focused, describe the risk and how you verified the change, and never commit generated data
(`data/`, `*.db`, demo output).
