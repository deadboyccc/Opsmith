# Security model

Opsmith reads production signals and source code, asks a model to reason about them, and can
push code. This document states what it guarantees, how each guarantee is enforced, which test
proves it, and where the guarantees stop. Read the last section before you rely on it.

- [The rules that never bend](#the-rules-that-never-bend)
- [Trust boundaries](#trust-boundaries)
- [Guarantees, enforcement and tests](#guarantees-enforcement-and-tests)
- [What is not protected](#what-is-not-protected)
- [Hardening checklist](#hardening-checklist)
- [Reporting a vulnerability](#reporting-a-vulnerability)

## The rules that never bend

1. **No code change without a recorded human approval.** One function,
   `executor.service.assert_approved`, gates every execution. It runs before the work starts
   and again immediately before anything is pushed.
2. **Opsmith cannot merge.** The SCM interface has no merge operation. A human merges.
3. **Nothing reaches a model unredacted.** Signals are redacted when stored and again in the
   gateway that every model call goes through.
4. **Untrusted text is data.** Logs, code and model output are never treated as instructions,
   and model output is validated before it is used.

If a change would weaken one of these, it is the wrong change.

## Trust boundaries

| Actor or input | Trusted? | Notes |
|---|---|---|
| The operator (sets `OPSMITH_*`, runs the processes) | Yes | Controls everything |
| Admins of a tenant | **Trusted with the worker** | They set repository commands, connector URLs and credential references. See [limits](#what-is-not-protected) |
| Reviewers, viewers | For their role only | Enforced on every route |
| `service` keys (log collectors) | Push signals only | Cannot read anything, cannot use the UI |
| Log lines, metrics, source code, file names | No | Attacker-influenced. Treated as data everywhere |
| Model output | No | Schema-validated; unknown ids and invented evidence are dropped |
| Repository checks (`build`, `lint`, `test`) and anything they run | No | Run only inside the sandbox |
| The coding agent | No | Fixed tool list, no shell, no network, no credentials |

## Guarantees, enforcement and tests

Every row names the tests that try to break the guarantee. They live in `tests/`, mostly in
`tests/test_hardening.py`, and run in CI.

### Human approval

| Guarantee | Enforcement | Tests |
|---|---|---|
| Only a `reviewer` or `admin` approval starts an execution | `assert_approved`: task state, latest review is an approval, reviewer role | `test_no_execution_without_recorded_human_approval`, `test_viewers_cannot_review` |
| An approval covers exactly what the reviewer saw | The approval stores a digest of the task's scope (target repository and branch, title, problem, approach, acceptance criteria, paths, protection level, attached findings). It is re-computed before the run and before the push; any difference voids the approval and returns the task to review | `test_approval_covers_only_the_scope_that_was_reviewed`, `test_finding_attached_after_approval_voids_it`, `test_scope_change_during_execution_blocks_the_push` |
| Nothing rewrites an approved task | The aggregator leaves `approved` and `executing` tasks alone; new evidence waits | `test_approved_task_is_not_changed_by_later_evidence` |
| Two decisions cannot both win | The task row is locked and re-read before a decision is recorded | `test_a_stale_decision_loses_to_the_one_already_recorded`, `test_stale_version_is_rejected` |
| Evidence added while you were reading invalidates your copy | The task version is bumped; `expected_version` rejects a stale approval | `test_evidence_attached_during_review_bumps_the_version` |
| A void approval never strands a task | A refused execution returns the task to the review queue with the reason and a notification | `test_approval_covers_only_the_scope_that_was_reviewed` |
| Protected paths need more than a normal approval | `elevated` paths need an admin with an explicit acknowledgement; `excluded` paths can never be approved | `test_protected_paths_need_elevated_admin_approval`, `test_excluded_paths_cannot_be_approved` |

### What gets pushed

| Guarantee | Enforcement | Tests |
|---|---|---|
| The diff is gated after the last sandboxed command | Protected-path and credential checks run after the platform's own check run; nothing sandboxed runs between the gate and the commit. The sandbox home is removed by exact path first, and a link or file left in its place is deleted, not followed | `test_files_written_by_the_check_run_are_gated_too`, `test_files_named_like_the_sandbox_home_are_gated`, `test_a_sandbox_home_replaced_by_a_link_is_never_committed` |
| File names cannot hide a protected path | Changed files are read from git's NUL-delimited output | `test_unusual_file_names_are_reported_verbatim`, `test_protected_path_gate_cannot_be_bypassed_by_file_name` |
| Every added line is scanned for credentials | The diff parser only treats `+++` as a file header inside a header; the diff is read with `--text`, so a `-diff` or `binary` attribute cannot hide a file's lines. The diff is read as bytes and split at newlines only, so a form feed, a bare carriage return or a Unicode separator cannot cut a line in two and hide its second half | `test_added_lines_that_look_like_diff_headers_are_still_scanned`, `test_secret_gate_cannot_be_bypassed_by_line_prefix`, `test_secret_gate_reads_files_that_git_would_show_as_binary`, `test_secret_scan_survives_binary_files_in_the_diff`, `test_a_secret_after_a_line_break_lookalike_is_still_scanned`, `test_secret_gate_cannot_be_bypassed_with_a_form_feed` |
| A protected path is protected however its pattern is written | A pattern covers what it matches as a glob and everything below a directory it names; a leading `/` or `./` is ignored; a pattern that can never match is refused when saved | `test_a_directory_pattern_protects_the_directory_however_it_is_spelled`, `test_a_protected_path_pattern_that_can_never_match_is_refused` |
| Pushes go only to `opsmith/...` branches, never the default branch | `scm.base.check_pushable` in every provider; branch names are validated | `test_pushes_stay_inside_the_opsmith_branch_namespace`, `test_github_refuses_default_branch_and_missing_token` |
| No merge | The provider interface has no such method | `test_scm_interface_has_no_merge_capability` |

### The sandbox

| Guarantee | Enforcement | Tests |
|---|---|---|
| Repository code runs without network, with a read-only root, no capabilities and no Docker socket | `DockerSandbox` flags | `test_docker_sandbox_really_isolates` (real containers), `test_docker_sandbox_isolation_flags` |
| The whole execution path works through the Docker sandbox | | `test_full_execution_through_the_docker_sandbox` (real containers) |
| The unconfined `local` backend cannot run real repositories | `sandbox_policy_error`: `local` only for the `local` SCM provider | `test_local_sandbox_is_refused_for_remote_repositories` |
| No silent downgrade when Docker is missing | `SandboxUnavailable`; the task returns to review | `test_missing_docker_fails_closed`, `test_sandbox_backend_defaults_to_docker_and_is_validated` |
| Sandboxed code cannot make the worker run something | Git metadata is snapshotted before the agent starts; later git commands use only the snapshot, with hooks and fsmonitor disabled. A nested repository (whose config git would read when it descends into a submodule) refuses the execution before the platform runs git on the workspace | `test_git_hooks_and_config_planted_by_sandboxed_code_never_run`, `test_a_nested_repository_planted_by_sandboxed_code_is_refused` |
| Repository settings cannot make git run something on the worker | A repository's address and default branch are checked when saved and again by the provider on every use: an https URL without credentials for GitHub, a plain branch name. git gets `--` before them and, for GitHub, may use https only | `test_a_github_repository_must_be_an_https_url_and_a_plain_branch`, `test_the_github_provider_refuses_a_stored_repository_it_must_not_fetch`, `test_a_local_repository_branch_cannot_be_a_git_option`, `test_apply_checks_a_repository_like_the_api_does` |
| The agent stays inside the checkout | `Workspace.resolve` rejects absolute paths, `..`, symlinks out, and `.git` at any depth | `test_workspace_confinement`, `test_workspace_refuses_oversized_reads`, `test_the_agent_cannot_write_into_a_nested_git_directory` |
| The agent's environment holds no credentials | Scrubbed environment; the remote is removed | `test_sandbox_environment_is_scrubbed` |
| A timeout kills everything the command started | Process group kill (local); container kill by name (Docker) | `test_local_sandbox_timeout_kills_the_whole_process_group`, `test_docker_sandbox_really_isolates` |

### Data and models

| Guarantee | Enforcement | Tests |
|---|---|---|
| Secrets and PII are redacted before storage and before every model call | `redaction.py`, applied in ETL and in `LLMGateway` | `tests/test_redaction.py`, `test_gateway_redacts_before_provider_and_guards_injection` |
| Untrusted text cannot escape its data block in a prompt | The payload is escaped so it cannot contain the closing tag; so is the validator's report quoted on a corrective retry | `test_untrusted_content_cannot_close_its_own_data_block`, `test_the_validation_report_cannot_close_its_own_framing` |
| Invented ids and evidence are discarded | The orchestrator checks every reference against the run's input | `test_untrusted_model_output_is_contained` |
| Only allowed providers receive data | Process allowlist intersected with the project's, for every provider | `test_provider_allowlist_is_enforced`, `test_openai_provider_is_subject_to_the_allowlists` |
| Project configuration cannot redirect model calls | Model endpoints and their keys are operator settings; a project chooses only a provider name and a model | `test_settings_and_default_gateway` |
| An audited repository cannot reconfigure the scanners that audit it | ruff runs `--isolated`; gitleaks gets the platform's config (`--config`); trivy runs from an empty directory, away from a committed `trivy.yaml` or `.trivyignore`; file names follow `--` | `test_file_names_are_never_read_as_scanner_options`, `test_scanner_configuration_in_the_checkout_is_not_used`, `test_real_gitleaks_ignores_an_allowlist_committed_to_the_repository` (real gitleaks), `test_real_trivy_ignores_a_config_committed_to_the_repository` (real trivy) |
| Spending is capped for every stage, and failed calls still count | `BudgetGuard`; usage is recorded for failed, refused and truncated calls, including Claude's (checked with the real SDK). The coding agent writes to the same ledger, does not start when the day's budget is spent, and stops at the per-run cap or what is left of the day | `test_budgets_per_run_and_per_day`, `test_billed_failures_are_recorded_before_they_surface`, `test_coding_agent_spending_is_recorded_and_counts_toward_the_daily_budget`, `test_an_exhausted_daily_budget_stops_the_execution_before_any_model_call`, `test_the_agent_may_only_spend_what_is_left_of_the_day`, `test_coding_agent_stops_when_the_cost_budget_is_spent` |
| The guarantees do not depend on the model provider | Every provider goes through the same gateway, and every coding agent through the same tool dispatcher and workspace | `test_acceptance_scenario_through_an_openai_compatible_endpoint`, `test_the_scenario_ends_the_same_whichever_provider_runs_it` |

### Access control

| Guarantee | Enforcement | Tests |
|---|---|---|
| Another tenant's objects look like missing ones | `api.deps.owned` returns 404 | `test_cross_tenant_ids_are_indistinguishable_from_missing`, `test_tenant_isolation` |
| Roles are enforced on every route | `require_role` | `test_auth_required_and_roles_enforced`, `test_ui_settings_editor_is_admin_only` |
| `service` keys can push and nothing else | Read routes and the UI require a human role | `test_service_keys_can_push_but_not_read` |
| Objects cannot be linked across projects | Project checks on create and trigger | `test_objects_cannot_be_linked_or_triggered_across_projects` |
| API keys are not stored | SHA-256 hashes only; the key is shown once | `tests/test_cli.py` |
| A credential reference cannot name the platform's secrets or arbitrary files | `config.check_credentials_ref`: no `OPSMITH_*`, database or model-provider key variables; `file:` only under `OPSMITH_SECRETS_DIR`, and never the file holding a model-provider key | `test_tenant_credential_references_cannot_reach_platform_secrets`, `test_configuration_input_is_validated`, `test_values_too_large_to_schedule_are_refused_when_saved` |
| Configuration cannot point the worker at arbitrary host files | File connectors and `local` repositories must resolve inside `OPSMITH_LOCAL_ROOT` and outside the data directory; unset means disabled. A file pattern may not contain `..`, and a refusal never names the file a pattern matched. A `github` repository cannot be a path on the worker | `tests/test_ingestion_hardening.py`, `test_file_connector_glob_cannot_climb_out_or_name_what_it_finds`, `test_a_github_repository_must_be_an_https_url_and_a_plain_branch` |
| A caller without a key cannot exhaust the process | Request bodies are limited before they are read (1 MiB; 32 MiB for pushed signals); the metrics labels are a fixed set; a connector request has a total deadline as well as a read timeout | `test_a_request_body_over_the_limit_is_refused_before_it_is_read`, `test_unknown_http_methods_share_one_metrics_label`, `test_a_connector_request_has_a_total_deadline` |
| Input is bounded where it is stored | Review text, edited fields, settings and identifiers are limited to their columns; a redaction pattern must match at least one character | `test_review_text_is_bounded`, `test_a_redaction_pattern_that_matches_nothing_at_all_is_refused`, `test_values_longer_than_their_columns_are_refused_when_saved` |
| The UI resists cross-site requests | HttpOnly, SameSite=Strict cookie; the `Origin` of every form post is checked; strict Content-Security-Policy. The API accepts the UI's cookie for reads only; a call that changes something needs the key in a header | `tests/test_ui_hardening.py`, `test_the_api_takes_the_ui_cookie_for_reading_only`, `test_health_metrics_and_security_headers` |

### Reliability that safety depends on

| Guarantee | Enforcement | Tests |
|---|---|---|
| A slow job is not run twice | Lease heartbeat while the worker is alive | `test_slow_job_keeps_its_lease_through_the_heartbeat`, `test_worker_that_lost_its_lease_does_not_overwrite_the_new_owner` |
| A job that keeps crashing its worker surfaces | Dead-lettered when its attempts are used up | `test_job_that_keeps_killing_its_worker_is_dead_lettered` |
| One bad schedule cannot stop the others | Disabled and audited, the rest proceed | `test_one_broken_schedule_does_not_stop_the_others` |
| Settings that would break jobs are rejected up front | Range checks on project settings; impossible cron expressions refused | `test_project_settings_reject_values_that_would_break_jobs`, `test_impossible_or_malformed_cron_is_rejected_up_front` |
| Finished analysis runs cannot be altered | A flush guard raises | `test_finished_runs_are_immutable_and_slots_idempotent` |

## What is not protected

Be clear-eyed about these. None is hidden in the code; all are choices or open work.

- **Tenants are a data boundary, not a security boundary between hostile parties.** Every
  tenant's jobs run on the same workers. A tenant admin chooses connector URLs, repository
  check commands and credential references, and all tenants share one namespace of
  environment variables and secret files for those references. An admin can therefore make the
  worker call URLs on your network and can reference a credential that belongs to another
  tenant. Run a separate deployment for parties that do not trust each other.
- **Connector URLs can reach your private network.** Loki and Prometheus usually live there, so
  private and loopback addresses are allowed. Link-local addresses (cloud metadata endpoints)
  are refused. DNS rebinding is not defended against. The same holds for a GitHub
  repository's https address, which may name any host (GitHub Enterprise).
- **A signal named by the model is trusted.** Evidence references are checked against what
  the model was shown; the `signal` of a finding is not. A finding with valid evidence and an
  invented signal is verified, after its fix is merged, against a signal that never recurs.
  See the [security audit](../SECURITY-AUDIT.md).
- **The worker with the sandbox overlay holds the Docker socket,** which is root on that host.
  The sandboxed code never gets the socket, but a compromise of the worker itself would.
- **The Docker sandbox is a container, not a virtual machine.** A kernel or container-runtime
  escape would break it. Use a dedicated host; a microVM runtime is future work.
- **The `local` sandbox isolates nothing.** It exists for development and the demo, and is
  refused for real repositories.
- **Redaction is pattern-based.** It catches common credential and PII formats and values of
  sensitive-looking keys. It cannot recognize a secret with no recognizable shape. Add
  `redaction_extra_patterns` for your own formats.
- **Admin-supplied regular expressions are trusted** (`redaction_extra_patterns`,
  `policy_rules`, file-connector `pattern`). A pathological pattern can stall a worker.
- **Per-finding suppressions in the audited repository are honoured.** A `.gitleaksignore`
  entry (one file, rule and line) or a `nosemgrep` comment hides that one finding from that
  scanner, as their authors intend; gitleaks offers no way to turn the file off. Opsmith's own
  secrets analyzer does not read them. Repository-wide scanner configuration is not used.
- **The credential scan on diffs is the same pattern set.** It is a backstop, not a guarantee
  that an agent cannot write a secret.
- **Your data goes to the model provider you configure.** Redaction removes recognizable
  secrets and personal data first, but the rest of the bundle (log templates, metric names,
  code excerpts) is sent. Use a local endpoint, or `heuristic`, if that is not acceptable.
- **A capable model can still be misled** by text in logs or code into proposing a bad task.
  The defence is structural: it can only produce findings, which a human must approve, which
  become a pull request a human must merge.
- **`/healthz` and `/metrics` are unauthenticated.** Keep the API port private.
- **API keys are bearer credentials** with no expiry or rotation yet. SSO is future work.

## Hardening checklist

- [ ] `POSTGRES_PASSWORD` is long and random; the database is not reachable from outside.
- [ ] `OPSMITH_SANDBOX_BACKEND=docker` (the default) on every worker that handles real
      repositories; the worker runs as a non-root user on a dedicated host or VM.
- [ ] The sandbox image contains your toolchain and dependencies, and nothing secret.
- [ ] The GitHub token is fine-grained: contents and pull requests read/write, checks read, on
      the needed repositories only. The default branch is protected.
- [ ] `OPSMITH_LOCAL_ROOT` is unset unless you use file connectors or local repositories.
- [ ] `OPSMITH_LLM_PROVIDER_ALLOWLIST` lists only the providers you intend to use, and
      `OPSMITH_OPENAI_BASE_URL` points where you mean it to.
- [ ] Budgets are set to what you are willing to spend per run and per day.
- [ ] Protected paths cover your CI configuration, authentication code and secrets.
- [ ] The API is behind TLS; `/metrics` and `/healthz` are not public. The proxy limits
      request size and rate (Opsmith limits size, not rate).
- [ ] `service` keys go to collectors; people get `viewer`, `reviewer` or `admin` keys.
- [ ] Dead jobs and failed executions notify someone.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private vulnerability
reporting on this repository (*Security* tab, *Report a vulnerability*) or email the maintainer
listed in `pyproject.toml`. Include what you did, what happened and what you expected. You will
get an acknowledgement, and credit in the changelog if you want it.
