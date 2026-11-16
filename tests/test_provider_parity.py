"""The acceptance scenario must not depend on which model provider drives it.

``test_e2e.py`` runs the scenario on the offline rule-based provider. Here the same scenario
runs with every model stage, and the coding agent, going through the OpenAI-compatible HTTP
path instead: prompts are rendered and redacted, sent as Chat Completions requests, answered by
a fake endpoint, parsed, validated and accounted for. The fake endpoint answers with the same
rules the offline provider uses, so the two runs must reach the same result. Anything the HTTP
path loses or mangles on the way shows up as a difference.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from opsmith.audits import external
from opsmith.config import Settings
from opsmith.demo import DemoResult, run_demo
from opsmith.executor.agents import _CODEMODS
from opsmith.gitutil import run_git
from opsmith.llm import ChatClient
from opsmith.llm.base import ProviderRequest
from opsmith.llm.heuristic import HeuristicProvider
from opsmith.models import ExecutionRun, Finding, InferenceRun, Lead, Task, UsageRecord
from opsmith.platform import Platform
from opsmith.schemas import InferenceOutput, TaskDraft, TriageOutput

pytestmark = pytest.mark.e2e

STAGES = {
    "InferenceOutput": ("inference.analyze", InferenceOutput),
    "TriageOutput": ("audit.triage", TriageOutput),
    "TaskDraft": ("task.draft", TaskDraft),
}
OPENAI_STAGE = {
    "provider": "openai",
    "model": "fake-open-model",
    "max_tokens": 4000,
    "input_price_per_mtok": 1.0,
    "output_price_per_mtok": 2.0,
}
OPENAI_SETTINGS = {"executor_agent": "openai", "llm": {"default": OPENAI_STAGE}}


def _untrusted_payload(message: str) -> Any:
    """The JSON a prompt carries between its <untrusted_data> tags."""
    body = message.split("<untrusted_data>\n", 1)[1].split("\n</untrusted_data>", 1)[0]
    return json.loads(body)


class FakeEndpoint:
    """An OpenAI-compatible server for one scenario run."""

    def __init__(self) -> None:
        self.rules = HeuristicProvider()
        self.structured: list[str] = []
        self.agent_turns = 0
        self.bodies: list[dict[str, Any]] = []

    def post(self, payload: dict[str, Any]) -> httpx.Response:
        """Stands in for ``ChatClient._post``. Installed on the class as a bound method of
        this endpoint, so every client in the process sends its requests here."""
        self.bodies.append(payload)
        reply = self._agent(payload) if "tools" in payload else self._structured(payload)
        return httpx.Response(
            200,
            json={
                "model": payload["model"],
                "choices": [reply],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 200},
            },
        )

    def _structured(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = payload["response_format"]["json_schema"]["name"]
        prompt_id, schema = STAGES[name]
        self.structured.append(prompt_id)
        system, user = (m["content"] for m in payload["messages"])
        request = ProviderRequest(
            stage=prompt_id,
            prompt_id=prompt_id,
            model="heuristic-v1",
            system=system,
            user=user,
            schema=schema,
            variables=_untrusted_payload(user),
        )
        content = json.dumps(self.rules.complete(request).data)
        return {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}

    def _agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A scripted coding agent: read the flagged file, fix it, run the tests, finish."""
        self.agent_turns += 1
        messages = payload["messages"]
        # The task merges a runtime finding (no rule) with the static one that names the fix.
        findings = _untrusted_payload(messages[1]["content"])["findings"]
        finding = next(f for f in findings if f.get("rule_id") in _CODEMODS)
        results = [m["content"] for m in messages if m["role"] == "tool"]
        if not results:
            call = ("read_file", {"path": finding["path"]})
        elif len(results) == 1:
            fixed = _CODEMODS[finding["rule_id"]](results[0], int(finding["line"]))
            assert fixed, "the scripted agent could not apply the fix"
            call = ("write_file", {"path": finding["path"], "content": fixed})
        elif len(results) == 2:
            call = ("run_check", {"name": "test"})
        else:
            assert results[2].startswith("exit_code=0"), results[2]
            call = ("finish", {"success": True, "summary": "Added a timeout to the HTTP call."})
        tool_call = {
            "id": f"call_{len(results)}",
            "type": "function",
            "function": {"name": call[0], "arguments": json.dumps(call[1])},
        }
        message = {"role": "assistant", "content": None, "tool_calls": [tool_call]}
        return {"message": message, "finish_reason": "tool_calls"}


