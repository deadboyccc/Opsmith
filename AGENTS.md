# AGENTS.md

Guidance for AI coding agents and human contributors working on this repository.
This file is canonical; tool-specific files (`CLAUDE.md`) import it rather than repeat it.

## What this is

Opsmith is an engineering-ops platform. It ingests logs, metrics and code, runs chained LLM
analysis with carried-over leads plus scheduled repo audits, turns the results into ranked tasks,
and, **only after a recorded human approval**, has a sandboxed subagent open a PR.

Where to read first:

| Need | Read |
|---|---|
| What the system does, from the outside | [`docs/guide/concepts.md`](docs/guide/concepts.md) |
| How it is built: job pipeline, model layer, execution path, plugins | [`docs/development/architecture.md`](docs/development/architecture.md) |
| How to run and write tests | [`docs/development/testing.md`](docs/development/testing.md) |
| Why it is built this way | [`ARCHITECTURE_DECISIONS.md`](ARCHITECTURE_DECISIONS.md); read the relevant ADR before changing a subsystem |
| What is guaranteed, and which test proves it | [`docs/guide/security.md`](docs/guide/security.md) |
| The original intent | [`docs/development/specification.md`](docs/development/specification.md) |
| How past fixes and tool choices were decided | [`docs/DECISIONS.md`](docs/DECISIONS.md) |
| What the last security audit found and left open | [`docs/SECURITY-AUDIT.md`](docs/SECURITY-AUDIT.md) |

## How every session works

Opsmith decides what code an AI agent may change on other people's repositories. Stability,
robustness, reliability and security come before speed and before features. Every session,
by a person or an agent, follows these rules.

**Start from a known state.**

1. Read this file, then the docs for the area you will touch, and the ADR behind it.
2. Run `make check` before changing anything, and note the result. If it is not green, find
   out why before you add to it. Never build on a red baseline without saying so.

**Change the code with care.**

3. Follow the conventions below and the code around you. Choose the correct, simple and
   readable solution over the clever one; a reviewer must be able to see that it is right.
4. Every change comes with tests: the happy path, the failure path and, for anything that
   guards safety, a negative test that shows the bad path is blocked.
5. **A bug fix starts with a failing test.** Write the test that reproduces the bug, run it,
   and see it fail for the reason you expect. Then fix the code and see it pass. The test
   stays, as the regression test.
6. Secure by default. Validate input at the edge and bound it to what stores it. Keep
   untrusted data as data. Never weaken an invariant below, never add a secret, credential,
   real customer data or token to code, tests, fixtures, logs or commits, and never add an
   unsafe pattern (`shell=True`, `eval`, `pickle`, string-built SQL, unconfined paths,
   unbounded reads, calls without timeouts).
7. Never hide a failure: no bare `except` that swallows, no skipped test to get green, no
   retry that masks a bug. A failure surfaces (see *No silent drops*).

**Prove it before you call it done.**

8. Run `make check` again after the change, plus `make site` when docs changed. Run the
   suite on PostgreSQL when you touched models or queries, and `uv run pytest -m docker`
   when you touched the sandbox or the executor and Docker is available. Report failures
   with their output; never describe a red run as green.
9. Never merge, and never ask for a merge, with a regression: a test that passed before must
   pass after, coverage must stay above the floor, and the build must be clean.

**Leave the docs true. Every session ends with these, without exception.**

10. **Update the Markdown docs** your change affects: the pages under `docs/`, the README,
    this file, `CONTRIBUTING.md`. A behaviour the docs do not describe, or describe wrongly,
    is a bug.
11. **Update the documentation site.** It is built from those same pages, so it changes when
    they do; add a new page to `nav` in `mkdocs.yml`, and run `make site` to prove it builds
    with no broken link.
12. **Update `CHANGELOG.md`** under *Unreleased* (Keep a Changelog: Added, Changed, Fixed,
    Security, with *Upgrade notes* for anything an operator must do). The site's changelog
    page is this file, so updating it updates the site.
