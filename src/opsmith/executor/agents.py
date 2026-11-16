"""Coding subagents.

Every agent works through the same *fixed* tool set (list/read/write/replace files, run a
configured check by name, finish). None can run arbitrary commands, reach the network, or
change its own tools or permissions.

* ``HeuristicAgent`` - deterministic codemods for well-understood audit rules. Lets the full
  loop run offline (demo, CI) and is a cheap first pass for trivial fixes.
* ``ClaudeCodingAgent`` - Claude, through the Anthropic SDK.
* ``OpenAICodingAgent`` - any model behind an OpenAI-compatible endpoint that supports tool
  calling (OpenAI, Azure OpenAI, Ollama, vLLM, LiteLLM, OpenRouter, ...).
"""

from __future__ import annotations

import ast
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from opsmith.executor.sandbox import Workspace, WorkspaceError
from opsmith.llm.anthropic_provider import fallback_kwargs, make_client
from opsmith.llm.openai_provider import ChatClient, usage_of
from opsmith.llm.pricing import cost_usd
from opsmith.llm.prompts import INJECTION_GUARD, untrusted_json

log = logging.getLogger(__name__)


@dataclass
class AgentTask:
    id: str
    title: str
    category: str
    problem_statement: str
    proposed_approach: str
    acceptance_criteria: list[str]
    paths: list[str]
    findings: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AgentBudget:
    max_iterations: int = 8
    max_tokens: int = 400_000


@dataclass
class AgentResult:
    success: bool
    summary: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    retryable: bool = False


class CodingAgent(Protocol):
    name: str

    def run(self, task: AgentTask, ws: Workspace, budget: AgentBudget) -> AgentResult: ...


# --------------------------------------------------------------------------- heuristic


class HeuristicAgent:
    name = "heuristic"

    def run(self, task: AgentTask, ws: Workspace, budget: AgentBudget) -> AgentResult:
        steps: list[dict[str, Any]] = [{"step": "plan", "detail": "apply deterministic codemods"}]
        applied = 0
        for f in task.findings:
            rule, path, line = f.get("rule_id") or "", f.get("path"), f.get("line")
            if not path or not line:
                continue
            fixer = _CODEMODS.get(rule.split(".", 1)[0] if rule.startswith("secret.") else rule)
            if fixer is None:
                continue
            try:
                text = ws.read_file(path, max_chars=10_000_000)
            except WorkspaceError as exc:
                steps.append({"step": "skip", "detail": str(exc)})
                continue
            new = fixer(text, int(line))
            if new is not None and new != text:
                ws.write_file(path, new)
                applied += 1
                steps.append({"step": "edit", "path": path, "rule": rule, "line": line})
        if not applied:
            return AgentResult(False, "No deterministic fix applies; use the Claude agent.", steps)
        if "test" in ws.checks:
            result = ws.run_check("test")
            steps.append({"step": "check", "name": "test", "exit_code": result.exit_code})
            if not result.ok:
                return AgentResult(False, "Tests fail after codemods.", steps)
        return AgentResult(True, f"Applied {applied} deterministic fix(es).", steps)


def _replace_line(text: str, line: int, fn: Any) -> str | None:
    lines = text.splitlines(keepends=True)
    if not 0 < line <= len(lines):
        return None
    updated = fn(lines[line - 1])
    if updated is None:
        return None
    lines[line - 1] = updated
    return "".join(lines)


def _fix_bare_except(text: str, line: int) -> str | None:
    return _replace_line(
        text, line, lambda s: re.sub(r"\bexcept\s*:", "except Exception:", s, count=1)
    )


def _fix_yaml_load(text: str, line: int) -> str | None:
    return _replace_line(text, line, lambda s: s.replace("yaml.load(", "yaml.safe_load(", 1))


def _fix_http_timeout(text: str, line: int) -> str | None:
    def fn(s: str) -> str | None:
        stripped = s.rstrip("\n")
        if stripped.count("(") != stripped.count(")") or not stripped.rstrip().endswith(")"):
            return None
        idx = stripped.rstrip().rfind(")")
        inner_empty = stripped[:idx].rstrip().endswith("(")
        sep = "" if inner_empty else ", "
        return (
            stripped[:idx]
            + f"{sep}timeout=10"
            + stripped[idx:]
            + ("\n" if s.endswith("\n") else "")
        )

    return _replace_line(text, line, fn)


_ASSIGN = re.compile(
    r"^(?P<indent>\s*)(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<q>['\"]).*(?P=q)\s*$"
)


def _fix_secret(text: str, line: int) -> str | None:
    lines = text.splitlines(keepends=True)
    if not 0 < line <= len(lines):
        return None
    m = _ASSIGN.match(lines[line - 1].rstrip("\n"))
    if not m:
        return None
    name = m.group("name")
    lines[line - 1] = f'{m.group("indent")}{name} = os.environ.get("{name.upper()}", "")\n'
    out = "".join(lines)
    if not re.search(r"^import os\b", out, re.M):
        out = _add_import(out, "import os")
    return out


