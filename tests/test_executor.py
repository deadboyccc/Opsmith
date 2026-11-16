from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from opsmith import queue
from opsmith.audit_log import transition_task
from opsmith.executor.agents import (
    AgentBudget,
    AgentResult,
    AgentTask,
    ClaudeCodingAgent,
    _fix_bare_except,
    _fix_http_timeout,
    _fix_secret,
)
from opsmith.executor.sandbox import DockerSandbox, LocalSandbox, Workspace, WorkspaceError
from opsmith.executor.service import ApprovalError, assert_approved
from opsmith.gitutil import run_git
from opsmith.models import AuditLog, ExecutionRun, Finding, Project, Review, Task, User
from opsmith.platform import Platform
from opsmith.review import ReviewRequest
from opsmith.scm.local import LocalSCM
from tests.conftest import FAKE_AWS_KEY
from tests.test_aggregation import add_finding, aggregate


def make_task(platform, seed, **finding_kw) -> str:
    with platform.db.session() as s:
        add_finding(s, seed, **finding_kw)
    aggregate(platform, seed)
    with platform.db.session() as s:
        return s.scalars(select(Task).order_by(Task.created_at.desc())).first().id


def approve(platform, seed, task_id: str, user_id: str | None = None, **kw) -> str:
    with platform.db.session() as s:
        rev = platform.reviews.review(
            s,
            s.get(Task, task_id),
            s.get(User, user_id or seed.reviewer.id),
            ReviewRequest(decision="approve", **kw),
        )
        return rev.id


def task(platform, task_id: str) -> Task:
    with platform.db.session() as s:
        return s.get(Task, task_id)


def test_no_execution_without_recorded_human_approval(platform, seed) -> None:
    task_id = make_task(platform, seed)
    result = platform.executor.execute(task_id, "rev_forged")
    assert "refused" in result
    with platform.db.session() as s:
        t = s.get(Task, task_id)
        assert t.status == "in_review"
        # Even if the status were forced to approved, a service principal's "approval" fails.
        transition_task(s, t, "approved")
        s.add(
            Review(
                tenant_id=t.tenant_id,
                task_id=t.id,
                reviewer_id=seed.service_user.id,
                reviewer_name="bot",
                reviewer_role="service",
                decision="approve",
            )
        )
        s.flush()
        with pytest.raises(ApprovalError, match="human"):
            assert_approved(s, t)
        assert s.scalar(select(AuditLog).where(AuditLog.action == "execution.refused"))
    assert not LocalSCM(platform.settings.data_dir / "local_prs")._load(seed.repo)


def test_approved_task_becomes_pr_with_ci(platform, seed, drain) -> None:
    task_id = make_task(platform, seed)
    approve(platform, seed, task_id)
    drain()
    t = task(platform, task_id)
    assert t.status == "pr_open", t.execution_report
    assert t.pr_url == "local://payments-api/pull/1"
    with platform.db.session() as s:
        run = s.scalars(select(ExecutionRun)).one()
        assert run.status == "succeeded"
        assert run.changed_files == ["app/payments.py"]
        assert run.checks[0]["name"] == "test"
        assert run.checks[0]["exit_code"] == 0
    prs = LocalSCM(platform.settings.data_dir / "local_prs")._load(seed.repo)
    assert prs[0]["ci"] == "success"
    assert f"opsmith:task={task_id}" in prs[0]["body"]
    assert "approved by Rui Reviewer" in prs[0]["body"]
    diff = run_git(["diff", "main", prs[0]["branch"]], seed.repo_path)
    assert "timeout=10" in diff
    log = run_git(["log", "-1", "--format=%an <%ae>%n%B", prs[0]["branch"]], seed.repo_path)
    assert "Opsmith Bot" in log
    assert f"Opsmith-Task: {task_id}" in log
    assert ".opsmith-home" not in run_git(["show", "--stat", prs[0]["branch"]], seed.repo_path)


def test_failed_execution_returns_to_review_with_report(platform, seed, drain) -> None:
    task_id = make_task(platform, seed, rule="py.mutable-default", fp="fp-m", category="fix")
    approve(platform, seed, task_id)
    drain()
    t = task(platform, task_id)
    assert t.status == "in_review"
    assert "No deterministic fix" in t.execution_report["error"]
    with platform.db.session() as s:
        states = [
            a.to_state
            for a in s.scalars(
                select(AuditLog)
                .where(AuditLog.entity_id == task_id, AuditLog.entity_type == "task")
                .order_by(AuditLog.id)
            )
        ]
        assert states[-4:] == ["approved", "executing", "failed", "in_review"]