13. **Record significant decisions in `docs/DECISIONS.md`**, with the reasoning and the
    options not taken: a choice between workable designs, a tool, something left open on
    purpose. Design decisions about the system itself also get an ADR in
    `ARCHITECTURE_DECISIONS.md`.

**Ship every change, with docs that already describe it.**

14. **Every commit carries its changelog entry, and every change that reaches `main` is
    released.** A commit that changes the product, its docs or its tooling adds its entry
    under *Unreleased* in the same commit. When it lands on `main`, release it at once: move
    the entries under `## <version> - <yyyy-mm-dd>` (a patch version for fixes, a minor one
    for features), set that version in `pyproject.toml` and `src/opsmith/__init__.py`, tag
    `v<version>`, and publish a GitHub Release whose notes are that changelog section. Do not
    let unreleased changes collect on `main`.
15. **Before every push, the docs and the site are up to date.** The Markdown pages, the
    README and `CHANGELOG.md` describe the change, every new page is in `nav`, and `make
    site` builds with no warning. After a push to `main`, check that the *Docs* workflow
    deployed GitHub Pages and that the live site shows the new version and changelog; a
    failed deploy is fixed before other work.

## Commands

```bash
uv sync --all-extras                 # install (Python 3.12+, uv 0.9+)
make check                           # lint, format, mypy, tests + coverage floor, docs (CI's main job)
make audit                           # known vulnerabilities in the locked dependencies (CI runs it)
make site                            # build the documentation site, strict (make site-serve to preview)
uv run pytest -q                     # tests (4-6 min); add -k <expr> to target, -m e2e for acceptance
uv run pytest tests/test_e2e.py      # spec §11 acceptance criteria, end to end
uv run pytest -m docker              # tests that start real containers (skipped without Docker)
uv run python scripts/check_docs.py  # docs: links, anchors, cited tests
uv run ruff check src tests --fix && uv run ruff format src tests
uv run mypy                          # strict-ish typing on src/
uv run opsmith demo --dir /tmp/demo  # full offline loop: ingest -> ... -> PR -> verified
make docker-test                     # same checks inside the dev image (no local Python needed)
```

The whole suite also runs on PostgreSQL. Point `OPSMITH_TEST_DATABASE_URL` at a server whose
user may create databases; each test then gets its own database:

```bash
docker run -d --name pg -e POSTGRES_USER=opsmith -e POSTGRES_PASSWORD=opsmith -p 5432:5432 postgres:18.6-alpine3.24
OPSMITH_TEST_DATABASE_URL=postgresql+psycopg://opsmith:opsmith@localhost:5432/opsmith uv run pytest -q
```

## Repository map

| Path | Responsibility |
|---|---|
| `src/opsmith/platform.py` | Composition root and queue handler wiring. Start here. |
| `models.py`, `schemas.py` | ORM entities (all tenant-scoped) / pydantic contracts incl. settings and LLM stage I/O |
| `migrations/` | Alembic revisions of the schema, applied by every process at start (ADR-002) |
| `queue.py`, `scheduler.py` | Durable idempotent jobs with leased, heartbeated claims; cron/interval schedules |
| `audit_log.py` | Append-only audit trail, task/lead state machines, run immutability |
| `config.py`, `localfs.py` | Process settings and credential references; confinement of host-file access |
| `connectors/`, `etl/` | Signal sources; normalize → enrich → redact → store → bundle |
| `redaction.py` | Secret/PII detectors shared by ETL, the gateway and the diff gate |
| `llm/` | `LLMGateway` (the only path to a model), prompts, pricing, providers |
| `inference/orchestrator.py` | Chained runs, lead carry/confirm/refute/expire |
| `audits/` | Analyzers (built-in + external tools), the audit runner, and checks (`checks.py`): admin-written instructions a model carries out on the code (ADR-019) |
| `aggregation/` | Correlation, dedup/suppression, scoring, task drafting |
| `review.py` | Human review gate and the approval scope digest |
| `executor/` | Sandboxes, workspace confinement, coding agents, execution service, PR body |
| `gitutil.py` | The only place git is invoked: safe config, trusted git dir, unambiguous parsing |
| `scm/` | GitHub + local providers. **No merge operation, by design.** Repository addresses and branches are checked before git sees them (`scm.base.check_repository`) |
| `tracking.py` | PR/CI tracking, post-merge verification |
| `api/`, `ui/` | FastAPI JSON API (`/api/v1`) and server-rendered UI (`/ui`) |
| `tests/` | Unit/integration tests; `conftest.py` seeds a tenant, users, project, git repo. `test_hardening.py` holds the adversarial tests, `test_runtime.py` the process loops |
| `docs/`, root `*.md` | The documentation. `docs/guide/` for people running Opsmith, `docs/development/` for people changing it, `docs/DECISIONS.md` and `docs/SECURITY-AUDIT.md` for the record |
| `mkdocs.yml`, `site/`, `scripts/build_site.py` | The documentation site: every Markdown page, published to GitHub Pages by `.github/workflows/docs.yml` |
| `scripts/check_docs.py` | Fails the build when the docs and the code disagree (links, settings, endpoints, cited tests) |

