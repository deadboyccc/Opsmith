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
  [security audit](SECURITY-AUDIT.md).
- **Leads age on runs without new data.** Already listed under known limitations in
  `ARCHITECTURE_DECISIONS.md`.
- **A bundle can still lose its log templates to a large number of anomalous metric series.**
  The trim order is documented behaviour; a floor for the top templates would be a change to
  what the model sees and belongs with an evaluation of analysis quality.
- **`opsmith apply` creates a repository as `local` when `scm_provider` is left out; the API
  defaults to `github`.** Changing either default would change what existing files and
  clients do.

## Security audit (1.2.2)

### Repository addresses are checked in three places

When saved through the API, when applied from a file, and by the providers every time they
are about to call git. The last one matters most: it also covers rows that were written
before the check existed, or by something other than the API. The rule is deliberately
narrow (`https://` without credentials; a branch that starts with a letter or digit) rather
than a list of known-bad values, because an allowlist does not have to anticipate the next
git option.

- **Not chosen:** restricting the host to `github.com`. GitHub Enterprise runs on other
  hosts, and the token is the tenant's own.

### Protected-path patterns got more permissive in what they match, not stricter in what they accept

`infra/`, `/infra/**` and `./infra/**` used to match nothing. Rejecting those spellings would
break settings that are already stored and leave the directory unprotected until someone
noticed. Instead a pattern now also covers everything below a directory it names, which can
only add protection. Only patterns that cannot match at all (empty, `..`) are refused.

### The body limit is a constant, not a setting

1 MiB per request and 32 MiB for a push are far above what any route needs and far below
what would hurt. A setting would be one more thing to document and get wrong; a deployment
that needs a different limit sets it at its proxy, which is where rate limiting belongs too.

### The body limit answers 413 itself

FastAPI turns any error raised while it reads the body into its own `400`. So the
middleware cannot raise: when the count goes over the limit it sends the `413`, tells the
application the client has disconnected, and drops whatever the application sends after.

### Redaction: only spellings with a clear shape were added

Each new pattern has a negative test next to it (`bypass=`, `compass:` are left alone). The
looser a credential pattern, the more ordinary log text it destroys, and redacted text also
changes how log lines cluster into templates. Secrets without a recognizable shape remain
the job of `redaction_extra_patterns`.

### A binary file that contains a credential-like string is refused

Fixing the line splitting made the scan see whole lines of binary files, where it used to
see fragments. A real-container test showed the consequence: a `.pyc` written by a check
command contains the string literals of its source, including a key that was already in the
repository. That is a correct refusal (a new file with a credential in it), so the test's
check was changed to compile in memory, and the behaviour is called out in the upgrade notes.

### What the audit left open

Listed with reasons in [`SECURITY-AUDIT.md`](SECURITY-AUDIT.md#open-findings): the model's
own `signal` being trusted, no rate limiting in the application, shared tenancy, private
hosts as addresses, and the CPU cost of template mining. (Actions pinned by tag was fixed in
1.2.3.)

## Tests and CI (1.2.3)

### A coverage floor of 93%, not a target

CI fails when coverage falls below 93%; it is 94.5%. The floor is set a little under the
current figure so that an honest refactor that removes tested code does not break the build,
while a change that adds untested code does. Raise it as coverage rises. A target such as
100% would push tests toward lines rather than behaviour; the code that is still not covered
is mostly defensive branches (a plugin that fails to load, a kernel refusing to kill a
process group).

### Coverage measured by path

`source = ["src/opsmith"]` instead of the package name. Alembic loads `migrations/env.py` as
a file, not as a module of the package, so measuring by package reported it as never run
although the migration tests run it on every start.

### The dependency audit is its own CI job and needs the network

`make check` stays runnable offline (it is what a contributor runs before every push);
`make audit` is separate because it queries advisory databases. `pip-audit` is pinned to a
version and reads the lock file with hashes, so it checks exactly what is installed.

### Actions pinned by commit

A tag in another repository can be moved to different code; a commit cannot. The tag stays
in a comment so the pin can be read and updated. This was open item O6 of the
[security audit](SECURITY-AUDIT.md).

### No test was removed as flaky

Every full run in this work (eight on SQLite, three on PostgreSQL) passed. Two tests depend
on real time. The heartbeat test's margin was widened (it allowed less than a second of
delay before the lease lapsed); the process-group test can only err toward passing, so it was
left as it is. Neither the suite nor CI retries failing tests.

