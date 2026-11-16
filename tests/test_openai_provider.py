"""The provider-neutral path: any OpenAI-compatible Chat Completions endpoint.

Everything here talks to a scripted HTTP transport, never the network. A live endpoint is
exercised by ``test_live_endpoint_*`` only when ``OPSMITH_TEST_OPENAI_BASE_URL`` is set.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from opsmith.config import Settings
from opsmith.executor.agents import AgentBudget, AgentTask, OpenAICodingAgent
from opsmith.executor.sandbox import LocalSandbox, Workspace
from opsmith.llm import (
    ChatClient,
    LLMError,
    LLMGateway,
    LLMOutputError,
    LLMRefusal,
    OpenAICompatibleProvider,
    ProviderNotAllowed,
    default_gateway,
)
from opsmith.llm.base import ProviderRequest
from opsmith.models import Project, UsageRecord
from opsmith.platform import default_agent_factories
from opsmith.schemas import ProjectSettings, StageModel


class Out(BaseModel):
    answer: str
    score: int = 0


def _completion(content: str | None = None, **over: Any) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    message.update(over.pop("message", {}))
    return {
        "model": over.pop("model", "served-model"),
        "choices": [{"message": message, "finish_reason": over.pop("finish_reason", "stop")}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        **over,
    }


class Server:
    """Scripted endpoint: each entry is a response body, or ``(status, body)``."""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.headers: list[httpx.Headers] = []
        self.urls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        self.headers.append(request.headers)
        self.urls.append(str(request.url))
        item = self.script.pop(0)
        status, body = item if isinstance(item, tuple) else (200, item)
        return httpx.Response(status, json=body)

    def client(self, base_url: str = "http://models.internal:8000/v1", key_ref: str | None = None):
        return ChatClient(base_url, key_ref, http=httpx.Client(transport=httpx.MockTransport(self)))


def _request(**over: Any) -> ProviderRequest:
    fields: dict[str, Any] = {
        "stage": "triage",
        "prompt_id": "audit.triage",
        "model": "my-model",
        "system": "You triage findings.",
        "user": "<untrusted_data>\n{}\n</untrusted_data>",
        "schema": Out,
        "max_tokens": 512,
    }
    return ProviderRequest(**{**fields, **over})


# --------------------------------------------------------------------------- structured stages


def test_structured_request_and_response(monkeypatch) -> None:
    monkeypatch.setenv("MY_MODEL_KEY", "sk-test")
    server = Server(_completion('{"answer": "ok", "score": 3}'))
    provider = OpenAICompatibleProvider(server.client(key_ref="env:MY_MODEL_KEY"))
    response = provider.complete(_request())
    assert Out.model_validate_json(response.data) == Out(answer="ok", score=3)
    assert (response.input_tokens, response.output_tokens) == (100, 20)
    assert response.model == "served-model"
    sent = server.requests[0]
    assert server.urls[0] == "http://models.internal:8000/v1/chat/completions"
    assert server.headers[0]["authorization"] == "Bearer sk-test"
    assert sent["model"] == "my-model"
    assert sent["max_tokens"] == 512
    assert [m["role"] for m in sent["messages"]] == ["system", "user"]
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["response_format"]["json_schema"]["schema"]["required"] == ["answer"]


def test_a_local_server_needs_no_api_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    server = Server(_completion('{"answer": "ok"}'))
    OpenAICompatibleProvider(server.client(key_ref="env:OPENAI_API_KEY")).complete(_request())
    assert "authorization" not in server.headers[0]


def test_falls_back_to_plain_json_mode_and_remembers() -> None:
    unsupported = (400, {"error": {"message": "response_format json_schema is not supported"}})
    server = Server(unsupported, _completion('{"answer": "a"}'), _completion('{"answer": "b"}'))
    provider = OpenAICompatibleProvider(server.client())
    assert '"a"' in provider.complete(_request()).data
    assert '"b"' in provider.complete(_request()).data
    modes = [r["response_format"]["type"] for r in server.requests]
    assert modes == ["json_schema", "json_object", "json_object"]  # no third probe
    # Without server-side schema enforcement the schema travels in the prompt instead.
    assert '"answer"' in server.requests[1]["messages"][1]["content"]
    assert "JSON Schema" in server.requests[1]["messages"][1]["content"]


def test_switches_to_max_completion_tokens_when_the_server_requires_it() -> None:
    wants_new = (
        400,
        {"error": {"message": "Unsupported parameter: use 'max_completion_tokens' instead."}},
    )
    server = Server(wants_new, _completion('{"answer": "a"}'), _completion('{"answer": "b"}'))
    provider = OpenAICompatibleProvider(server.client())
    provider.complete(_request())
    provider.complete(_request())
    assert "max_tokens" in server.requests[0]
    for later in server.requests[1:]:
        assert later["max_completion_tokens"] == 512
        assert "max_tokens" not in later


def test_code_fences_around_json_are_removed() -> None:
    server = Server(_completion('```json\n{"answer": "fenced"}\n```'))
    data = OpenAICompatibleProvider(server.client()).complete(_request()).data
    assert Out.model_validate_json(data).answer == "fenced"


def test_truncation_and_refusal_raise_with_the_billed_usage() -> None:
    server = Server(
        _completion('{"answ', finish_reason="length"),
        _completion(None, message={"refusal": "I cannot help with that."}),
        _completion("", finish_reason="content_filter"),
    )
    provider = OpenAICompatibleProvider(server.client())
    with pytest.raises(LLMOutputError, match="truncated") as truncated:
        provider.complete(_request())
    assert truncated.value.usage.output_tokens == 20
    for _ in range(2):
        with pytest.raises(LLMRefusal) as refused:
            provider.complete(_request())
        assert refused.value.usage.input_tokens == 100


def test_endpoint_errors_are_clear_and_do_not_fall_back() -> None:
    server = Server((401, {"error": {"message": "Incorrect API key"}}))
    with pytest.raises(LLMError, match="HTTP 401: Incorrect API key"):
        OpenAICompatibleProvider(server.client()).complete(_request())
    assert len(server.requests) == 1  # an auth error is not a "mode unsupported" signal

    def unreachable(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    client = ChatClient(
        "http://nowhere:1/v1", http=httpx.Client(transport=httpx.MockTransport(unreachable))
    )
    with pytest.raises(LLMError, match="unreachable"):
        OpenAICompatibleProvider(client).complete(_request())
    for body in ({"choices": []}, {"unexpected": True}):
        with pytest.raises(LLMError, match="no choices"):
            OpenAICompatibleProvider(Server(body).client()).complete(_request())


# --------------------------------------------------------------------------- through the gateway


def _use_openai(platform, seed, server: Server, **stage: Any) -> None:
    platform.gateway.providers["openai"] = OpenAICompatibleProvider(server.client())
    with platform.db.session() as s:
        project = s.get(Project, seed.project.id)
        project.settings = {
            **project.settings,
            "llm": {"default": {"provider": "openai", "model": "my-model", **stage}},
        }


def _call(platform, seed):
    with platform.db.session() as s:
        result = platform.gateway.structured(
            s,
            project=s.get(Project, seed.project.id),
            stage="triage",
            prompt_id="audit.triage",
            variables={"findings": [{"note": "password=hunter2hunter2"}]},
            schema=Out,
        )
        usage = [(u.provider, u.model, u.cost_usd) for u in s.query(UsageRecord)]
    return result, usage


def test_gateway_redacts_validates_and_uses_configured_prices(platform, seed) -> None:
    server = Server(_completion('{"answer": 1}'), _completion('{"answer": "fixed"}'))
    _use_openai(platform, seed, server, input_price_per_mtok=1000.0, output_price_per_mtok=5000.0)
    result, usage = _call(platform, seed)
    assert result.output.answer == "fixed"  # the invalid first answer was retried
    assert result.provider == "openai"
    assert "hunter2hunter2" not in json.dumps(server.requests)  # redacted before it left
    assert "failed schema validation" in server.requests[1]["messages"][1]["content"]
    per_call = (100 * 1000.0 + 20 * 5000.0) / 1_000_000
    assert [u[2] for u in usage] == [per_call, per_call]
    assert result.cost_usd == pytest.approx(2 * per_call)


def test_a_local_model_can_be_priced_at_zero_and_unpriced_models_are_not_free(platform, seed):
    _use_openai(
        platform,
        seed,
        Server(_completion('{"answer": "a"}')),
        input_price_per_mtok=0,
        output_price_per_mtok=0,
    )
    assert _call(platform, seed)[0].cost_usd == 0
    _use_openai(platform, seed, Server(_completion('{"answer": "a"}')))
    assert _call(platform, seed)[0].cost_usd > 0  # no price set: costed at the ceiling rate


def test_openai_provider_is_subject_to_the_allowlists(platform, seed) -> None:
    server = Server(_completion('{"answer": "a"}'))
    _use_openai(platform, seed, server)
    strict = LLMGateway(platform.gateway.providers, process_allowlist=["heuristic", "anthropic"])
    with platform.db.session() as s, pytest.raises(ProviderNotAllowed):
        strict.structured(
            s,
            project=s.get(Project, seed.project.id),
            stage="triage",
            prompt_id="audit.triage",
            variables={},
            schema=Out,
        )
    assert server.requests == []


def test_settings_and_default_gateway(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPSMITH_OPENAI_BASE_URL", "http://ollama:11434/v1/")
    monkeypatch.setenv("OPSMITH_OPENAI_JSON_MODE", "json_object")
    monkeypatch.setenv("OPSMITH_DATA_DIR", str(tmp_path))
    settings = Settings.from_env()
    assert "openai" in settings.llm_provider_allowlist
    gateway = default_gateway(settings)
    provider = gateway.providers["openai"]
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.client.base_url == "http://ollama:11434/v1"
    assert provider.json_mode == "json_object"
    assert set(gateway.providers) >= {"heuristic", "anthropic", "openai"}
    with pytest.raises(ValueError, match="json_mode"):
        Settings(openai_json_mode="xml")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="both"):
        StageModel(provider="openai", model="m", input_price_per_mtok=1.0)


# --------------------------------------------------------------------------- coding agent


def _tool_call(index: int, name: str, args: Any) -> dict[str, Any]:
    arguments = args if isinstance(args, str) else json.dumps(args)
    return {
        "id": f"call_{index}",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _turn(*calls: dict[str, Any], content: str | None = None, finish: str = "tool_calls"):
    message: dict[str, Any] = {"tool_calls": list(calls)} if calls else {}
    return _completion(content, message=message, finish_reason=finish)


def test_coding_agent_tool_loop(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x = 1\n")
    server = Server(
        _turn(
            _tool_call(0, "read_file", {"path": "app.py"}),
            _tool_call(1, "read_file", {"path": "../escape"}),
            _tool_call(2, "write_file", "{not json"),
        ),
        _turn(content="Let me think about it."),  # no tool call: nudged, not finished
        _turn(
            _tool_call(3, "replace_in_file", {"path": "app.py", "old_text": "1", "new_text": "2"}),
            _tool_call(4, "run_check", {"name": "test"}),
        ),
        _turn(_tool_call(5, "finish", {"success": True, "summary": "bumped x"})),
    )
    agent = OpenAICodingAgent(client=server.client(), model="my-model", prices=(1.0, 2.0))
    ws = Workspace(LocalSandbox(tmp_path), {"test": "grep -q 'x = 2' app.py"})
    result = agent.run(
        AgentTask("t", "Bump", "fix", "p", "a", ["c"], ["app.py"]),
        ws,
        AgentBudget(max_iterations=2),
    )
    assert result.success
    assert result.summary == "bumped x"
    assert (tmp_path / "app.py").read_text() == "x = 2\n"
    assert result.input_tokens == 400
    assert result.cost_usd == pytest.approx((400 * 1.0 + 80 * 2.0) / 1_000_000)

    first = server.requests[0]
    assert [t["function"]["name"] for t in first["tools"]] == [
        "list_files",
        "read_file",
        "write_file",
        "replace_in_file",
        "run_check",
        "finish",
    ]
    assert first["messages"][0]["role"] == "system"
    assert "<untrusted_data>" in first["messages"][1]["content"]
    # Tool results come back as `tool` messages tied to their call ids, errors included.
    second = server.requests[1]["messages"]
    assert second[2]["tool_calls"][0]["id"] == "call_0"
    results = {m["tool_call_id"]: m["content"] for m in second if m["role"] == "tool"}
    assert results["call_0"] == "x = 1\n"
    assert results["call_1"].startswith("error:")  # path escape refused by the workspace
    assert results["call_2"] == "invalid tool input"
    assert server.requests[2]["messages"][-1] == {
        "role": "user",
        "content": "Use the tools to make the change, then call `finish`.",
    }


def test_coding_agent_stops_at_budget_and_on_refusal(tmp_path: Path) -> None:
    listing = _turn(_tool_call(0, "list_files", {"path": "."}))
    server = Server(*[listing] * 10)
    agent = OpenAICodingAgent(client=server.client(), model="m")
    ws = Workspace(LocalSandbox(tmp_path), {})
    task = AgentTask("t", "x", "fix", "", "", [], [])
    result = agent.run(task, ws, AgentBudget(max_iterations=1))
    assert not result.success
    assert "budget" in result.summary
    assert len(server.requests) == 4
    refused = Server(_completion(None, message={"refusal": "no"}))
    result = OpenAICodingAgent(client=refused.client(), model="m").run(task, ws, AgentBudget())
    assert not result.success
    assert "declined" in result.summary


def test_executor_agent_factory_requires_an_openai_executor_model(settings) -> None:
    gateway = default_gateway(settings)
    factories = default_agent_factories(settings, gateway)
    assert {"heuristic", "claude", "openai"} <= set(factories)
    configured = ProjectSettings.model_validate(
        {
            "executor_agent": "openai",
            "llm": {
                "stages": {
                    "executor": {
                        "provider": "openai",
                        "model": "qwen2.5-coder",
                        "input_price_per_mtok": 0,
                        "output_price_per_mtok": 0,
                    }
                }
            },
        }
    )
    agent = factories["openai"](configured)
    assert isinstance(agent, OpenAICodingAgent)
    assert (agent.model, agent.prices) == ("qwen2.5-coder", (0.0, 0.0))
    with pytest.raises(LLMError, match=r"llm\.stages\.executor"):
        factories["openai"](ProjectSettings(executor_agent="openai"))
    narrowed = ProjectSettings.model_validate(
        {
            **configured.model_dump(),
            "llm": {**configured.llm.model_dump(), "provider_allowlist": []},
        }
    )
    with pytest.raises(ProviderNotAllowed):
        factories["openai"](narrowed)


# --------------------------------------------------------------------------- live (opt-in)

LIVE_URL = os.environ.get("OPSMITH_TEST_OPENAI_BASE_URL", "")
LIVE_MODEL = os.environ.get("OPSMITH_TEST_OPENAI_MODEL", "")
live = pytest.mark.skipif(
    not (LIVE_URL and LIVE_MODEL),
    reason="set OPSMITH_TEST_OPENAI_BASE_URL and OPSMITH_TEST_OPENAI_MODEL to run",
)


@live
def test_live_endpoint_structured_output() -> None:
    """Against a real OpenAI-compatible server (for example a local Ollama)."""
    provider = OpenAICompatibleProvider(ChatClient(LIVE_URL, "env:OPSMITH_TEST_OPENAI_API_KEY"))
    request = _request(
        model=LIVE_MODEL,
        system="You answer with JSON only.",
        user='Set "answer" to the capital of France and "score" to 7.',
    )
    response = provider.complete(request)
    parsed = Out.model_validate_json(response.data)
    assert "paris" in parsed.answer.lower()
    assert response.input_tokens > 0
    assert response.output_tokens > 0


@live
def test_live_endpoint_coding_agent(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("TIMEOUT = 0\n")
    agent = OpenAICodingAgent(client=ChatClient(LIVE_URL), model=LIVE_MODEL, max_tokens=2048)
    ws = Workspace(LocalSandbox(tmp_path), {"test": "grep -q 'TIMEOUT = 10' app.py"})
    task = AgentTask(
        "t",
        "Set the timeout to 10",
        "fix",
        "app.py sets TIMEOUT = 0, which disables the timeout.",
        "In app.py replace `TIMEOUT = 0` with `TIMEOUT = 10`, run the test check, then finish.",
        ["app.py contains TIMEOUT = 10"],
        ["app.py"],
    )
    result = agent.run(task, ws, AgentBudget(max_iterations=4))
    assert any(step["tool"] for step in result.steps), "the model made no tool calls"
    assert result.input_tokens > 0