## Invariants: never break these

These are spec §8 hard constraints and are covered by tests (`docs/guide/security.md` maps each one
to its tests). If a change seems to require weakening one, stop and ask a human.

1. **No code change without a recorded human approval.** Execution goes through
   `executor.service.assert_approved` (latest review is an approval by role `reviewer|admin`,
   and the task still has the scope that review approved), checked before the run and again
   right before the push. Never add a bypass, flag or "auto approve" path.
2. **An approval covers what the reviewer saw.** Anything that changes what the agent is told to
   do must be part of `review.approval_scope_digest`. Nothing may modify a task that is
   `approved` or `executing` (the aggregator defers evidence for such tasks).
3. **No merge capability.** Do not add merge/auto-merge to `SCMProvider` or call merge APIs.
   Pushes go only to `opsmith/...` branches (`scm.base.check_pushable`). `LocalSCM.human_merge`
   exists only to simulate a human in tests and the demo.
4. **Redact before any LLM call.** Model calls go through `LLMGateway.structured` (or the
   executor agent, which only sees the confined workspace). Never call a provider SDK directly
   from pipeline code.
5. **Untrusted data stays data.** Logs, code, file names and model output go inside
   `<untrusted_data>` via `llm.prompts.untrusted_json`. Validate model output against schemas,
   and drop unknown IDs and fabricated evidence refs.
6. **Sandboxed code never runs outside the sandbox.** Repository commands run only through a
   `Sandbox`. After the agent starts, the platform runs git only against the trusted snapshot
   (`run_git(..., git_dir=...)`), never against the checkout's own `.git`. The diff gates run
   after the last sandboxed command. The `local` backend is development-only and must stay
   refused for non-local repositories (`sandbox_policy_error`); never fall back to it.
7. **Least privilege.** Agents get no shell, network or credentials. Tools are a fixed list.
   Tokens are resolved from `credentials_ref` (`env:`/`file:`) through
   `config.resolve_tenant_credential` and never persisted. Paths from configuration that touch
   the worker's filesystem go through `localfs.confine`.
8. **Tenant isolation.** Every query filters by `tenant_id`. Use `api.deps.owned()` to load
   by id, which returns 404 for other tenants. Read routes require a human role; `service`
   keys may only push.
9. **Auditability.** State changes go through `audit_log.transition_task` / `transition_lead`
   or `audit_log.record`. Finished `InferenceRun`/`AuditRun` rows are immutable (a flush guard
   raises).
10. **No silent drops.** Failures must surface: retry, `dead` job with notification, task back
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
- **No model vendor lock-in.** Pipeline code depends on `llm.base.LLMProvider` and on
  `executor.agents.CodingAgent`, never on a vendor SDK. Anything a provider needs that is
  vendor-specific stays inside that provider. A change to the model path must keep
  `tests/test_provider_parity.py` green: the acceptance scenario has to end the same on every
  provider.
