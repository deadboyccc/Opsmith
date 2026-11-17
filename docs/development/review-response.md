# Response to the independent project review

[`independent-project-review.md`](independent-project-review.md) assessed Opsmith from its README
and deployment configuration on 16 November 2026 and listed improvements by priority. This page
records, item by item, what was done about each one, and what was deliberately left open.

The review's bottom line was to stop adding features and prove that the existing loop is
secure, repeatable, measurable and easy to deploy. The work below follows that: it adds no
product features.

## Summary

| Review item | Status |
|---|---|
| P0 · Fail closed on the database password | **Done** |
| P0 · Make the sandbox mode an enforceable policy | **Done** |
| P0 · Bind approval to the exact work being authorized | **Done**, with one stated limit |
| P1 · Harden the container boundary | **Done** for what containers can give; microVMs remain future work |
| P1 · Turn the claims into security and reliability tests | **Done** for the listed scenarios |
| P1 · Publish quality and operational measurements | **Not done.** Needs real usage data |
| P2 · Make the project easier to evaluate | **Partly done**: documentation, a demo video and a connector version matrix. The About section is a repository setting (text in [Repository About text](repository-about.md)); a roadmap remains |

## P0 · Fail closed on the database password

`docker-compose.yml` no longer falls back to a known password. `POSTGRES_PASSWORD` is required;
without it `docker compose` stops with a message that says what to set. `.env.example` ships it
empty with a command to generate one. CI asserts that the file refuses an empty password.
`opsmith init` no longer prints the database password either.

## P0 · Make the sandbox mode an enforceable policy

- `OPSMITH_SANDBOX_BACKEND` now defaults to `docker`. `local` must be chosen explicitly and is
  documented as development-only.
- The executor **refuses** to run a repository that is not on the `local` SCM provider with the
  `local` sandbox. This is code, not documentation.
- If Docker is selected but unavailable, the execution is refused and the task returns to
  review. There is no fallback to a weaker sandbox.