def _add_import(text: str, stmt: str) -> str:
    """Insert ``stmt`` after the module docstring and any ``from __future__`` imports."""
    lines = text.splitlines(keepends=True)
    insert = 0
    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None
    if tree is not None:
        for i, node in enumerate(tree.body):
            is_doc = (
                i == 0
                and isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            )
            is_future = isinstance(node, ast.ImportFrom) and node.module == "__future__"
            if is_doc or is_future:
                insert = node.end_lineno or insert
            else:
                break
    lines.insert(insert, stmt + "\n")
    return "".join(lines)


_CODEMODS = {
    "py.bare-except": _fix_bare_except,
    "py.yaml-load": _fix_yaml_load,
    "py.http-no-timeout": _fix_http_timeout,
    "secret": _fix_secret,
}


# --------------------------------------------------------------------------- claude


def _tool(name: str, description: str, props: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": list(props),
            "additionalProperties": False,
        },
    }


_S = {"type": "string"}
AGENT_TOOLS = [
    _tool(
        "list_files",
        "List repository files under a relative directory ('.' for root).",
        {"path": _S},
    ),
    _tool("read_file", "Read a UTF-8 text file (relative path).", {"path": _S}),
    _tool(
        "write_file",
        "Create or overwrite a file with the full new content.",
        {"path": _S, "content": _S},
    ),
    _tool(
        "replace_in_file",
        "Replace exactly one occurrence of old_text with new_text.",
        {"path": _S, "old_text": _S, "new_text": _S},
    ),
    _tool(
        "run_check",
        "Run a configured repository check by name: build, lint or test.",
        {"name": {"type": "string", "enum": ["build", "lint", "test"]}},
    ),
    _tool(
        "finish",
        "Call when done. success=false if the task cannot be completed safely.",
        {"success": {"type": "boolean"}, "summary": _S},
    ),
]

AGENT_SYSTEM = (
    "You are an Opsmith coding subagent working in an isolated checkout of one repository. "
    "Implement the approved task with the smallest correct change, keep to the task's paths "
    "unless a change elsewhere is strictly required, add or update tests that prove the fix, "
    "and run the `test` check (plus `lint`/`build` when available) until they pass. You "
    "cannot run arbitrary commands or access the network. Never add credentials. When the "
    "checks pass, call `finish` with success=true and a short summary of what changed; if "
    "the task cannot be done safely, call `finish` with success=false and explain why.\n\n"
    + INJECTION_GUARD
)


class ClaudeCodingAgent:
    name = "claude"

    def __init__(
        self,
        *,
        model: str = "claude-opus-5-5",
        effort: str = "high",
        max_tokens: int = 32000,
        client: Any | None = None,
        api_key_ref: str | None = None,
    ) -> None:
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self._client = client
        self._api_key_ref = api_key_ref

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = make_client(self._api_key_ref)
        return self._client

    def run(self, task: AgentTask, ws: Workspace, budget: AgentBudget) -> AgentResult:
        messages: list[dict[str, Any]] = [{"role": "user", "content": task_brief(task, ws)}]
        steps: list[dict[str, Any]] = []
        tin = tout = 0
        cost = 0.0
        for turn in range(budget.max_iterations * 4):  # several tool calls per iteration
            if tin + tout >= budget.max_tokens:
                return AgentResult(False, "Token budget exhausted.", steps, tin, tout, cost)
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=AGENT_SYSTEM,
                tools=AGENT_TOOLS,
                messages=messages,
                output_config={"effort": self.effort},
                **fallback_kwargs(),
            )
            usage = resp.usage
            tin += int(getattr(usage, "input_tokens", 0) or 0)
            tout += int(getattr(usage, "output_tokens", 0) or 0)
            cost = cost_usd(self.model, tin, tout)
            if resp.stop_reason == "refusal":
                return AgentResult(False, "Model declined the task.", steps, tin, tout, cost)
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "pause_turn":
                continue
            if resp.stop_reason == "max_tokens":
                messages.append({"role": "user", "content": "Continue."})
                continue
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                messages.append(
                    {
                        "role": "user",
                        "content": "Use the tools to make the change, then call `finish`.",
                    }
                )
                continue
            results: list[dict[str, Any]] = []
            finished: tuple[bool, str] | None = None
            for tu in tool_uses:
                content, is_error = dispatch_tool(ws, tu.name, tu.input)
                steps.append(
                    {
                        "step": "tool",
                        "turn": turn,
                        "tool": tu.name,
                        "path": (tu.input or {}).get("path"),
                        "error": is_error,
                    }
                )
                if tu.name == "finish" and not is_error:
                    finished = (bool(tu.input.get("success")), str(tu.input.get("summary", "")))
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": content,
                        "is_error": is_error,
                    }
                )
            messages.append({"role": "user", "content": results})
            if finished is not None:
                return AgentResult(finished[0], finished[1][:2000], steps, tin, tout, cost)
        return AgentResult(False, "Iteration budget exhausted.", steps, tin, tout, cost)