class EditingAgent:
    name = "scripted"

    def __init__(self, path: str, content: str) -> None:
        self.path, self.content = path, content

    def run(self, task: AgentTask, ws: Workspace, budget: AgentBudget) -> AgentResult:
        ws.write_file(self.path, self.content)
        return AgentResult(True, "edited")


def _use_agent(platform: Platform, seed, agent: Any) -> None:
    with platform.db.session() as s:
        project = s.get(Project, seed.project.id)
        project.settings = {**project.settings, "executor_agent": "scripted"}
    platform.executor.agent_factories["scripted"] = lambda _ps: agent


def test_diff_touching_protected_path_is_blocked(platform, seed, drain) -> None:
    task_id = make_task(platform, seed)
    _use_agent(platform, seed, EditingAgent(".github/workflows/ci.yml", "on: push\n"))
    approve(platform, seed, task_id)
    drain()
    t = task(platform, task_id)
    assert t.status == "in_review"
    assert "protected paths" in t.execution_report["error"]
    assert t.protection == "elevated"


def test_diff_introducing_a_secret_is_blocked(platform, seed, drain) -> None:
    task_id = make_task(platform, seed)
    _use_agent(platform, seed, EditingAgent("app/keys.py", f'K = "{FAKE_AWS_KEY}"\n'))
    approve(platform, seed, task_id)
    drain()
    assert "credential-like" in task(platform, task_id).execution_report["error"]


def test_tasks_touching_same_files_are_serialized(platform, seed) -> None:
    first = make_task(platform, seed)
    second = make_task(platform, seed, fp="fp-other", rule="py.eval", category="security", line=50)
    with platform.db.session() as s:
        t1 = s.get(Task, first)
        transition_task(s, t1, "approved")
        transition_task(s, t1, "executing")
    rev = approve(platform, seed, second)
    with pytest.raises(queue.RetryLater, match="same files"):
        platform.executor.execute(second, rev)
    assert task(platform, second).status == "approved"


def test_workspace_confinement(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / ".git").mkdir()
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret")
    (tmp_path / "link").symlink_to(outside)
    ws = Workspace(LocalSandbox(tmp_path), {"test": "true"})
    assert ws.read_file("a.txt") == "hello"
    for bad in ("../outside.txt", "/etc/passwd", ".git/config", "link", ""):
        with pytest.raises(WorkspaceError):
            ws.read_file(bad)
    with pytest.raises(WorkspaceError, match="unknown check"):
        ws.run_check("rm -rf /")
    ws.replace_in_file("a.txt", "hello", "bye")
    with pytest.raises(WorkspaceError, match="exactly one"):
        ws.replace_in_file("a.txt", "zzz", "x")
    assert ws.run_check("test").ok


def test_sandbox_environment_is_scrubbed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    out = LocalSandbox(tmp_path).run("env")
    assert "should_not_leak" not in out.output
    assert "should-not-leak" not in out.output
    timed = LocalSandbox(tmp_path).run("sleep 5", timeout=1)
    assert timed.timed_out
    assert not timed.ok


def test_docker_sandbox_isolation_flags(tmp_path: Path) -> None:
    cmd = DockerSandbox(tmp_path, "python:3.12-slim").docker_command("pytest -q")
    joined = " ".join(cmd)
    for flag in (
        "--network none",
        "--cap-drop ALL",
        "no-new-privileges",
        "--pids-limit",
        f"{tmp_path.resolve()}:/workspace",
    ):
        assert flag in joined
    assert cmd[-3:] == ["sh", "-c", "exec 2>&1; pytest -q"]
    for flag in ("--read-only", "--init", "--memory-swap"):
        assert flag in cmd
    assert "/var/run/docker.sock" not in joined


def test_codemods() -> None:
    assert _fix_bare_except("try:\n    x()\nexcept:\n    pass\n", 3) == (
        "try:\n    x()\nexcept Exception:\n    pass\n"
    )
    assert _fix_http_timeout("r = httpx.get(url)\n", 1) == "r = httpx.get(url, timeout=10)\n"
    assert _fix_http_timeout("r = httpx.get()\n", 1) == "r = httpx.get(timeout=10)\n"
    assert _fix_http_timeout("r = httpx.get(\n", 1) is None
    fixed = _fix_secret('"""Doc."""\nfrom __future__ import annotations\nKEY = "abc"\n', 3)
    assert fixed == (
        '"""Doc."""\nfrom __future__ import annotations\nimport os\n'
        'KEY = os.environ.get("KEY", "")\n'
    )