- The Compose stack does not silently execute without isolation. Execution in Compose needs the
  explicit `docker-compose.sandbox.yml` overlay, whose trade-off (the worker holds the Docker
  socket) is stated in the file and in [Deployment](../guide/deployment.md#execution-sandbox).

## P0 · Bind approval to the exact work being authorized

- Each approval stores a digest of the task's scope: task, repository, branch, title, problem,
  approach, acceptance criteria, paths, protection level and the attached findings.
- The executor recomputes it before the run and again **before the push** (previously the second
  check came after the push). A mismatch voids the approval and returns the task to review.
- The aggregator no longer modifies a task that is approved or executing.
- Two simultaneous decisions on one task are serialized; a stale one is refused.
- A refused execution can no longer leave a task parked in `approved`.

Tested: stale approvals, a scope edited after approval, a finding attached after approval, a
scope changed during execution, duplicate and conflicting decisions, and edits to every
editable field.

**Limit.** The approval is bound to the task (the plan), not to a diff, because the diff does not
exist until the agent has run. The diff is constrained by the gates and reviewed by a human in
the pull request, which records the base commit. Binding to the base commit at approval time
would need a checkout at review time and was judged not worth the added machinery.

## P1 · Harden the container boundary

Sandbox containers now run with no network, a read-only root filesystem, a size-limited `/tmp`,
all capabilities dropped, `no-new-privileges`, a non-root user, CPU, memory (no swap) and
process limits, an init process, and only the checkout mounted. No Docker socket and no
credentials are passed in. A timed-out container is killed by name and removed.

Two escapes that did not depend on the container were closed:

- Sandboxed code could plant git hooks or configuration in the checkout's `.git`, which the
  worker would then execute when committing. Opsmith now commits and pushes from a snapshot of
  the git metadata taken before the agent starts, with hooks disabled.
- Files written by the platform's own check run were committed without passing the diff gates.
  The gates now run after the last sandboxed command.

These are verified with **real containers** in the test suite (network, filesystem,
capabilities, socket, timeout cleanup, and a complete execution).

**Open.** Access to cloud metadata and internal networks is prevented by `--network none`; that
is tested by attempting an outbound connection. A container is still not a VM: microVM
isolation is future work.

## P1 · Turn the claims into security and reliability tests

| Scenario the review named | Where it is tested |
|---|---|
| Cross-tenant IDOR | `test_cross_tenant_ids_are_indistinguishable_from_missing`, `test_tenant_isolation` |
| Role escalation | `test_service_keys_can_push_but_not_read`, `test_auth_required_and_roles_enforced`, `test_viewers_cannot_review` |
| Secret redaction | `tests/test_redaction.py`, `tests/test_ingestion_hardening.py` |
| Prompt injection in logs and source code | `test_untrusted_content_cannot_close_its_own_data_block`, `test_gateway_redacts_before_provider_and_guards_injection` |
| Invented evidence | `test_untrusted_model_output_is_contained` and the bounds tests in `tests/test_inference.py` |
| SSRF in connectors | `tests/test_ingestion_hardening.py` (link-local and metadata addresses refused); see the stated limit in [Security](../guide/security.md#what-is-not-protected) |
| Queue duplicate delivery | `test_enqueue_is_idempotent`, `test_slow_job_keeps_its_lease_through_the_heartbeat`, `test_worker_that_lost_its_lease_does_not_overwrite_the_new_owner` |
| Crash recovery | `test_expired_leases_are_recovered`, `test_job_that_keeps_killing_its_worker_is_dead_lettered` |
| Dead-job retry | `test_worker_success_retry_and_dead_letter`, `test_trigger_and_dead_job_retry` |
| Audit-log and run immutability | `test_finished_runs_are_immutable_and_slots_idempotent` |

Writing these tests found real defects, all fixed; the [changelog](../../CHANGELOG.md) lists them.

**Open.** The audit log is append-only by convention in the application; it is not protected
against someone with direct database access.

## P1 · Publish quality and operational measurements

Not done. Precision and recall of findings, approval and merge rates, verified-fix rate,
time-to-triage and per-task cost need a labelled evaluation set and real usage. Opsmith records
the raw material (every run with its prompt and model version, cost per call, the task funnel,
verification outcomes), but no benchmark has been built or published. Until one is, treat
finding quality as unmeasured.

## P2 · Make the project easier to evaluate

Done:

- Documentation rewritten and split by reader ([index](../README.md)). For people using
  Opsmith: [Getting started](../guide/getting-started.md), [Concepts](../guide/concepts.md),
  [Configuration](../guide/configuration.md), [Models](../guide/models.md),
  [Deployment](../guide/deployment.md), [Operations](../guide/operations.md),
  [API](../guide/api.md) and [Security](../guide/security.md). For people changing it:
  [CONTRIBUTING.md](../../CONTRIBUTING.md), [AGENTS.md](../../AGENTS.md),
  [Architecture](architecture.md) and [Testing](testing.md). Every link and every cited test
  is checked in CI.
- A [changelog](../../CHANGELOG.md).
- The README states plainly what has been tested against real services and what only against
  fixtures, and no longer describes pull requests as "ready to merge".

- Screenshots of the review queue, a task with its provenance chain and every other page, in
  the [UI tour](../guide/ui-tour.md). They are generated from the offline demo, so they can be
  regenerated when the UI changes.
- A step-by-step [getting-started guide](../guide/getting-started.md) whose commands were run
  as written.

Done since: a recorded demo and a vertical reel ([UI tour](../guide/ui-tour.md#how-the-pictures-and-videos-are-made)),
and a tested connector and version matrix ([Compatibility](../guide/compatibility.md), run in
CI). The About description and topics are a repository setting; their text is in
[Repository About text](repository-about.md). Not done: a public roadmap.

## Roadmap items from the review

| Timeframe in the review | Exit criterion | State |
|---|---|---|
| First 1 to 2 weeks | No default DB password; production cannot use the local sandbox; approval bound to immutable work; threat model and adversarial tests | Met |
| Next 30 days | Fresh-install guide; reproducible demo; tests on PostgreSQL; GitHub PR flow; documented connector versions; crash, retry and tenant-isolation evidence | Partly: the guide, the demo, the full suite on PostgreSQL, the connector version matrix and the reliability tests exist. A live GitHub flow in CI and a connector version matrix do not |
| Next 60 days | Public benchmark; measured fix and verification rates; sample PR; comparison with manual triage; documented limitations | Only the documented limitations |