## Documentation site (1.3.0)

### MkDocs with Material for MkDocs

The site is built with [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/)
9.8 and published to GitHub Pages from `main`.

- **Why it fits:** the documentation is already plain Markdown written for GitHub, and the
  project is Python managed by uv, so the site needs no Node toolchain and lives in a
  dependency group (`docs`) that the application never installs. Material gives the
  requirements without custom code: a responsive layout with a navigation drawer on phones,
  built-in search, light and dark schemes that follow the system, a table of contents per
  page, copy buttons on code blocks, Mermaid diagrams, and a strict build that fails on a
  broken link or heading anchor.
- **Not chosen:** Docusaurus and VitePress. Both are good, both need Node and a JavaScript
  dependency tree to keep patched for a site that has no interactive pages, and both read
  Markdown with their own extensions (MDX in Docusaurus) where these pages are plain
  GitHub Markdown.
- **Known horizon:** Material for MkDocs reaches end of life on 5 May 2027 (critical and
  security fixes continue until then). Its maintained successor, Zensical, is by the same
  team and reads `mkdocs.yml`, but it is at version 0.1 (0.1.1, November 2026). The
  site uses no plugin beyond search, so moving to Zensical once it has matured should be a
  configuration change; revisit before May 2027.

### The pages are published from where they are

Every Markdown file in the repository (README, the root guides, `docs/`, `reports/`) is
published without being moved or edited. `scripts/build_site.py` copies them into
`build/site-src` with their relative layout, so the links between them keep working on
GitHub and on the site alike, and adjusts only the copies: the README becomes `overview.md`
(the landing page is `index.md`), links to files that are not pages (`LICENSE`, scripts,
example configurations) point at GitHub, and raw-HTML links get the extra `../` that
MkDocs' directory URLs need. Headings use GitHub's anchor rules, so links written for GitHub
resolve on the site. The changelog page is `CHANGELOG.md` itself, rebuilt on every push to
`main`, so it cannot fall out of step.

- **Not chosen:** moving the pages into a `docs_dir`. It would break every link written for
  reading on GitHub, where most people meet the project.

### Images: which one goes where

The README references two brand images, both in `docs/images/brand/`:

| Placement | Image | Why |
|---|---|---|
| Header logo | `mascot.svg` | A vector at any size, 6 KB, transparent, and legible at 28 px |
| Favicon | `mascot.svg` | The same file; modern browsers take SVG favicons |
| Landing page (hero) | `mascot.svg` above the headline, `social-preview.png` beside it | The mascot introduces the page; the preview carries the tagline as a picture |

Neither file is altered. The 503 KB preview is served as WebP copies generated at build time
(17 KB at 640 px wide, 42 KB at 1280 px), chosen by the browser through `srcset`, with the
original PNG as the fallback. The mascot was checked on white and on the dark scheme's navy:
its outline and colours hold on both, so it needs no per-scheme variant. The preview has its
own dark background and a border on the light scheme.

### How the site was verified

A headless browser loaded all 28 pages served under `/Opsmith/` (the path GitHub Pages uses
for this project) at 390 px and 1366 px wide, in light and dark mode. On every page: no
sideways scrolling, every image loaded (lazy ones after scrolling to them), and the header
logo and favicon present. Two defects were found and fixed this way: the README's raw-HTML
images were broken under directory URLs, and both landing-page buttons were navy on navy in
dark mode.

### Pages is enabled by a person, once

Turning GitHub Pages on for a repository needs the `pages: write` or `administration: write`
permission, which the token used for this work did not have; the workflow's own token cannot
do it either. The deploy job therefore checks first: while Pages is off it builds and checks
the site, skips the deployment and leaves a warning on the run, instead of turning `main`
red. Once a repository admin selects *Settings, Pages, Source: GitHub Actions*, the next push
to `main` (or running the *Docs* workflow by hand) publishes the site.