def dispatch_tool(ws: Workspace, name: str, args: Any) -> tuple[str, bool]:
    """Run one tool call against the confined workspace: ``(result text, is_error)``.

    The only place a model's tool request turns into an action, for every agent."""
    if not isinstance(args, dict):
        return "invalid tool input", True
    try:
        if name == "list_files":
            return "\n".join(ws.list_files(str(args.get("path", ".")))), False
        if name == "read_file":
            return ws.read_file(str(args["path"])), False
        if name == "write_file":
            ws.write_file(str(args["path"]), str(args["content"]))
            return "ok", False
        if name == "replace_in_file":
            ws.replace_in_file(str(args["path"]), str(args["old_text"]), str(args["new_text"]))
            return "ok", False
        if name == "run_check":
            res = ws.run_check(str(args["name"]))
            return f"exit_code={res.exit_code}\n{res.output[-8000:]}", False
        if name == "finish":
            return "acknowledged", False
    except (WorkspaceError, KeyError, OSError) as exc:
        return f"error: {exc}", True
    return f"unknown tool {name}", True


def task_brief(task: AgentTask, ws: Workspace) -> str:
    """The first user message for a model-driven agent. Everything in a task derives from
    logs, code or model output, so it is framed as untrusted data."""
    brief = {
        "title": task.title,
        "category": task.category,
        "problem_statement": task.problem_statement,
        "proposed_approach": task.proposed_approach,
        "acceptance_criteria": task.acceptance_criteria,
        "paths": task.paths,
        "findings": task.findings[:20],
        "available_checks": sorted(ws.checks),
    }
    return (
        "Implement this approved task.\n<untrusted_data>\n"
        + untrusted_json(brief, sort_keys=False)
        + "\n</untrusted_data>"
    )


# --------------------------------------------------------------------------- openai-compatible


class OpenAICodingAgent:
    """The same tool loop over the Chat Completions tool-calling protocol.

    Needs a model that supports tool calling. Costs come from the prices configured on the
    executor stage, because there is no price list for arbitrary endpoints.
    """

    name = "openai"

    def __init__(
        self,
        *,
        client: ChatClient,
        model: str,
        max_tokens: int = 16000,
        prices: tuple[float, float] | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.prices = prices

    @staticmethod
    def _tools() -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["input_schema"],
                },
            }
            for tool in AGENT_TOOLS
        ]

    def run(self, task: AgentTask, ws: Workspace, budget: AgentBudget) -> AgentResult:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": AGENT_SYSTEM},
            {"role": "user", "content": task_brief(task, ws)},
        ]
        steps: list[dict[str, Any]] = []
        tin = tout = 0
        cost = 0.0
        for turn in range(budget.max_iterations * 4):  # several tool calls per iteration
            if tin + tout >= budget.max_tokens:
                return AgentResult(False, "Token budget exhausted.", steps, tin, tout, cost)
            body = self.client.chat(
                {"model": self.model, "messages": messages, "tools": self._tools()},
                max_tokens=self.max_tokens,
            )
            used_in, used_out = usage_of(body)
            tin, tout = tin + used_in, tout + used_out
            cost = cost_usd(self.model, tin, tout, prices=self.prices)
            choice = body["choices"][0]
            message = choice.get("message") or {}
            if message.get("refusal") or choice.get("finish_reason") == "content_filter":
                return AgentResult(False, "Model declined the task.", steps, tin, tout, cost)
            calls = message.get("tool_calls") or []
            # Echo the assistant turn back exactly as the protocol expects it.
            reply: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
            if calls:
                reply["tool_calls"] = calls
            messages.append(reply)
            if not calls:
                nudge = (
                    "Continue."
                    if choice.get("finish_reason") == "length"
                    else "Use the tools to make the change, then call `finish`."
                )
                messages.append({"role": "user", "content": nudge})
                continue
            finished: tuple[bool, str] | None = None
            for call in calls:
                function = call.get("function") or {}
                name = str(function.get("name", ""))
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except ValueError:
                    args = None  # reported back to the model as invalid input
                content, is_error = dispatch_tool(ws, name, args)
                steps.append(
                    {
                        "step": "tool",
                        "turn": turn,
                        "tool": name,
                        "path": args.get("path") if isinstance(args, dict) else None,
                        "error": is_error,
                    }
                )
                if name == "finish" and not is_error and isinstance(args, dict):
                    finished = (bool(args.get("success")), str(args.get("summary", "")))
                messages.append(
                    {"role": "tool", "tool_call_id": call.get("id", ""), "content": content}
                )
            if finished is not None:
                return AgentResult(finished[0], finished[1][:2000], steps, tin, tout, cost)
        return AgentResult(False, "Iteration budget exhausted.", steps, tin, tout, cost)
