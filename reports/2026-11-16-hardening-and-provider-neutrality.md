# Work report: hardening, provider neutrality and documentation

**Date:** 16 November 2026 · **Scope:** commits `899053a` to `70a7575` on `main` (17 commits)

This report summarizes one body of work on Opsmith: a review of the repository against the
[independent project review](../docs/development/independent-project-review.md), a bug-fix and
hardening pass, removal of the dependency on a single model vendor, and a rewrite of the
documentation. It contains no credentials, keys, addresses or other sensitive data.

- [Summary](#summary)
- [Starting point](#starting-point)
- [What was done](#what-was-done)
- [How it was verified](#how-it-was-verified)
- [Commits](#commits)
- [Breaking changes](#breaking-changes)
- [What is still open](#what-is-still-open)
- [Follow-ups for the maintainer](#follow-ups-for-the-maintainer)

## Summary

| | Before | After |
|---|---|---|
| Tests | 113 | 365 (plus 3 opt-in live tests) |
| Test coverage | 89% | 92% |
| Database backends the full suite runs on | SQLite (PostgreSQL for the acceptance tests only) | SQLite and PostgreSQL |
| Model providers | Offline rules, Claude | Offline rules, Claude, any OpenAI-compatible endpoint, plugins |
| Sandbox default | Unconfined subprocess | Docker container; the unconfined one is refused for real repositories |
| Database password in Compose | Fell back to a known value | Required; the stack refuses to start without it |
| Documentation | One README, a specification, decision records | A user guide and a contributor guide, checked against the code in CI |
| CI | Intermittently failing | Green, including a weekly run against a real local model |

In numbers: 116 files changed, about 11,900 lines added and 1,300 removed. Roughly 3,600 of
the added lines are source code, 4,700 are tests, and the rest is documentation and tooling.

Scope was held deliberately. The review's advice was to stop adding features and prove the
existing loop. Apart from provider neutrality, which was requested, only two small additions
were made, both because an existing feature did not work without them: push-only connector
types, and an overlay that lets a containerized worker use the Docker sandbox.

## Starting point

The independent review rated the concept and architecture 8/10 and maturity evidence
6.5 to 7/10, and listed three top-priority gaps:

1. The Compose file fell back to a known database password.
2. The sandbox mode was a recommendation, not an enforced policy.
3. An approval was not bound to the exact work it authorized.

It also asked for adversarial tests behind each safety claim, a hardened container boundary,
and documentation that makes the project easier to evaluate.

## What was done

### 1. The review's top priorities

- **Database password.** `docker-compose.yml` requires `POSTGRES_PASSWORD`. `opsmith init` no
  longer prints the password as part of the database URL.
- **Sandbox policy, enforced in code.** Docker is the default backend. The unconfined `local`
  backend is refused for any repository outside the local SCM provider. A missing Docker daemon
  refuses the execution instead of falling back.
- **Approval bound to the work.** Each approval stores a digest of the task's scope (target,
  problem, approach, acceptance criteria, paths, protection level, attached findings). It is
  re-checked before the run and again before the push; previously the second check ran after
  the branch had already been pushed. Concurrent decisions on one task are serialized.

### 2. Defects found and fixed

The hardening pass read the whole codebase and reproduced each defect before fixing it. Every
fix has a regression test. The most significant:

**Ways around a safety gate**

| Defect | Effect |
|---|---|
| A file named like `dir/x -> y` was reported as `y` | A change under a protected path could pass the protected-path gate |
| An added line starting with `++ ` was read as a diff header | That line was never scanned for credentials |
| The platform's own check run happened after the gates | Files written during it were committed ungated |
| Sandboxed code could write git hooks and configuration into the checkout | The worker executed them when committing, outside the sandbox |
| The aggregator attached new evidence to already-approved tasks | Model-chosen paths and severities changed what a human had approved |
| A log line containing the closing data tag ended the untrusted block in a prompt | Prompt-injection framing could be escaped |
| File names were passed to external scanners without a separator | A file name could be read as a scanner option |

**Cross-tenant and credential exposure**

| Defect | Effect |
|---|---|
| File connectors accepted any path on the worker | One tenant could ingest another tenant's stored signals |
| A credential reference could name any environment variable or file | The platform's own secrets could be sent to a URL chosen in project configuration |
| Connector URLs were unrestricted | Cloud metadata addresses were reachable; responses were unbounded |
| `service` API keys could read everything | Keys meant only for pushing signals exposed tasks, configuration and the audit log |
| The secrets analyzer masked only the first secret on a line | A second one was stored in clear text in the finding |

**Budgets and cost accounting**

| Defect | Effect |
|---|---|
| Failed, refused and truncated model calls were not recorded | Cost caps never tripped for a failing stage |
| An unlisted model id was priced at zero | Cost caps were disabled for it |
| The coding agent's spending was recorded on the run only | The most expensive stage was outside both cost caps |

**Reliability**

| Defect | Effect |
|---|---|
| Job leases were never renewed | A job longer than its lease was handed to a second worker while the first was still running |
| A job that kept crashing its worker was retried forever | It never reached the dead-letter state |
| One schedule that could not be computed raised inside the scheduler | Every schedule for every tenant stopped |
| One malformed log record raised inside a connector | The connector never advanced and re-read the same data forever |
| A non-finite metric value or a timestamp without a time zone | Bundle building failed for the whole project, permanently |
| Loki results beyond the page limit were dropped | The newest lines in a busy window were silently lost |
| One malformed file crashed an analyzer | That analyzer's findings were lost for the whole repository, and the audit still advanced |
| The demo named its tenant after the current second | Two runs within one second failed; this is what made CI fail intermittently |

More than sixty defects were fixed in total. The [changelog](../CHANGELOG.md) lists all of them.

### 3. No model vendor lock-in

- A new `openai` provider speaks the OpenAI-compatible Chat Completions protocol over plain
  HTTP, with no vendor SDK. It works with OpenAI, Azure OpenAI, Ollama, vLLM, LM Studio,
  LiteLLM, OpenRouter and similar servers, and adapts to servers that lack schema-enforced
  output or expect a different token-limit parameter.
- A matching coding agent drives the same fixed tool set through the standard tool-calling
  protocol. All three agents now share one tool dispatcher and one task brief.
- The endpoint is operator configuration. A project chooses a provider name and a model, never
  the address model calls go to.
- Prices can be set per stage so cost budgets work for any model, including zero for a local
  one. A model with no price is costed at the most expensive known rate, never at zero.
- Other providers can be added as plugins through an entry point.
- Claude remains available and the offline rule-based provider remains the default.

### 4. Deployment

- `docker-compose.sandbox.yml` is an explicit overlay that gives a containerized worker a real
  sandbox. Its trade-off, that the worker then holds the Docker socket, is stated where it is
  enabled.
- The sandbox container runs with no network, a read-only root filesystem, no capabilities, a
  non-root user and resource limits. A Docker failure is reported as an unavailable sandbox,
  not as failing checks.
- `.env.example` documents every setting. An invalid setting stops the process at start with a
  message naming it.

### 5. Documentation

Rewritten and split by reader, with an [index](../docs/README.md):

- **Using Opsmith** (`docs/guide/`): getting started, a UI tour with screenshots, concepts,
  configuration, models, deployment, operations, API and security.
- **Developing Opsmith** (`docs/development/`): architecture for contributors and testing, plus
  `CONTRIBUTING.md` and the updated `AGENTS.md` and decision records.
- The [security page](../docs/guide/security.md) maps every guarantee to the code that enforces
  it and the tests that try to break it, and states plainly what is not protected.
- The [review response](../docs/development/review-response.md) answers the independent review
  item by item.
- The screenshots are generated from the offline demo, whose data is entirely synthetic.

## How it was verified

Claims in this report rest on things that were run, not only on tests passing.

| What | How |
|---|---|
| Lint, formatting, types | `ruff` and `mypy` clean on every pushed commit |
| Unit and integration tests | 365 tests, 92% coverage; each milestone re-checked in a clean checkout before it was pushed |
| PostgreSQL | The whole suite, with a fresh database per test, locally and in CI |
| Sandbox isolation | Real containers: no network, read-only root, no capabilities, no Docker socket inside, timeout cleanup, and a complete execution |
| Compose deployment | Built and started; tenant created; the shipped example configuration applied; the worker started sandbox containers through the overlay |
| Provider neutrality | The acceptance scenario run through the OpenAI-compatible HTTP path, required to end identically to the offline run |
| A real model | Structured output, a pipeline stage through the gateway, and the tool-calling agent loop against an Ollama server with a small open model, locally and in CI |
| Getting-started guide | Every command run as written, from an empty directory to a pull request branch containing the fix |
| Regression tests | New tests were checked to fail without their fix |
| Documentation | A CI check compares the docs with the code: links, heading anchors, cited tests, environment variables, project settings, API endpoints, notification events, connector types, states, providers and commands. It was shown to catch deliberately broken docs |
| CI | Green on the last six pushes; the weekly live-model workflow passed on its first run |

## Commits

| Commit | Subject |
|---|---|
| `899053a` | Harden the execution path, review gate, queue and scheduler |
| `a621410` | Confine local repositories to `OPSMITH_LOCAL_ROOT` |
| `fb61ba8` | Aggregator: never change a task a human has approved |
| `30f1a34` | Deployment: fail closed on the database password; usable Docker sandbox in Compose |
| `175ec7b` | LLM gateway and analysis: count every billed call, bound model output |
| `4b316a4` | Ingestion: confine connectors, survive bad input, stop losing and duplicating data |
| `b05416a` | Audits and UI: close scanner injection, keep findings, tighten the forms |
| `2d115f6` | Tests and CI: whole suite on PostgreSQL, hermetic scanners, compose checks |
| `621a130` | Add push-only connector types |
| `294534d` | No model vendor lock-in: any OpenAI-compatible endpoint, and provider plugins |
| `9bfca6c` | Rewrite the documentation for readers, operators and contributors |
| `682534f` | Fix the flaky demo test that failed CI; update workflow actions |
| `4065e93` | Count coding-agent spending against the budgets; prove provider parity |
| `c465a2e` | Add a UI tour with screenshots from the offline demo |
| `5d1bfb9` | Docs: separate using Opsmith from developing it; verify every command |
| `d27f21d` | Check the docs against the code in CI; live-model workflow |
| `70a7575` | README: size the screenshot; check HTML image and link tags in the docs |

## Breaking changes

Anyone with an existing installation should read the upgrade notes in the
[changelog](../CHANGELOG.md). In short:

- The database schema changed and there are no migrations yet, so a fresh database is needed.
- `POSTGRES_PASSWORD` is required by Compose.
- The sandbox default is `docker`. Development with local repositories needs
  `OPSMITH_SANDBOX_BACKEND=local`.
- File connectors and local repositories are disabled until `OPSMITH_LOCAL_ROOT` is set.
- `file:` credential references must live under `OPSMITH_SECRETS_DIR`, and `env:` references
  may not name the platform's own variables.
- `service` API keys can no longer read anything.
- Approvals recorded before this work are not honoured; approve those tasks again.
- The sandbox has no network, so its image must already contain a repository's dependencies.

## What is still open

Stated plainly, so nobody reads more into this than was done.

- **Not tested against live services:** Claude (fake clients only), GitHub, Loki and Prometheus
  (mocked HTTP only).
- **No quality measurements.** Precision of findings, share of pull requests merged and
  verified-fix rate need a labelled evaluation set and real usage. This was the review's
  remaining first-priority item and was not attempted.
- **No schema migrations.**
- **No production use, load test or long-running deployment.**
- **Tenants are a data boundary, not a security boundary between hostile parties.** They share
  workers and one credential namespace. Mutually untrusting parties need separate deployments.
- **The Docker sandbox is a container, not a virtual machine,** and a containerized worker
  needs the Docker socket to start sandboxes.
- **Redaction is pattern-based,** and regular expressions supplied by admins are trusted.
- One sandbox image serves every repository a worker handles.

The [security page](../docs/guide/security.md#what-is-not-protected) and the
[decision records](../ARCHITECTURE_DECISIONS.md) have the full list.

## Follow-ups for the maintainer

- Rotate the access token that was used for this work.
- Set the repository's About description and topics; the token used did not have permission to
  change repository settings.
- Consider enabling private vulnerability reporting, which the security page points readers to.
- The next pieces of work with the most value: an evaluation set with published results, schema
  migrations, and a live GitHub and Claude run in CI.