## Contributor rules (1.3.1)

### The session rules live in `AGENTS.md`; `CLAUDE.md` imports them

`AGENTS.md` is read by people and by any coding agent; `CLAUDE.md` is read by Claude Code and
imports `AGENTS.md`. Writing the rules once and importing them keeps the two from drifting.
`CLAUDE.md` repeats only a short summary (so the rules are visible even where the import is
not followed) and adds what is specific to Claude Code and the Claude API.

### What the rules leave out on purpose

The work that produced these rules pushed each release straight to `main`, which the
repository owner allowed for that work only. The rules say the opposite (branch, pull
request, green CI), because that is the standing practice.

## Videos

### A vertical reel, made from the product itself

The reel asked for was modelled on short social videos promoting open-source tools: vertical,
about thirty seconds, big captions, a call to action at the end. It is original, in that
genre, and uses only Opsmith's own material: the mascot as it is, and screenshots of the
offline demo, whose data is all synthetic.

- **Rendered frame by frame from HTML,** not screen-recorded: the storyboard is a page with a
  timeline function, and each of the 930 frames is a screenshot at an exact time. The video
  is the same on every run and every machine, and a change is an edit to the page.
- **Music, no voice.** The captions carry the message, so the video works muted, which is
  how phones start social video. The music is the subject of the next entry.
- **Size:** 2.2 MB at 1080 x 1920, H.264 with `faststart` so it streams. The site loads it
  only when someone presses play (`preload="none"`), showing an 87 KB poster until then.

### Original music, synthesized, on both videos

Both videos have an instrumental track and no voice-over (a narrated version was tried and
dropped). The music is composed and synthesized by `scripts/soundtrack.py`, not taken from a
library.

- **Why synthesize it:** a library track needs a licence that allows redistribution in a
  public repository and on a website, and the terms have to be kept with it. Music generated
  here from oscillators and noise has no such terms, and the same command always produces
  the same audio, so a re-recorded video gets its music back with one command.
- **What it is:** Am, F, C, G (a common, warm, slightly hopeful progression), with a chord
  pad, a plucked arpeggio with a bouncing echo, a bassline, soft kick, hi-hat and, in the
  reel, claps, plus reverb. Each piece is fitted to its video, and the last chord is always
  A minor, the home key, held while it fades, so it ends instead of stopping.
- **Two moods:** the reel is upbeat (112 BPM), with drums from the second bar, because it
  has to hold attention on its own. The demo video is calmer (92 BPM), sparser and 3 LU
  quieter, because it plays under captions someone is reading.
- **Loudness:** -16 LUFS for the reel, the usual target for social video, and -19 LUFS for
  the demo video. Both are measured to that target with headroom below the peak limit.
- **Checked without listening:** the mix was judged from its spectrum, per bar and per
  band. The first version put half its energy below 80 Hz, which phone speakers barely
  reproduce, so it would have sounded thin on a phone. The bass was moved up an octave and
  the kick given an overtone, putting the weight between 80 and 250 Hz.

## Serving under a path, reading VictoriaLogs (1.4.0)

### Under a path: mount the application, do not strip the prefix

To serve Opsmith at `https://<domain>/opsmith/` next to other sites, the whole application is
mounted under the base path. The proxy forwards requests unchanged, and every link, redirect
and asset is built from the path the request arrived through. Stripping the prefix in the
proxy instead would leave the pages' absolute links (`/ui/...`) pointing at the other sites
on the domain. The sign-in cookie is scoped to the path, so it is never sent to them.

### A VictoriaLogs connector instead of a forwarder

Stacks that keep their logs in VictoriaLogs could not be read: it does not offer Loki's
query API. A read-only connector reads it directly; the alternative, shipping a
second copy of every log line to Opsmith's push endpoint, would duplicate storage and add a
moving part. The connector takes the level a message states over the syslog severity,
because Docker's syslog driver records everything written to stderr as an error.
