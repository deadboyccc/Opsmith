# CLAUDE.md

@AGENTS.md

## Claude Code notes

`AGENTS.md` (imported above) is canonical. This file only adds what is specific to Claude
Code; where the two seem to differ, `AGENTS.md` wins.

- **Follow *How every session works* in `AGENTS.md` in every session**, from the first step
  to the last. In short:
  - run `make check` before you change anything, and again after;
  - every change has tests, and every bug fix starts with a test that fails;
  - stability, security and simple, correct code come before speed or cleverness;
  - never merge, or ask for a merge, with a regression.
- **Every session ends with the docs**, without exception:
  - update the Markdown pages your change affects;
  - update the documentation site: run `make site`, and add any new page to `nav` in
    `mkdocs.yml`;
  - update `CHANGELOG.md` under *Unreleased* (the site's changelog page is that file);
  - record significant decisions, with the reasoning, in `docs/DECISIONS.md`.
- The **Invariants** section of `AGENTS.md` is non-negotiable: never add an approval bypass,
  a merge path, an LLM call that skips `LLMGateway`, or a way for repository code to run
  outside the sandbox. If a task seems to need one, stop and ask.
- **Verify before claiming done.**
  - Run `make check`, or `make docker-test` when the host has no Python 3.12 or uv.
  - Report failures with their output instead of summarizing them away. Say what you did not
    run.
- **Iterate on the area, then run everything.**
  - While iterating, prefer targeted runs (`uv run pytest tests/test_<area>.py -q`).
  - At the end, run the full suite once.
  - Also run it on PostgreSQL when you touched models or queries (see `AGENTS.md`,
    *Commands*).
- **Quick end-to-end check.** The fastest one is `uv run opsmith demo --dir /tmp/opsmith-demo`.
  It should finish with `"final_status": "verified"`.
- **Docker tests.** If Docker is available, also run `uv run pytest -m docker`. Those tests
  exercise the real sandbox and are skipped silently otherwise, so a green run without
  Docker proves less.
- **Editing Claude API code** (`llm/anthropic_provider.py`, `executor/agents.py`): keep the
  existing patterns.
  - Call `messages.create`, with `output_config.format` set by
    `anthropic_provider.output_format(schema)` for structured stages. Do not use
    `messages.parse`: it raises inside the SDK before the stop reason and usage can be read.
  - Pass an explicit `timeout=anthropic_provider.request_timeout(max_tokens)`. Without it
    the SDK refuses a non-streaming request above about 21,000 `max_tokens` before sending
    it.
  - Use a manual tool loop with strict tool schemas, and set `output_config.effort`.
  - Handle the `refusal` and `max_tokens` stop reasons. A tool call cut off by `max_tokens`
    still gets a `tool_result`, as an error, and is never run.
  - Count input with `anthropic_provider.input_tokens_of(usage)`, so cached input is
    counted too.
  - Append the full `response.content` back to the conversation.
  - Test with the real SDK over a mocked transport, not only with a fake client.
- Do not commit local artifacts (`data/`, `opsmith-demo/`, `build/`, `*.db`, `.scratch/`).
- Never put real credentials in files. Configuration takes references (`env:NAME`).
