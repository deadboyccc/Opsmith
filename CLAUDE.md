# CLAUDE.md

@AGENTS.md

## Claude Code notes

- Read `AGENTS.md` (imported above) first. Its **Invariants** section is non-negotiable:
  never add an approval bypass, a merge path, an LLM call that skips `LLMGateway`, or a way
  for repository code to run outside the sandbox.
- Verify before claiming done: run `make check`, or `make docker-test` when the host has no
  Python 3.12 / uv. Report failures with their output instead of summarizing them away.
- Prefer targeted runs while iterating (`uv run pytest tests/test_<area>.py -q`), then run the
  full suite once at the end.
- The fastest end-to-end sanity check is `uv run opsmith demo --dir /tmp/opsmith-demo`; it should
  finish with `"final_status": "verified"`.
- If Docker is available, also run `uv run pytest -m docker`: those tests exercise the real
  sandbox and are skipped silently otherwise, so a green run without Docker proves less.
- When editing Claude API code (`llm/anthropic_provider.py`, `executor/agents.py`), keep the
  existing patterns: `messages.parse(output_format=...)` for structured stages, a manual
  tool loop with strict tool schemas, `output_config.effort`, refusal and `max_tokens` stop
  reasons handled, and full `response.content` appended back to the conversation.
- Do not commit local artifacts (`data/`, `opsmith-demo/`, `*.db`, `.scratch/`).
- Never put real credentials in files. Configuration takes references (`env:NAME`).
