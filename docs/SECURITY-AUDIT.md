# Security audit

An audit of the whole code base, carried out in November 2026 against release 1.2.1. It
covers what was looked for, what was found, what was fixed in 1.2.2 and what is still open.
The standing description of what Opsmith guarantees, and which test proves each guarantee,
is [Security](guide/security.md); this page is the record of one audit.

It does not cover code added after 1.2.1: the VictoriaLogs connector and serving under a base
path (1.4.0), and checks (1.5.0, ADR-019), which send an administrator's instruction to the
model.

- [Scope and method](#scope-and-method)
- [Summary](#summary)
- [Fixed in 1.2.2](#fixed-in-122)
- [Open findings](#open-findings)
- [Checked and found sound](#checked-and-found-sound)
- [Tool results](#tool-results)

## Scope and method

Everything under `src/opsmith/`, the container and Compose files, the example configuration
and the CI workflows. The audit looked for: injection (command, option, prompt), flaws in
authentication and authorization, missing input validation, unsafe deserialization, SSRF,
path traversal, secrets in code or history, insecure defaults, weak cryptography, sensitive
data in logs, missing timeouts and limits, and vulnerable dependencies.

The code was read in full, in five parts. A suspected defect was only recorded once it was
reproduced; every fix has a test that failed before it and is named below. Severity is rated
for a deployment as the documentation describes it: one operator, tenant admins who
configure projects, the API port behind a proxy.

| Severity | Meaning here |
|---|---|
| High | Breaks one of the [rules that never bend](guide/security.md#the-rules-that-never-bend), or lets a configured party run code on a worker or read another tenant's data |
| Medium | Weakens a documented guarantee, or lets a caller without a key degrade the service |
| Low | Hardening: no demonstrated impact on its own |

## Summary

| | High | Medium | Low |
|---|---|---|---|
| Fixed in 1.2.2 | 2 | 6 | 4 |
| Fixed in 1.2.3 | 0 | 0 | 1 |
| Open | 0 | 2 | 4 |

No vulnerable dependency, no secret in the code or its history, no use of unsafe
deserialization (`pickle`, `yaml.load`, `eval`) and no weak cryptography were found.

## Fixed in 1.2.2

### High

**H1. A repository's address and branch were handed to git unchecked: command execution on
the worker, and reading other tenants' checkouts.**
`scm/github.py`, `scm/local.py`, `api/routes.py`, `configfile.py`.
A `github` repository accepted any string as `url` and as `default_branch`, and both went
to `git clone` and `git fetch` as bare arguments.

- A `url` that is a path (or `file://`) made the GitHub provider clone any git repository
  the worker can read, including another tenant's checkout under the data directory. This
  went past `OPSMITH_LOCAL_ROOT`, which confines only the `local` provider.
- A `default_branch` such as `--upload-pack=<command>` was read by `git fetch` as an option
  on the repository's second audit; for a local address git then ran the command on the
  worker, outside the sandbox. Reproduced with a harmless command.

*Who could do it:* a tenant admin, through the API or `opsmith apply`.
*Fix:* the address must be an `https://` URL without credentials and the branch a plain
name. Both are checked when saved and again by the providers on every use, so a row written
earlier is refused too. git gets `--` before them and, for GitHub, may use https only.
*Tests:* `test_a_github_repository_must_be_an_https_url_and_a_plain_branch`,
`test_the_github_provider_refuses_a_stored_repository_it_must_not_fetch`,
`test_a_local_repository_branch_cannot_be_a_git_option`,
`test_apply_checks_a_repository_like_the_api_does`.

**H2. A credential placed after a form feed passed the push-time credential scan.**
`gitutil.staged_added_lines`.
The staged diff was split with `str.splitlines`, which also breaks at form feeds, bare
carriage returns and seven other characters. The second half of an added line then did not
start with `+` and was never scanned. A bare carriage return was also turned into a newline
by the text-mode pipe git's output was read through.

*Who could do it:* whatever writes the change: the coding agent (which reads untrusted
code and logs) or a check command in the repository.
*Fix:* git's output is read as bytes and the diff is split at newlines only.
*Tests:* `test_a_secret_after_a_line_break_lookalike_is_still_scanned` (nine characters),
`test_secret_gate_cannot_be_bypassed_with_a_form_feed`.

### Medium

**M1. Protected-path patterns that matched nothing.** `protection.py`.
`infra/`, `/infra/**`, `./infra/**` and `infra` are how people write "the infra directory".
None matched any path, so a directory its admin believed protected was not, with no error.
*Fix:* a pattern also covers everything below a directory it names, a leading `/` or `./`
is ignored, and a pattern that can never match is refused when saved.
*Tests:* `test_a_directory_pattern_protects_the_directory_however_it_is_spelled`,
`test_a_protected_path_pattern_that_can_never_match_is_refused`.

**M2. A file-connector pattern could leave the local root and name what it found.**
`connectors/files.py`.
Only the part before the first wildcard was confined. A `..` after a wildcard was expanded
first, and the refusal quoted the file it had matched, in an error a tenant can read: file
names outside the root (other tenants' object keys, secret file names) could be read one
pattern at a time. File contents were never read.
*Fix:* `..` is refused; a refusal never names a match.
*Test:* `test_file_connector_glob_cannot_climb_out_or_name_what_it_finds`.

**M3. Request bodies had no size limit and are read before the key is checked.**
`api/app.py`.
Anyone who could reach the port could have the process buffer a body of any size.
*Fix:* 1 MiB per request, 32 MiB for pushed signals, enforced before the body is read.
*Test:* `test_a_request_body_over_the_limit_is_refused_before_it_is_read`.

**M4. `/metrics` grew without bound.** `api/app.py`.
The request method was a metric label as sent, and the server accepts any token as a
method. Each new one added fourteen series for the life of the process, without a key.
*Fix:* methods outside the standard set are counted as `OTHER`.
*Test:* `test_unknown_http_methods_share_one_metrics_label`.

**M5. Redaction missed credentials in common spellings.** `redaction.py`.
JSON carried inside a JSON string (`{\"password\": \"...\"}`), `*_PASS` and `--pass`,
`Authorization` with a scheme other than Bearer or Basic, `Cookie` and `Set-Cookie`
headers, OpenAI project keys (`sk-proj-...`), PGP private key blocks, and fields named in
the plural (`cookies`, `tokens`) or `pass`.
*Fix:* each is detected. *Tests:* `test_credentials_in_common_spellings_are_masked`,
`test_credential_fields_are_masked_in_the_plural_and_the_short_form`,
`test_ordinary_words_that_contain_pass_are_left_alone`.

**M6. A connector request had no total deadline.** `connectors/base.py`.
`timeout` bounds each read, so a server that kept sending a byte now and then held a worker
for as long as it liked.
*Fix:* a request may take four read timeouts in all.
*Test:* `test_a_connector_request_has_a_total_deadline`.

### Low

- **L1. The validation report quoted on a corrective retry could close its own framing.**
  Only the exact lower-case closing tag was neutralized. Every `</` is now escaped.
  `test_the_validation_report_cannot_close_its_own_framing`.
- **L2. Review text was unbounded** (comment, investigation question, edited list items).
  `test_review_text_is_bounded`.
- **L3. A redaction pattern that matches the empty string** inflated every text it touched.
  `test_a_redaction_pattern_that_matches_nothing_at_all_is_refused`.
- **L4. Epoch timestamps were masked as credit cards** one time in ten, which inflated the
  redaction counts and changed log templates from line to line. A card number must now
  start like one. `test_epoch_milliseconds_are_not_card_numbers`.

## Open findings

None of these is fixed in 1.2.2. Each has a recommended fix and the reason it was not made
here.

### Medium

**O1. A signal named by the model is trusted.** `inference/orchestrator.py`.
Evidence references are checked against what the model was shown; the `signal` of a finding
is not, and falls back to the model's own string. Post-merge verification later watches that
signal. A hallucinated, or injected, finding with valid evidence and an invented signal is
marked `verified` after its fix is merged, because the invented signal never recurs, while
the real problem continues.
*Impact:* a false "verified". No code change results from it: the task still needed a human
approval and the pull request a human merge.
*Recommended fix:* store a signal only when it was in the input; otherwise derive it from
the cited evidence or leave it empty and record the model's value for the reviewer.
*Why not now:* it changes how findings are fingerprinted (and so deduplicated), and the
current tests assert that unknown signals are kept. It needs a design decision and a
migration path for existing findings.

**O2. No rate limiting.** `api/`.
The API does not limit requests per key or per address. Keys are 256 bits of randomness, so
guessing one is not practical, but a caller can still load the service.
*Recommended fix:* limit at the proxy (documented in the hardening checklist); a
per-key limit in the application would need shared state between API processes.

### Low

- **O3. Tenants share workers, credential references and network reach.** Documented as a
  data boundary, not a security boundary, in [Security](guide/security.md#what-is-not-protected)
  and ADR-013. *Recommended:* per-tenant credential namespaces and an egress allowlist
  before hosting parties that do not trust each other.
- **O4. Connector, webhook and repository addresses may name private hosts**, because that
  is where self-hosted Loki, Prometheus and GitHub Enterprise live; DNS rebinding is not
  defended against. *Recommended:* an operator-set allowlist of hosts.
- **O5. Template mining costs CPU in proportion to message length.** About ten thousand
  crafted 8,000-token messages cost a worker roughly an hour. *Recommended:* cap the tokens
  compared per message.
- **O6. Workflow actions were pinned by tag, not by commit**, and the live-model workflow
  pulled `ollama/ollama:latest`. **Fixed in 1.2.3:** actions are pinned to a commit and the
  image to a release.
- **O7. API keys do not expire and cannot be rotated in place.** Documented. *Recommended:*
  expiry and SSO.

## Checked and found sound

- **Approval gate.** Execution cannot start, and nothing is pushed, without a recorded
  approval by a reviewer or admin whose scope digest still matches. No bypass was found.
- **No merge capability** in any provider.
- **Sandbox.** Network-less, read-only, capability-free containers; the platform's git
  commands use a metadata snapshot taken before the agent starts; nested repositories are
  refused. The `local` backend is refused for real repositories.
- **Roles and tenancy.** Every route has a role dependency; cross-tenant ids are
  indistinguishable from missing ones; `service` keys reach only the push route.
- **Session handling.** HttpOnly, SameSite=Strict cookie; `Origin` checked on every form
  post; the API takes the cookie for reads only; no open redirect; templates autoescape.
- **Credential references.** `env:` cannot name platform secrets; `file:` is confined to
  the secrets directory. Tokens are never persisted or logged; git's error text is scrubbed.
- **SSRF.** Connector and webhook hosts are resolved and checked (link-local refused),
  redirects are not followed, responses are size-bounded and never echoed into errors.
- **Scanner isolation.** An audited repository cannot reconfigure ruff, gitleaks or trivy.
- **Keys.** 256-bit random, stored as SHA-256.
- **Containers.** The image runs as a non-root user; Compose binds loopback, publishes no
  database port and refuses to start without a database password.

## Tool results

| Tool | Scope | Result |
|---|---|---|
| `pip-audit` 2.x | Every locked dependency (`uv export --all-extras`, 48 packages) | No known vulnerabilities |
| `bandit` 1.9 | `src/` (12,800 lines) | No high or medium findings apart from one false positive (`/tmp` inside the sandbox container, B108). Low: `assert` used to narrow types (B101, 44 places; no security check relies on one) and `subprocess` without a shell (B603/B607: git, docker and the scanners, by design) |
| `gitleaks` 8.30.1 | The whole git history (48 commits) | 5 matches, all synthetic test fixtures in `tests/` (a fake JWT, a fake key block, placeholder API keys) |
| `ruff` with the `S` (bandit) rules | `src/`, `tests/`, `scripts/` | Clean; part of CI |
