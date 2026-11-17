# Contributing to Opsmith

Thanks for helping. This page gets you from a fresh clone to a change that passes CI.
[`AGENTS.md`](AGENTS.md) is the full contributor guide (for people and for AI coding agents):
the repository map, the invariants that must never be broken, and the conventions the code
follows. Read its **Invariants** section before changing anything in the review gate, the
executor, the LLM gateway or the API.

## Set up

You need Python 3.12+, [uv](https://docs.astral.sh/uv/) and git. Docker is optional: without
it the tests that start real containers are skipped.

```bash
git clone https://github.com/deadboyccc/Opsmith.git && cd Opsmith
uv sync --all-extras
uv run opsmith demo --dir /tmp/opsmith-demo   # the whole loop, offline; ends with "verified"
```

No local Python? `make docker-test` runs the same checks inside a container.

[`docs/development/testing.md`](docs/development/testing.md) explains the test suite, including
how to run it on PostgreSQL, with real sandbox containers, and against a live model. Internals
are in [`docs/development/architecture.md`](docs/development/architecture.md).

## Make a change

```bash
uv run pytest tests/test_<area>.py -q    # iterate on the area you are touching
make fmt                                 # auto-fix lint and formatting
make check                               # ruff, format, mypy, tests + coverage floor, docs links
make audit                               # known vulnerabilities in the locked dependencies
```

What reviewers look for:

- **A test for the behaviour**, including the failure path. For anything that guards safety
  (approval, sandbox, redaction, tenancy, roles) add a test that tries the bad path and shows it
  is blocked. `tests/test_hardening.py` has many examples.
- **Tests stay offline.** Use `httpx.MockTransport` and fake clients; never the network.
- **No silent drops.** A failure must end as a retry, a dead job with a notification, a task
  back in review with a report, or a finding marked suppressed with a reason.
- **Docs move with the code.** Pages for people *using* Opsmith are under `docs/guide/`; pages
  for people *changing* it are under `docs/development/`. Update the one your change affects,
  `ARCHITECTURE_DECISIONS.md` for a design change, and add a line to `CHANGELOG.md`.
  `make docs` checks every link.

## Commits and pull requests

- Imperative, scoped subject lines: `executor: block secrets on added lines only`.
- One topic per pull request. Say what changed, why, the risk, and how you verified it.
- Never commit generated data (`data/`, `opsmith-demo/`, `*.db`) or real credentials.
  Configuration takes references such as `env:GITHUB_TOKEN`, never values.

## Reporting problems

- Bugs and ideas: open an issue with what you did, what happened and what you expected.
- Security problems: do not open a public issue. See
  [the security page](docs/guide/security.md#reporting-a-vulnerability).

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).
