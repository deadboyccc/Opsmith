# Decisions

A log of engineering decisions and why they were made: choices between workable options,
things deliberately left alone, and anything a later reader would otherwise have to guess.
Newest entries are at the end of each section.

Design decisions about how Opsmith itself is built are recorded as ADRs in
[`ARCHITECTURE_DECISIONS.md`](../ARCHITECTURE_DECISIONS.md). This page records the
decisions taken while working on the code base: how a defect was fixed when there was more
than one way, what tooling was chosen, what was left open. Add an entry whenever a session
makes a choice that is not obvious from the diff.

## How the 1.2.1 to 1.3.x work was run (November 2026)

- **Each phase is a milestone with its own release.** Bug fixes (1.2.1), the security audit
  (1.2.2), tests (1.2.3), documentation (1.2.4), the documentation site (1.3.0) and the
  contributor guides (1.3.1). The owner authorized direct pushes to `main` for this work
  only; the normal route for a change is a pull request (see `CONTRIBUTING.md`).
- **Every defect was confirmed before it was fixed.** The code base was read in full, in
  five parts. A suspected defect counted only once a test reproduced it; the test was written
  first, seen to fail for the stated reason, and kept as the regression test. Suspicions that
  did not reproduce were dropped, not "fixed".
- **Baseline before any change** (release 1.2.0): 395 tests passed, 13 skipped; 92% line and
  branch coverage; ruff, mypy and the documentation check clean.

## Bug fixes (1.2.1)

### Claude calls pass an explicit timeout instead of switching to streaming

The Anthropic SDK refuses a non-streaming request that it expects to run for more than ten
minutes (above about 21,000 `max_tokens`) unless the caller sets the timeout. The coding
agent's default is 32,000, so with the real SDK every turn failed before anything was sent.

- **Chosen:** pass `timeout=` on every `messages.create`, computed with the SDK's own
  estimate (an hour per 128,000 output tokens, never less than its ten-minute default).
- **Not chosen:** `messages.stream(...).get_final_message()`, which the SDK recommends for
  long requests. It is the better transport for very long generations, but it changes the
  call that every fake client in the tests imitates, and `CLAUDE.md` asks for the
  `messages.create` pattern to be kept. Revisit if stages start to use output allowances
  where a single HTTP response can sit idle for many minutes.
- **Also:** a test now runs the agent against the real SDK over a mock transport. The
  hand-written fake accepted any arguments, which is how this went unnoticed.

### Provider exceptions become `LLMError` in the gateway, not in each provider

An SDK's own exception types (rate limit, overload, connection) passed the stages, which
handle `LLMError`: the job was rolled back together with the usage records of calls already
billed. The gateway now wraps anything a provider raises. Doing it once in the gateway keeps
the rule out of every provider, including third-party ones registered through an entry
point.

### One audit per repository: a file lock, not a row lock

Audits of one repository share one checkout directory. The other "one at a time" rules use a
database row lock, but an audit deliberately does its slow work before it opens a write
transaction, and the shared thing is a directory on one host, not a row. An advisory `flock`
next to the checkout fits that: it holds between worker processes that share the data
directory, the kernel releases it if a worker dies, and a second audit is deferred with
`RetryLater` rather than queued behind a lock.

### Serialization locks are `FOR NO KEY UPDATE`

`FOR UPDATE` on a project or service row also blocks the `FOR KEY SHARE` that PostgreSQL
takes for every foreign-key check, so nothing that references the row could be inserted
while a model call ran. `FOR NO KEY UPDATE` excludes a second holder just the same and lets
those inserts through. Confirmed against a real PostgreSQL 18 server; the test is skipped on
SQLite, which has no row locks.

### The API commits before it answers

The request's transaction was committed in a dependency's exit code, which FastAPI runs after
the response is sent. `Depends(get_session, scope="function")` runs it when the handler
returns. That parameter exists from FastAPI 0.121, so the minimum version was raised (the
lock file was already far past it).

### `/docs` and `/redoc` get their own Content-Security-Policy

The strict policy blocked the script, style sheet and inline bootstrap that Swagger UI and
ReDoc need, so the documented `/docs` page was blank.

- **Chosen:** a second policy for exactly those two paths that allows their CDN and inline
  script. They render the OpenAPI schema and nothing a user or a model wrote.
- **Not chosen:** turning the pages off (removes a documented feature), or bundling the
  assets (a new vendored dependency to keep patched).

### Records that cannot be stored are dropped and counted, text is cleaned

A NUL or half a surrogate pair in a log message is replaced with U+FFFD when the record is
stored. A service or environment *name* that holds one is different: changing it could merge
two services, so the record is dropped and counted in `skipped_records`, as an over-long name
already was. Batches written by earlier releases are cleaned when they are read, so an old
poisoned batch stops blocking its service too.

### `ingest_interval_seconds` is applied when a connector is created

The setting was documented as the default connector interval and never used, because every
connector row has an interval of its own. Making the column nullable would need a schema
migration, which a patch release should not carry. Instead a connector created without an
interval takes the project's value at that moment (`opsmith apply` re-applies it every time).

### A bounded retry for OpenAI-compatible endpoints

Two more attempts on 429/500/502/503/504 and on a refused connection, 1 s then 2 s apart or
as `Retry-After` says. A read timeout is not retried, because the request may still be
running on the server. A reply without token usage is estimated at four characters per token
rather than recorded as free, in line with the existing rule that an unpriced model is never
costed at zero.

### Left as they are, on purpose

- **A signal named by the model is stored even when it was not in the input.** Evidence
  references are checked against the input; `signal` is not, and post-merge verification
  later watches that signal. Dropping unknown signals changes how findings are fingerprinted
  and what existing tests assert, so it needs a design decision, not a patch. Recorded in the
  security audit that follows this release.
- **Leads age on runs without new data.** Already listed under known limitations in
  `ARCHITECTURE_DECISIONS.md`.
- **A bundle can still lose its log templates to a large number of anomalous metric series.**
  The trim order is documented behaviour; a floor for the top templates would be a change to
  what the model sees and belongs with an evaluation of analysis quality.
- **`opsmith apply` creates a repository as `local` when `scm_provider` is left out; the API
  defaults to `github`.** Changing either default would change what existing files and
  clients do.
