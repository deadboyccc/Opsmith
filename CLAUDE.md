# CLAUDE.md

@AGENTS.md

## Claude Code notes

`AGENTS.md`, imported above, is canonical: never break its invariants, and follow *How to
work* from the first step to the last. This file adds only what is specific to Claude Code.
Where the two seem to differ, `AGENTS.md` wins.

### The steps most often skipped

- `make check` before the change and after it, and a test that fails before every bug fix
  (rules 2, 7 and 11).
- The docs, the changelog entry in the same commit, and the decision record (rules 14 to 16).
- A look at what leaves before every push, a release for every change that reaches `main`,
  and a check that CI and the live site caught up (rules 17 to 19).
- The closing report: what changed, how it was verified, what was not run.

### Working in Claude Code

- **Track multi-step work in a task list,** and mark an item done only once it is verified,
  not when the code is written.
- **Subagents and reviewers can be wrong.** Give them read-only tasks with a precise scope,
  and check each finding against the code before you apply it.
- **Long runs.** The full suite takes about six minutes; run it in the background if you have
  other work. Never run two full suites, or a suite and an image build, at once on a small
  host: check free memory first. If `/tmp` is RAM-backed, run
  `PYTEST_ADDOPTS="--basetemp=/var/tmp/opsmith-pytest" make check` and remove that directory
  afterwards.
- **Tokens.** A token the user gives you goes into a file outside the repository, created with
  `umask 077`, and is deleted when the task is done. Never echo it, and remind the user to
  revoke a token that was pasted into the chat.
- **Edits.** After `make fmt` or `ruff format`, re-read a file before matching its text: the
  formatter rewraps lines.
- **Quick end-to-end check.** `uv run opsmith demo --dir /tmp/opsmith-demo` should finish with
  `"final_status": "verified"`.
- **Docker tests.** If Docker is available, also run `uv run pytest -m docker`. They exercise
  the real sandbox and the gitleaks and trivy scanners, and skip otherwise
  (`uv run pytest -q -rs` lists the skips), so a green run without Docker proves less. With no
  Python 3.12 or uv on the host, `make docker-test` runs the checks in the dev image.

### Editing Claude API code

`llm/anthropic_provider.py` and `executor/agents.py` follow these patterns (the rules are also
under *Conventions* in `AGENTS.md`; here are the reasons):

- Call `messages.create`, with `output_config.format` set by
  `anthropic_provider.output_format(schema)` for structured stages. Do not use
  `messages.parse`: it raises inside the SDK before the stop reason and usage can be read.
- Pass an explicit `timeout=anthropic_provider.request_timeout(max_tokens)`. Without it the
  SDK refuses a non-streaming request above about 21,000 `max_tokens` before sending it.
- Use a manual tool loop with strict tool schemas, and set `output_config.effort`.
- Handle the `refusal` and `max_tokens` stop reasons. A tool call cut off by `max_tokens`
  still gets a `tool_result`, as an error, and is never run.
- Count input with `anthropic_provider.input_tokens_of(usage)`, so cached input is counted
  too.
- Append the full `response.content` back to the conversation.
- Test with the real SDK over a mocked transport, not only with a fake client: a fake accepts
  any arguments and once hid a bug that stopped the coding agent from running at all.

### Never

- Add an approval bypass, a merge path, a model call outside `LLMGateway` (the coding agents'
  own loop, inside the confined workspace, is the one exception), or a way for repository
  code to run outside the sandbox. If a task seems to need one, stop and ask.
- Commit local artifacts (`data/`, `opsmith-demo/`, `build/`, `*.db`, `.scratch/`).
- Put real credentials in files. Configuration takes references (`env:NAME`).