- Default Claude model is `claude-opus-5-5`. Claude model ids and prices live in
  `llm/pricing.py`; update it when adding one. Models of other providers are priced by the
  stage settings (`input_price_per_mtok`, `output_price_per_mtok`).
- Every stage that calls a model must record its usage in `UsageRecord`, including the coding
  agent, because that ledger is what the budgets read.
- Invoke git only through `gitutil.run_git`. Parse its NUL-delimited output (`-z`), never the
  line-based formats: file names are attacker-controlled.
- Validate at the edge. Project settings are range-checked in `schemas.ProjectSettings`; add a
  bound whenever you add a setting, because a bad value otherwise fails later inside a job.
- **A model change that alters the schema needs a migration** (ADR-002): a new revision in
  `src/opsmith/migrations/versions/`, written with `uv run alembic revision --autogenerate`
  (see `docs/development/architecture.md`). Never edit a released revision.
  `tests/test_migrations.py` fails when the models and the revisions disagree. Call the change
  out under *Upgrade notes* in `CHANGELOG.md`.

## Extending

Connectors, analyzers and model providers are plugins (`opsmith.connectors`,
`opsmith.analyzers`, `opsmith.llm_providers` entry points; built-ins use `@register`). The
rules for each, and for adding a model stage, a task state or a setting, are in
[`docs/development/architecture.md`](docs/development/architecture.md#extending-opsmith). In
short:

- **Connector:** read-only; skip and count records it cannot parse; test with
  `httpx.MockTransport`.
- **Analyzer:** never execute repository code; isolate failures per file; mask secrets.
- **Model provider:** implement `LLMProvider`; report usage, including on refusals and
  truncation.
- **LLM stage:** add the output schema, the prompt, and a `HeuristicProvider` branch so the
  stage works offline. Call it through `LLMGateway.structured`.
- **Task state:** extend `TASK_TRANSITIONS`, the UI status chips, `dashboard.FUNNEL`, and add
  an ADR entry.

## Testing

- Tests must not touch the network. Use `httpx.MockTransport` for HTTP and fake clients for
  the Anthropic SDK (see `tests/test_llm.py` and `tests/test_executor.py`).
- Use the `platform`, `seed` and `drain` fixtures. `drain()` runs queued jobs to completion.
  Fixtures use the `local` sandbox and set `local_root` to the test's temp dir.
- Any behaviour change to the pipeline needs a test at the module level, and the spec §11
  assertions in `tests/test_e2e.py` must keep passing.
- Security-relevant code (gates, confinement, redaction, tenancy) needs a negative test that
  proves the bad path is blocked. Put it in `tests/test_hardening.py` or next to its area, and
  list it in `docs/guide/security.md`.
- Tests that need a real Docker daemon carry `@pytest.mark.docker` and skip themselves when
  Docker or the image is unavailable.
- Code must work on SQLite and PostgreSQL. PostgreSQL enforces column lengths and types that
  SQLite ignores; run the suite against it (see Commands) when you touch models or queries.

## Definition of done

- [ ] `make check` passed before the change and passes after it (ruff, format, mypy, the
      suite with the coverage floor, the docs check), with no test newly failing or skipped.
- [ ] New behaviour is covered by tests, including failure and negative paths; a bug fix has
      a test that failed before the fix.
- [ ] Invariants above are untouched, or a human approved the change and an ADR records it.
- [ ] The Markdown docs match the code, and `make site` builds the documentation site.
- [ ] `CHANGELOG.md` describes every user-visible change, in the commit that makes it; once on
      `main`, the change is released (tag and GitHub Release) and the live site shows it.
- [ ] Significant decisions are in `docs/DECISIONS.md` (and design changes in
      `ARCHITECTURE_DECISIONS.md`), with the reasoning.
- [ ] No secrets, tokens or real customer data in code, tests, fixtures, logs or commits.

## Commits and PRs

Use imperative, scoped messages (`executor: block secrets on added lines only`), one topic
per commit. Work on a branch and open a pull request; keep PRs focused, describe the risk and
how you verified the change, and wait for CI to be green. Never commit generated data
(`data/`, `*.db`, demo output, `build/`).