def _run(base: Path, monkeypatch, project_settings: dict[str, Any] | None = None) -> DemoResult:
    # Built-in analyzers only, as in test_e2e: the scenario must not vary with host scanners.
    monkeypatch.setattr(external.ExternalTool, "available", lambda _self: False)
    return run_demo(base, verbose=False, project_settings=project_settings)


def _platform(result: DemoResult, base: Path) -> Platform:
    return Platform(
        Settings(data_dir=result.data_dir, log_json=False, sandbox_backend="local", local_root=base)
    )


def _outcome(platform: Platform, result: DemoResult) -> dict[str, Any]:
    """What the scenario produced, in terms that do not depend on ids or timing."""
    with platform.db.session() as s:
        task = s.get(Task, result.approved_task_id)
        run = s.scalars(select(ExecutionRun)).one()
        return {
            "final_status": result.final_status,
            "tasks": s.scalar(select(func.count()).select_from(Task)),
            "task_title": task.title,
            "task_category": task.category,
            "task_paths": task.paths,
            "evidence_kinds": sorted({e["kind"] for e in task.evidence}),
            "findings": sorted(
                (f.source, f.rule_id or "", f.path or "", f.status)
                for f in s.scalars(select(Finding))
            ),
            "leads": sorted((lead.state, lead.signal or "") for lead in s.scalars(select(Lead))),
            "inference_runs": [
                (r.status, len(r.input_bundle_ids), len(r.input_leads))
                for r in s.scalars(select(InferenceRun).order_by(InferenceRun.started_at))
            ],
            "changed_files": run.changed_files,
            "checks": [(c["name"], c["exit_code"]) for c in run.checks],
        }


def test_acceptance_scenario_through_an_openai_compatible_endpoint(tmp_path, monkeypatch) -> None:
    endpoint = FakeEndpoint()
    monkeypatch.setattr(ChatClient, "_post", endpoint.post)
    result = _run(tmp_path / "openai", monkeypatch, OPENAI_SETTINGS)
    assert result.final_status == "verified", result.steps
    platform = _platform(result, tmp_path / "openai")

    # Every model stage really went over HTTP, and so did the coding agent.
    assert {"inference.analyze", "audit.triage", "task.draft"} <= set(endpoint.structured)
    assert endpoint.agent_turns == 4

    with platform.db.session() as s:
        usage = s.scalars(select(UsageRecord)).all()
        assert {u.provider for u in usage} == {"openai"}
        assert {u.model for u in usage} == {"fake-open-model"}
        assert {u.stage for u in usage} == {"inference", "audit_triage", "task", "executor"}
        per_call = (1000 * 1.0 + 200 * 2.0) / 1_000_000  # the prices configured on the stage
        for record in usage:
            calls = 4 if record.stage == "executor" else 1
            assert record.cost_usd == pytest.approx(calls * per_call)
        runs = s.scalars(select(InferenceRun)).all()
        assert {(r.provider, r.model, r.prompt_version) for r in runs} == {
            ("openai", "fake-open-model", "2")
        }
        execution = s.scalars(select(ExecutionRun)).one()
        assert (execution.agent, execution.status) == ("openai", "succeeded")
        assert execution.cost_usd == pytest.approx(4 * per_call)
        steps = [step.get("tool") for step in execution.steps if step.get("step") == "tool"]
        assert steps == ["read_file", "write_file", "run_check", "finish"]

    # The fix that reached the repository is the real one.
    merged = run_git(["show", "main:app/payments.py"], result.repo_path)
    assert "timeout=10" in merged
    # Secrets in the demo's logs were redacted before any request left the process.
    sent = json.dumps(endpoint.bodies)
    assert "[REDACTED:email]" in sent
    assert "@example.com" not in sent  # the demo's access logs carry user email addresses


def test_the_scenario_ends_the_same_whichever_provider_runs_it(tmp_path, monkeypatch) -> None:
    offline = _run(tmp_path / "offline", monkeypatch)
    expected = _outcome(_platform(offline, tmp_path / "offline"), offline)

    endpoint = FakeEndpoint()
    monkeypatch.setattr(ChatClient, "_post", endpoint.post)
    remote = _run(tmp_path / "remote", monkeypatch, OPENAI_SETTINGS)
    actual = _outcome(_platform(remote, tmp_path / "remote"), remote)

    assert expected["final_status"] == "verified"
    assert actual == expected
