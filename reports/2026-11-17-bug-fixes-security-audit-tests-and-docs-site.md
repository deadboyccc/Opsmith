# Bug fixes, security audit, tests and a documentation site

November 17, 2026. Releases 1.2.1 to 1.3.1.

## What was asked

Read the whole code base and fix real defects, test first. Audit it for security, and fix
what can be fixed safely. Improve the tests and CI. Bring every Markdown page in line with
the code. Publish all of them as a documentation site on GitHub Pages. Make `AGENTS.md` and
`CLAUDE.md` hold every future session to the same standard. Release each of these as its own
version.

## What was done

| Release | Content |
|---|---|
| [1.2.1](../CHANGELOG.md#121---2026-11-17) | About 45 defects fixed, each with a test that failed first |
| [1.2.2](../CHANGELOG.md#122---2026-11-17) | Security audit: 2 high, 6 medium and 4 low findings fixed ([audit](../docs/SECURITY-AUDIT.md)) |
| [1.2.3](../CHANGELOG.md#123---2026-11-17) | Tests and CI: coverage floor, dependency audit, actions pinned by commit |
| [1.2.4](../CHANGELOG.md#124---2026-11-17) | Every guide checked against the code |
| [1.3.0](../CHANGELOG.md#130---2026-11-17) | The documentation site |
| [1.3.1](../CHANGELOG.md#131---2026-11-17) | Session rules in `AGENTS.md` and `CLAUDE.md` |

The defects that mattered most:

- **With the real Claude SDK the coding agent could not run at all.** Every turn failed
  before a request was sent. The tests used a hand-written client, which is why nobody saw
  it.
- **A tenant admin could make the worker run a command**, through a repository's default
  branch (`--upload-pack=...`), or read another tenant's checkout through its address.
- **A credential placed after a form feed passed the push-time credential gate.**
- **One odd log record could stop a connector, or a service's analysis, for good**: half an
  emoji, a NUL, a number with thousands of digits, two metric points of 1e308.
- **On PostgreSQL, a running aggregation blocked all ingestion** for that project.
- **A write the API had answered `201` for could be lost**, because the commit ran after the
  response had been sent.

## How it was verified

- Every finding was reproduced before it was fixed; the reproducing test is the regression
  test. Suspicions that did not reproduce were dropped.
- Each release passed `make check` (lint, format, types, the suite, the docs check) and the
  whole suite on PostgreSQL 18 before it was pushed. CI ran green on each.
- The documentation site was loaded in a headless browser: all 28 pages, under the
  `/Opsmith/` path, at 390 and 1366 px wide, in light and dark mode. No page scrolls
  sideways, every image loads, and the logo and favicon are present.

| | Before (1.2.0) | After |
|---|---|---|
| Tests | 395 passed, 13 skipped | 582 passed, 13 skipped |
| Coverage (lines and branches) | 92% | 94.5%, with CI failing below 93% |
| Known vulnerable dependencies | none found | none; CI now checks every push |

## What is still open

- **GitHub Pages must be switched on by a repository admin**, once: *Settings, Pages,
  Source: GitHub Actions*. The token used for this work could not do it. Until then the site
  is built and checked on every push, and not published.
- The audit's open findings: a finding's `signal` is taken from the model unchecked, the API
  has no rate limiting of its own, and tenants share workers and credential references. Each
  is described, with the recommended fix, in the
  [security audit](../docs/SECURITY-AUDIT.md#open-findings).
- Material for MkDocs reaches end of life in May 2027; its successor, Zensical, should be
  evaluated before then ([decision](../docs/DECISIONS.md#mkdocs-with-material-for-mkdocs)).