class FakeClaude:
    """Mimics ``client.messages.create`` for the tool loop."""

    def __init__(self, script: list[list[dict[str, Any]]]) -> None:
        self.script = script
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})  # snapshot
        blocks = [
            SimpleNamespace(type="tool_use", id=f"tu{i}", name=b["name"], input=b["input"])
            for i, b in enumerate(self.script.pop(0))
        ]
        return SimpleNamespace(
            stop_reason="tool_use",
            content=blocks,
            usage=SimpleNamespace(input_tokens=1000, output_tokens=100),
        )


def test_claude_agent_tool_loop(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPSMITH_ANTHROPIC_FALLBACKS", "off")
    (tmp_path / "app.py").write_text("x = 1\n")
    client = FakeClaude(
        [
            [
                {"name": "read_file", "input": {"path": "app.py"}},
                {"name": "read_file", "input": {"path": "../escape"}},
            ],
            [
                {
                    "name": "replace_in_file",
                    "input": {"path": "app.py", "old_text": "x = 1", "new_text": "x = 2"},
                },
                {"name": "run_check", "input": {"name": "test"}},
            ],
            [{"name": "finish", "input": {"success": True, "summary": "bumped x"}}],
        ]
    )
    agent = ClaudeCodingAgent(client=client, model="claude-opus-5-5", effort="high")
    ws = Workspace(LocalSandbox(tmp_path), {"test": "grep -q 'x = 2' app.py"})
    res = agent.run(
        AgentTask("t", "Bump", "fix", "p", "a", ["c"], ["app.py"]),
        ws,
        AgentBudget(max_iterations=2),
    )
    assert res.success
    assert res.summary == "bumped x"
    assert (tmp_path / "app.py").read_text() == "x = 2\n"
    first = client.calls[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["output_config"] == {"effort": "high"}
    assert all(t["strict"] for t in first["tools"])
    results = client.calls[1]["messages"][-1]["content"]
    assert results[0]["is_error"] is False
    assert results[1]["is_error"] is True
    assert "<untrusted_data>" in first["messages"][0]["content"]
    assert res.input_tokens == 3000


def test_claude_agent_respects_iteration_budget(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPSMITH_ANTHROPIC_FALLBACKS", "off")
    client = FakeClaude([[{"name": "list_files", "input": {"path": "."}}]] * 10)
    agent = ClaudeCodingAgent(client=client)
    res = agent.run(
        AgentTask("t", "x", "fix", "", "", [], []),
        Workspace(LocalSandbox(tmp_path), {}),
        AgentBudget(max_iterations=1),
    )
    assert not res.success
    assert "budget" in res.summary
    assert len(client.calls) == 4


def test_findings_feed_agent_task(platform, seed) -> None:
    task_id = make_task(platform, seed)
    with platform.db.session() as s:
        agent_task = platform.executor._agent_task(s, s.get(Task, task_id))
        assert agent_task.findings[0]["rule_id"] == "py.http-no-timeout"
        assert s.scalars(select(Finding)).first().task_id == task_id


def test_claude_agent_keeps_what_it_spent_when_a_call_fails(tmp_path: Path, monkeypatch) -> None:
    """A provider error part-way through must not lose the turns already billed."""
    monkeypatch.setenv("OPSMITH_ANTHROPIC_FALLBACKS", "off")
    client = FakeClaude([[{"name": "list_files", "input": {"path": "."}}]])  # then runs dry

    def create(**kwargs: Any) -> Any:
        if not client.script:
            raise RuntimeError("overloaded")
        return FakeClaude.create(client, **kwargs)

    client.messages = SimpleNamespace(create=create)
    agent = ClaudeCodingAgent(client=client, model="claude-opus-5-5")
    res = agent.run(
        AgentTask("t", "x", "fix", "", "", [], []),
        Workspace(LocalSandbox(tmp_path), {}),
        AgentBudget(),
    )
    assert not res.success
    assert res.retryable
    assert "overloaded" in res.summary
    assert (res.input_tokens, res.output_tokens) == (1000, 100)
    assert res.cost_usd > 0


def test_claude_agent_stops_when_the_cost_budget_is_spent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPSMITH_ANTHROPIC_FALLBACKS", "off")
    client = FakeClaude([[{"name": "list_files", "input": {"path": "."}}]] * 10)
    agent = ClaudeCodingAgent(client=client, model="claude-opus-5-5")
    # One turn is 1000 input + 100 output tokens of claude-opus-5-5: $0.006.
    res = agent.run(
        AgentTask("t", "x", "fix", "", "", [], []),
        Workspace(LocalSandbox(tmp_path), {}),
        AgentBudget(max_iterations=5, max_cost_usd=0.01),
    )
    assert res.summary == "Cost budget exhausted."
    assert len(client.calls) == 2
