"""Adversarial tests: each one tries a specific way around a safety or reliability guarantee
and asserts the bad path is blocked. Grouped by the guarantee they protect."""

from __future__ import annotations

import shutil
import subprocess
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from opsmith import queue
from opsmith.api.app import create_app
from opsmith.config import (
    CredentialRefError,
    Settings,
    check_credentials_ref,
    resolve_tenant_credential,
)
from opsmith.db import utcnow
from opsmith.etl.features import metric_signal
from opsmith.executor.agents import AgentBudget, AgentResult, AgentTask
from opsmith.executor.sandbox import (
    DockerSandbox,
    LocalSandbox,
    SandboxUnavailable,
    Workspace,
    WorkspaceError,
    make_sandbox,
)
from opsmith.executor.service import sandbox_policy_error
from opsmith.gitutil import run_git, staged_added_lines, worktree_changes
from opsmith.llm import BudgetGuard
from opsmith.models import (
    AuditLog,
    ExecutionRun,
    Finding,
    Job,
    Notification,
    Project,
    Repository,
    Review,
    Schedule,
    Task,
    UsageRecord,
    User,
)
from opsmith.platform import Platform
from opsmith.queue import Worker
from opsmith.review import ReviewError, ReviewRequest, valid_branch_name
from opsmith.scheduler import (
    CronExpr,
    compute_next,
    ensure_project_schedules,
    upsert_schedule,
)
from opsmith.schemas import ProjectSettings
from opsmith.scm.base import SCMError, check_pushable
from opsmith.security import create_tenant, create_user
from tests.conftest import FAKE_AWS_KEY
from tests.test_executor import EditingAgent, _use_agent, approve, make_task, task


def _pr_branches(seed: Any) -> str:
    return run_git(["branch", "--list", "opsmith/*"], seed.repo_path).strip()


def _set_repo(platform: Platform, seed: Any, **fields: Any) -> None:
    with platform.db.session() as s:
        repo = s.get(Repository, seed.repo.id)
        for key, value in fields.items():
            setattr(repo, key, value)


# --------------------------------------------------------------------------- diff gates


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    run_git(["init", "-q", "-b", "main"], path)
    run_git(["commit", "-q", "--allow-empty", "-m", "init"], path, author=("t", "t@example.com"))
    return path


def test_unusual_file_names_are_reported_verbatim(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "r")
    (repo / ".github" / "workflows").mkdir(parents=True)
    # Line-based `git status` output would render this as a rename to `innocuous.yml`.
    (repo / ".github" / "workflows" / "x -> innocuous.yml").write_text("on: push\n")
    (repo / "with space.txt").write_text("y\n")
    (repo / "café.txt").write_text("z\n")
    assert sorted(worktree_changes(repo)) == [
        ".github/workflows/x -> innocuous.yml",
        "café.txt",
        "with space.txt",
    ]


def test_renames_report_both_paths(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "r")
    (repo / "old.py").write_text("x = 1\n" * 20)
    run_git(["add", "-A"], repo)
    run_git(["commit", "-q", "-m", "add"], repo, author=("t", "t@example.com"))
    (repo / "old.py").rename(repo / "new.py")
    run_git(["add", "-A"], repo)
    assert sorted(worktree_changes(repo)) == ["new.py", "old.py"]


def test_added_lines_that_look_like_diff_headers_are_still_scanned(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "r")
    (repo / "notes.txt").write_text('++ password = "hunter2hunter2"\n-- also this\nplain\n')
    (repo / "with space.txt").write_text("second file\n")
    added = staged_added_lines(repo)
    assert ("notes.txt", 1, '++ password = "hunter2hunter2"') in added
    assert ("notes.txt", 3, "plain") in added
    assert ("with space.txt", 1, "second file") in added


def test_protected_path_gate_cannot_be_bypassed_by_file_name(platform, seed, drain) -> None:
    task_id = make_task(platform, seed)
    _use_agent(platform, seed, EditingAgent(".github/workflows/x -> innocuous.yml", "on: push\n"))
    approve(platform, seed, task_id)
    drain()
    t = task(platform, task_id)
    assert t.status == "in_review"
    assert "protected paths" in t.execution_report["error"]
    assert not _pr_branches(seed)


def test_secret_gate_cannot_be_bypassed_by_line_prefix(platform, seed, drain) -> None:
    task_id = make_task(platform, seed)
    content = '++ api_key = "0123456789abcdef0123456789abcdef"\n'
    _use_agent(platform, seed, EditingAgent("docs/notes.txt", content))
    approve(platform, seed, task_id)
    drain()
    assert "credential-like" in task(platform, task_id).execution_report["error"]
    assert not _pr_branches(seed)


def test_files_written_by_the_check_run_are_gated_too(platform, seed, drain) -> None:
    """The platform re-runs the checks itself. Whatever that run writes must pass the same
    gates as the agent's edits: the first invocation (the agent's) is clean, the second
    (the platform's) drops a workflow file."""
    task_id = make_task(platform, seed)
    _set_repo(
        platform,
        seed,
        test_command=(
            "if [ -f .opsmith-home/seen ]; then mkdir -p .github/workflows && "
            "echo 'on: push' > .github/workflows/evil.yml; else touch .opsmith-home/seen; fi"
        ),
    )
    approve(platform, seed, task_id)
    drain()
    t = task(platform, task_id)
    assert t.status == "in_review"
    assert "protected paths" in t.execution_report["error"]
    assert not _pr_branches(seed)


# --------------------------------------------------------------------------- sandbox boundary


def test_git_hooks_and_config_planted_by_sandboxed_code_never_run(
    platform, seed, drain, tmp_path: Path
) -> None:
    """Sandboxed commands can write anywhere in the workspace, including `.git`. The platform
    commits and pushes from its own snapshot, so nothing planted there is executed."""
    hook_marker, config_marker = tmp_path / "hook-ran", tmp_path / "config-ran"
    task_id = make_task(platform, seed)
    _set_repo(
        platform,
        seed,
        test_command=(
            "mkdir -p .git/hooks && "
            f"printf '#!/bin/sh\\ntouch {hook_marker}\\n' > .git/hooks/pre-commit && "
            "chmod +x .git/hooks/pre-commit && cp .git/hooks/pre-commit .git/hooks/pre-push && "
            f"printf '[core]\\n\\tfsmonitor = touch {config_marker}\\n' >> .git/config"
        ),
    )
    approve(platform, seed, task_id)
    drain()
    assert task(platform, task_id).status == "pr_open", task(platform, task_id).execution_report
    assert not hook_marker.exists()
    assert not config_marker.exists()
    assert ".git/" not in run_git(["show", "--stat", _pr_branches(seed)], seed.repo_path)


def test_a_nested_repository_planted_by_sandboxed_code_is_refused(
    platform, seed, drain, tmp_path: Path
) -> None:
    """The repository has a submodule (checked out empty). Sandboxed code fills it with a
    repository whose config defines a filter driver. `git status` in the parent descends into
    the submodule and would run that driver on the worker, outside the sandbox."""
    marker = tmp_path / "filter-ran"
    lib = _git_repo(tmp_path / "lib")
    (lib / "a.txt").write_text("one\n")
    run_git(["add", "a.txt"], lib)
    run_git(["commit", "-q", "-m", "lib"], lib, author=("t", "t@example.com"))
    run_git(
        ["-c", "protocol.file.allow=always", "submodule", "add", "-q", str(lib), "sub"],
        seed.repo_path,
    )
    run_git(["commit", "-q", "-m", "add sub"], seed.repo_path, author=("t", "t@example.com"))
    task_id = make_task(platform, seed)
    _set_repo(
        platform,
        seed,
        test_command=(
            f"cp -r {lib}/. sub/ && git -C sub config filter.x.clean 'touch {marker}; cat' && "
            "echo '* filter=x' > sub/.gitattributes && echo two > sub/a.txt"
        ),
    )
    approve(platform, seed, task_id)
    drain()
    t = task(platform, task_id)
    assert t.status == "in_review"
    assert "nested git repository" in t.execution_report["error"]
    assert not marker.exists()
    assert not _pr_branches(seed)


def test_the_agent_cannot_write_into_a_nested_git_directory(tmp_path: Path) -> None:
    ws = Workspace(LocalSandbox(tmp_path), {})
    for path in ("sub/.git/config", "a/b/.git/hooks/pre-commit", ".git/config"):
        with pytest.raises(WorkspaceError, match="not editable"):
            ws.write_file(path, "x")


def test_secret_gate_reads_files_that_git_would_show_as_binary(platform, seed, drain) -> None:
    """A "-diff" attribute makes `git diff` print "Binary files differ" instead of the lines."""
    task_id = make_task(platform, seed)
    _set_repo(platform, seed, test_command="printf '*.lock -diff\\n' > .gitattributes")
    _use_agent(platform, seed, EditingAgent("deps.lock", f'key = "{FAKE_AWS_KEY}"\n'))
    approve(platform, seed, task_id)
    drain()
    assert "credential-like" in task(platform, task_id).execution_report["error"]
    assert not _pr_branches(seed)


def test_secret_scan_survives_binary_files_in_the_diff(tmp_path: Path) -> None:
    """Bytecode and images are staged with the change; their bytes are not UTF-8."""
    repo = _git_repo(tmp_path / "r")
    (repo / "cache.pyc").write_bytes(b"\xcb\x0d\x0d\x0a\x00\xff" * 50)
    (repo / "app.py").write_text(f'K = "{FAKE_AWS_KEY}"\n')
    assert ("app.py", 1, f'K = "{FAKE_AWS_KEY}"') in staged_added_lines(repo)


def test_files_named_like_the_sandbox_home_are_gated(platform, seed, drain) -> None:
    task_id = make_task(platform, seed)
    _use_agent(platform, seed, EditingAgent(".opsmith-home-certs/server.pem", "-----BEGIN\n"))
    approve(platform, seed, task_id)
    drain()
    t = task(platform, task_id)
    assert t.status == "in_review"
    assert "excluded paths" in t.execution_report["error"]
    assert not _pr_branches(seed)


def test_a_sandbox_home_replaced_by_a_link_is_never_committed(
    platform, seed, drain, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    task_id = make_task(platform, seed)
    _set_repo(
        platform, seed, test_command=f"rm -rf .opsmith-home && ln -s {elsewhere} .opsmith-home"
    )
    approve(platform, seed, task_id)
    drain()
    assert task(platform, task_id).status == "pr_open", task(platform, task_id).execution_report
    assert ".opsmith-home" not in run_git(["show", "--stat", _pr_branches(seed)], seed.repo_path)
    assert elsewhere.is_dir()  # the link was removed, not followed


def test_local_sandbox_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "survivor"
    result = LocalSandbox(tmp_path).run(f"(sleep 2; touch {marker}) & sleep 30", timeout=1)
    assert result.timed_out
    time.sleep(2.5)
    assert not marker.exists()


def test_local_sandbox_is_refused_for_remote_repositories(platform, seed, drain) -> None:
    assert sandbox_policy_error("docker", Repository(scm_provider="github")) is None
    assert sandbox_policy_error("local", Repository(scm_provider="local")) is None
    assert "sandbox policy" in (sandbox_policy_error("local", Repository(scm_provider="github")))
    task_id = make_task(platform, seed)
    _set_repo(platform, seed, scm_provider="github")
    approve(platform, seed, task_id)
    drain()
    t = task(platform, task_id)
    assert t.status == "in_review"
    assert "sandbox policy" in t.execution_report["error"]
    assert not _pr_branches(seed)


def test_missing_docker_fails_closed(platform, seed, drain, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(SandboxUnavailable):
        make_sandbox("docker", tmp_path, "python:3.12-slim")
    with pytest.raises(ValueError, match="unknown sandbox backend"):
        make_sandbox("chroot", tmp_path, "x")
    platform.settings.sandbox_backend = "docker"
    task_id = make_task(platform, seed)
    approve(platform, seed, task_id)
    drain()
    t = task(platform, task_id)
    assert t.status == "in_review"
    assert "sandbox unavailable" in t.execution_report["error"]
    assert not _pr_branches(seed)


def test_sandbox_backend_defaults_to_docker_and_is_validated(monkeypatch) -> None:
    monkeypatch.delenv("OPSMITH_SANDBOX_BACKEND", raising=False)
    assert Settings.from_env().sandbox_backend == "docker"
    with pytest.raises(ValueError, match="sandbox_backend"):
        Settings(sandbox_backend="chroot")  # type: ignore[arg-type]


def _docker_ready(image: str) -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "image", "inspect", image], capture_output=True, check=False)
    if probe.returncode == 0:
        return True
    pull = subprocess.run(["docker", "pull", "-q", image], capture_output=True, check=False)
    return pull.returncode == 0


@pytest.mark.docker
def test_docker_sandbox_really_isolates(tmp_path: Path) -> None:
    """Runs real containers (skipped where Docker is unavailable)."""
    image = "busybox:stable"
    if not _docker_ready(image):
        pytest.skip("docker (or the busybox image) is not available")
    tmp_path.chmod(0o777)  # the container user must be able to write the bind mount
    sandbox = DockerSandbox(tmp_path, image)

    assert sandbox.run("ls /sys/class/net").output.split() == ["lo"]  # loopback only
    net = sandbox.run("wget -q -T 3 -O- http://1.1.1.1 || echo NO_NETWORK")
    assert "NO_NETWORK" in net.output
    ordered = sandbox.run("echo one; echo two >&2; echo three")
    assert ordered.output.split() == ["one", "two", "three"]  # stderr stays in order

    fs = sandbox.run(
        "touch /escape 2>/dev/null && echo ROOT_WRITABLE; "
        "touch /tmp/ok && echo TMP_OK; touch /workspace/made && echo WORKSPACE_OK; "
        "ls /var/run/docker.sock 2>/dev/null && echo HAS_SOCKET; "
        "grep CapEff /proc/self/status; echo home=$HOME"
    )
    assert "ROOT_WRITABLE" not in fs.output
    assert "HAS_SOCKET" not in fs.output
    assert "TMP_OK" in fs.output
    assert "WORKSPACE_OK" in fs.output
    assert (tmp_path / "made").exists()
    assert "0000000000000000" in fs.output  # no effective capabilities, even as uid 0
    assert "home=/workspace/.opsmith-home" in fs.output

    timed = sandbox.run("sleep 60", timeout=3)
    assert timed.timed_out
    left = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"label=opsmith.sandbox={sandbox.sandbox_id}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert left.stdout.strip() == ""  # the timed-out container was killed and removed


def test_workspace_refuses_oversized_reads(tmp_path: Path) -> None:
    (tmp_path / "big.bin").write_bytes(b"x" * (6 * 1024 * 1024))
    ws = Workspace(LocalSandbox(tmp_path), {})
    with pytest.raises(ValueError, match="too large"):
        ws.read_file("big.bin")


# --------------------------------------------------------------------------- approval binding


def test_approval_covers_only_the_scope_that_was_reviewed(platform, seed, drain) -> None:
    task_id = make_task(platform, seed)
    review_id = approve(platform, seed, task_id)
    with platform.db.session() as s:
        assert s.get(Review, review_id).scope_digest
        s.get(Task, task_id).proposed_approach = "Rewrite the payment module instead."
    drain()
    t = task(platform, task_id)
    assert t.status == "in_review"  # not parked in `approved`: it goes back to a human
    assert "changed after it was approved" in t.execution_report["error"]
    assert not _pr_branches(seed)
    with platform.db.session() as s:
        assert s.scalar(select(AuditLog).where(AuditLog.action == "execution.refused"))
        assert s.scalar(select(Notification).where(Notification.event == "task.execution_failed"))


def test_finding_attached_after_approval_voids_it(platform, seed, drain) -> None:
    task_id = make_task(platform, seed)
    approve(platform, seed, task_id)
    with platform.db.session() as s:
        s.add(
            Finding(
                tenant_id=seed.tenant.id,
                project_id=seed.project.id,
                source="audit:test",
                category="security",
                title="late arrival",
                fingerprint="late",
                task_id=task_id,
                path="app/payments.py",
                line=3,
                rule_id="py.bare-except",
            )
        )
    drain()
    assert task(platform, task_id).status == "in_review"
    assert not _pr_branches(seed)


class ScopeShiftingAgent:
    """Makes a legitimate edit, while the task is changed underneath the running execution."""

    name = "scripted"

    def __init__(self, platform: Platform) -> None:
        self.platform = platform

    def run(self, task: AgentTask, ws: Workspace, budget: AgentBudget) -> AgentResult:
        ws.write_file("app/extra.py", "VALUE = 1\n")
        with self.platform.db.session() as s:
            s.get(Task, task.id).paths = ["app/", "somewhere/else.py"]
        return AgentResult(True, "edited")


def test_scope_change_during_execution_blocks_the_push(platform, seed, drain) -> None:
    task_id = make_task(platform, seed)
    _use_agent(platform, seed, ScopeShiftingAgent(platform))
    approve(platform, seed, task_id)
    drain()
    t = task(platform, task_id)
    assert t.status == "in_review"
    assert t.execution_report["error"].startswith("approval:")
    assert not _pr_branches(seed)  # refused before anything left the worker


def test_a_stale_decision_loses_to_the_one_already_recorded(platform, seed) -> None:
    """Two reviewers act on the same task: the second decision must not overwrite the first."""
    task_id = make_task(platform, seed)
    second = platform.db.new_session()
    try:
        stale = second.get(Task, task_id)
        assert stale.status == "in_review"
        approve(platform, seed, task_id)  # the first reviewer's approval commits
        with pytest.raises(ReviewError, match="approved"):
            platform.reviews.review(
                second,
                stale,
                second.get(User, seed.admin.id),
                ReviewRequest(decision="reject", reason_code="wont_fix"),
            )
    finally:
        second.close()
    with platform.db.session() as s:
        assert [r.decision for r in s.scalars(select(Review))] == ["approve"]
        assert s.get(Task, task_id).status == "approved"


@pytest.mark.parametrize(
    ("edits", "message"),
    [
        ({"title": 123}, "title"),
        ({"title": "   "}, "title"),
        ({"title": "x" * 301}, "title"),
        ({"paths": ["../outside.py"]}, "relative"),
        ({"paths": ["/etc/passwd"]}, "relative"),
        ({"paths": "app/"}, "list"),
        ({"acceptance_criteria": []}, "empty"),
        ({"branch_name": "main"}, "branch_name"),
        ({"branch_name": "opsmith/../main"}, "branch_name"),
        ({"branch_name": "opsmith/x y"}, "branch_name"),
        ({"branch_name": "opsmith/x.lock"}, "branch_name"),
    ],
)
def test_review_edits_are_validated(platform, seed, edits, message) -> None:
    task_id = make_task(platform, seed)
    with platform.db.session() as s, pytest.raises(ReviewError, match=message):
        platform.reviews.review(
            s,
            s.get(Task, task_id),
            s.get(User, seed.reviewer.id),
            ReviewRequest(decision="approve_with_edits", edits=edits),
        )
    assert task(platform, task_id).status == "in_review"


def test_pushes_stay_inside_the_opsmith_branch_namespace() -> None:
    repo = Repository(default_branch="main")
    check_pushable(repo, "opsmith/fix/timeout-abc123")
    for branch in ("main", "release/1.0", "feature"):
        with pytest.raises(SCMError):
            check_pushable(repo, branch)
    assert valid_branch_name("opsmith/fix/timeout-abc123")
    assert not valid_branch_name("opsmith/")
    assert not valid_branch_name("opsmith/-x")


# --------------------------------------------------------------------------- queue


def _job(platform: Platform, seed: Any, key: str, **fields: Any) -> str:
    with platform.db.session() as s:
        job = queue.enqueue(s, tenant_id=seed.tenant.id, kind="t", payload={}, idempotency_key=key)
        for name, value in fields.items():
            setattr(job, name, value)
        return job.id


def test_job_that_keeps_killing_its_worker_is_dead_lettered(platform, seed) -> None:
    job_id = _job(
        platform,
        seed,
        "crashy",
        status="running",
        attempts=5,
        max_attempts=5,
        locked_by="worker-gone",
        lease_expires_at=utcnow() - timedelta(minutes=1),
    )
    worker = Worker(platform.db, {"t": lambda _s, _j: {}}, kinds={"t"}, on_dead=platform._on_dead)
    assert worker.recover_expired_leases() == 1
    with platform.db.session() as s:
        job = s.get(Job, job_id)
        assert job.status == "dead"
        assert s.scalar(select(AuditLog).where(AuditLog.action == "job.dead"))
        assert s.scalar(select(Notification).where(Notification.event == "job.dead"))
    assert worker.run_once() is None  # it is not handed out again


def test_slow_job_keeps_its_lease_through_the_heartbeat(platform, seed) -> None:
    job_id = _job(platform, seed, "slow")
    other = Worker(platform.db, {"t": lambda _s, _j: {}})
    seen: dict[str, Any] = {}

    def slow(_s, _job):
        time.sleep(2.5)  # longer than the lease below
        seen["recovered_by_other_worker"] = other.recover_expired_leases()
        return {"ok": True}

    worker = Worker(platform.db, {"t": slow}, lease_seconds=1, heartbeat_seconds=0.2)
    assert worker.run_once().status == "succeeded"
    assert seen["recovered_by_other_worker"] == 0
    with platform.db.session() as s:
        assert s.get(Job, job_id).attempts == 1


def test_worker_that_lost_its_lease_does_not_overwrite_the_new_owner(platform, seed) -> None:
    job_id = _job(platform, seed, "taken")

    def handler(_s, _job):
        with platform.db.session() as s:  # another worker took the job over meanwhile
            s.get(Job, job_id).locked_by = "worker-other"
        return {"ok": True}

    result = Worker(platform.db, {"t": handler}).run_once()
    assert result.status == "lost_lease"
    with platform.db.session() as s:
        job = s.get(Job, job_id)
        assert job.status == "running"
        assert job.locked_by == "worker-other"


# --------------------------------------------------------------------------- scheduler


def test_one_broken_schedule_does_not_stop_the_others(platform, seed) -> None:
    with platform.db.session() as s:
        broken = Schedule(
            tenant_id=seed.tenant.id,
            project_id=seed.project.id,
            job_kind="track_prs",
            target_id="broken",
            interval_seconds=None,
            cron=None,
            next_run_at=utcnow() - timedelta(minutes=5),
        )
        s.add(broken)
        good = upsert_schedule(
            s,
            tenant_id=seed.tenant.id,
            project_id=seed.project.id,
            job_kind="track_prs",
            target_id="good",
            interval_seconds=600,
            payload={"project_id": seed.project.id},
            first_run_at=utcnow() - timedelta(minutes=5),
        )
        s.flush()
        broken_id, good_id = broken.id, good.id
    platform.scheduler.tick()
    with platform.db.session() as s:
        assert s.get(Schedule, broken_id).enabled is False
        assert s.scalar(select(AuditLog).where(AuditLog.action == "schedule.disabled"))
        assert s.scalar(select(Job).where(Job.idempotency_key.like(f"sched:{good_id}:%")))
    assert platform.scheduler.tick() == []  # and the next tick is clean


def test_impossible_or_malformed_cron_is_rejected_up_front() -> None:
    for bad in ("0 0 31 2 *", "0 0 30 2 *", "a b c d e", "*/0 * * * *", "61 * * * *"):
        with pytest.raises(ValueError, match="cron"):
            CronExpr.parse(bad)
    leap = CronExpr.parse("0 0 29 2 *").next_after(utcnow())
    assert (leap.month, leap.day) == (2, 29)


@pytest.mark.parametrize(
    "bad",
    [
        {"audit_cron": "0 0 31 2 *"},
        {"audit_cron": "nonsense"},
        {"pr_poll_seconds": 0},
        {"ingest_interval_seconds": 5},
        {"inference_interval_hours": 0},
        {"lead_max_carry": 0},
        {"min_task_confidence": 1.5},
        {"budgets": {"max_concurrent_executions": 0}},
        {"budgets": {"max_tokens_per_run": -1}},
        {"budgets": {"typo_field": 1}},
        {"redaction_extra_patterns": ["("]},
        {"policy_rules": [{"id": "r", "description": "d", "forbid_regex": "["}]},
        {"notifications": [{"type": "webhook"}]},
        {"notifications": [{"type": "webhook", "url": "file:///etc/passwd"}]},
        {"protected_paths": [{"pattern": ""}]},
    ],
)
def test_project_settings_reject_values_that_would_break_jobs(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ProjectSettings.model_validate(bad)


def test_changed_timing_applies_immediately_and_custom_timing_survives(platform, seed) -> None:
    with platform.db.session() as s:
        project = s.get(Project, seed.project.id)
        audit = s.scalar(select(Schedule).where(Schedule.job_kind == "audit"))
        poll = s.scalar(select(Schedule).where(Schedule.job_kind == "track_prs"))
        audit.next_run_at = utcnow() + timedelta(days=300)  # stale slot from the old cron
        poll.interval_seconds, poll.custom = 7200, True  # an admin set this directly
        project.settings = {**project.settings, "audit_cron": "15 3 * * *", "pr_poll_seconds": 60}
        ensure_project_schedules(s, project)
        assert audit.cron == "15 3 * * *"
        assert audit.next_run_at < utcnow() + timedelta(days=2)
        assert (audit.next_run_at.hour, audit.next_run_at.minute) == (3, 15)
        assert poll.interval_seconds == 7200


def test_missed_interval_slots_coalesce_in_constant_time() -> None:
    start = utcnow() - timedelta(days=3650)
    sch = Schedule(interval_seconds=60, next_run_at=start)
    now = utcnow()
    began = time.perf_counter()
    nxt = compute_next(sch, now)
    assert time.perf_counter() - began < 0.05
    assert now < nxt <= now + timedelta(seconds=60)
    assert (nxt - start).total_seconds() % 60 == 0  # still on the schedule's own grid


# --------------------------------------------------------------------------- API and roles


@pytest.fixture
def client(platform: Platform) -> TestClient:
    return TestClient(create_app(platform))


def _h(seed: Any, role: str = "admin") -> dict[str, str]:
    return {"Authorization": f"Bearer {seed.keys[role]}"}


def test_service_keys_can_push_but_not_read(client, seed) -> None:
    pid = seed.project.id
    svc = _h(seed, "service")
    assert client.get("/api/v1/me", headers=svc).json()["role"] == "service"
    for path in (
        "/api/v1/projects",
        f"/api/v1/projects/{pid}",
        f"/api/v1/projects/{pid}/tasks",
        f"/api/v1/projects/{pid}/connectors",
        "/api/v1/jobs",
        "/api/v1/audit-log",
        "/api/v1/catalog",
    ):
        assert client.get(path, headers=svc).status_code == 403, path
    login = client.post("/ui/login", data={"api_key": seed.keys["service"]}, follow_redirects=False)
    assert "error" in login.headers["location"]
    client.cookies.set("opsmith_key", seed.keys["service"])
    assert client.get("/ui", follow_redirects=False).headers["location"] == "/ui/login"


def test_configuration_input_is_validated(client, seed) -> None:
    pid = seed.project.id
    repos = f"/api/v1/projects/{pid}/repositories"
    body = {"name": "r2", "url": "https://github.com/acme/r2.git"}
    for ref in ("env:OPSMITH_DATABASE_URL", "env:ANTHROPIC_API_KEY", "file:/etc/passwd", "vault:x"):
        resp = client.post(repos, json={**body, "credentials_ref": ref}, headers=_h(seed))
        assert resp.status_code == 422, ref
    assert client.post(repos, json=body, headers=_h(seed)).status_code == 201
    assert client.post(repos, json=body, headers=_h(seed)).status_code == 409  # not a 500
    assert client.post(repos, json={**body, "name": ""}, headers=_h(seed)).status_code == 422
    for bad in ({"audit_cron": "0 0 31 2 *"}, {"pr_poll_seconds": 0}):
        resp = client.patch(
            f"/api/v1/projects/{pid}/settings", json={"settings": bad}, headers=_h(seed)
        )
        assert resp.status_code == 422, bad
    for limit in ("0", "-1", "100000"):
        resp = client.get(f"/api/v1/projects/{pid}/tasks?limit={limit}", headers=_h(seed))
        assert resp.status_code == 422, limit


def test_objects_cannot_be_linked_or_triggered_across_projects(client, seed) -> None:
    other = client.post("/api/v1/projects", json={"name": "other"}, headers=_h(seed)).json()["id"]
    link = client.post(
        f"/api/v1/projects/{other}/services",
        json={"name": "svc", "repository_id": seed.repo.id},
        headers=_h(seed),
    )
    assert link.status_code == 422
    conn = client.post(
        f"/api/v1/projects/{other}/connectors",
        json={"name": "c", "type": "file_logs", "service_id": seed.service.id},
        headers=_h(seed),
    )
    assert conn.status_code == 422
    trig = client.post(
        f"/api/v1/projects/{other}/trigger",
        json={"job": "audit", "target_id": seed.repo.id},
        headers=_h(seed),
    )
    assert trig.status_code == 404


def test_schedule_update_is_audited_and_survives_settings_changes(client, seed) -> None:
    pid = seed.project.id
    schedules = client.get(f"/api/v1/projects/{pid}/schedules", headers=_h(seed)).json()
    audit = next(s for s in schedules if s["job_kind"] == "audit")
    both = client.patch(
        f"/api/v1/schedules/{audit['id']}",
        json={"cron": "0 4 * * 1", "interval_seconds": 600},
        headers=_h(seed),
    )
    assert both.status_code == 422
    upd = client.patch(
        f"/api/v1/schedules/{audit['id']}", json={"cron": "0 4 * * 1"}, headers=_h(seed)
    )
    assert upd.json()["custom"] is True
    assert upd.json()["next_run_at"][11:16] == "04:00"
    client.patch(
        f"/api/v1/projects/{pid}/settings",
        json={"settings": {"audit_cron": "0 5 * * *"}},
        headers=_h(seed),
    )
    schedules = client.get(f"/api/v1/projects/{pid}/schedules", headers=_h(seed)).json()
    assert next(s for s in schedules if s["job_kind"] == "audit")["cron"] == "0 4 * * 1"
    log = client.get(f"/api/v1/audit-log?entity_id={audit['id']}", headers=_h(seed)).json()
    assert log[0]["action"] == "schedule.updated"


def test_cross_tenant_ids_are_indistinguishable_from_missing(client, platform, seed) -> None:
    task_id = make_task(platform, seed)
    with platform.db.session() as s:
        _, key = create_user(s, create_tenant(s, "rival"), "Mallory", "admin")
    rival = {"Authorization": f"Bearer {key}"}
    probes = [
        ("get", f"/api/v1/tasks/{task_id}", None),
        ("get", f"/api/v1/tasks/{task_id}/provenance", None),
        ("post", f"/api/v1/tasks/{task_id}/review", {"decision": "approve"}),
        ("post", f"/api/v1/tasks/{task_id}/resubmit", None),
        ("get", f"/api/v1/projects/{seed.project.id}/tasks", None),
        ("patch", f"/api/v1/projects/{seed.project.id}/settings", {"settings": {}}),
        ("post", f"/api/v1/connectors/{seed.log_connector.id}/push", []),
        ("post", f"/api/v1/projects/{seed.project.id}/trigger", {"job": "aggregate"}),
    ]
    for method, path, body in probes:
        resp = client.request(method, path, json=body, headers=rival)
        assert resp.status_code == 404, (method, path)
    assert client.get("/api/v1/jobs", headers=rival).json() == []
    assert client.get("/api/v1/audit-log", headers=rival).json()[0]["action"] == "user.created"
    assert task(platform, task_id).status == "in_review"


# --------------------------------------------------------------------------- credentials


def test_tenant_credential_references_cannot_reach_platform_secrets(
    monkeypatch, tmp_path: Path
) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "token").write_text("s3cret\n")
    (tmp_path / "elsewhere").write_text("private\n")
    monkeypatch.setenv("OPSMITH_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv("OPSMITH_DATABASE_URL", "postgresql://u:pw@db/x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-platform")
    monkeypatch.setenv("LOKI_TOKEN", "tenant-token")

    assert resolve_tenant_credential("env:LOKI_TOKEN") == "tenant-token"
    assert resolve_tenant_credential(f"file:{secrets_dir / 'token'}") == "s3cret"
    assert resolve_tenant_credential(None) is None
    for ref in (
        "env:OPSMITH_DATABASE_URL",
        "env:opsmith_database_url",
        "env:ANTHROPIC_API_KEY",
        "env:POSTGRES_PASSWORD",
        "env:BAD NAME",
        f"file:{tmp_path / 'elsewhere'}",
        f"file:{secrets_dir}/../elsewhere",
        "file:/proc/self/environ",
        "http://example.com/secret",
    ):
        with pytest.raises(CredentialRefError):
            resolve_tenant_credential(ref)
    # Docker secrets keep the LLM key next to tenants' tokens: that file stays reserved,
    # also through a link to it.
    (secrets_dir / "anthropic_key").write_text("sk-ant-platform\n")
    (secrets_dir / "innocent").symlink_to(secrets_dir / "anthropic_key")
    monkeypatch.setenv("OPSMITH_ANTHROPIC_API_KEY_REF", f"file:{secrets_dir / 'anthropic_key'}")
    for ref in (f"file:{secrets_dir / 'anthropic_key'}", f"file:{secrets_dir / 'innocent'}"):
        with pytest.raises(CredentialRefError, match="reserved"):
            resolve_tenant_credential(ref)
    assert resolve_tenant_credential(f"file:{secrets_dir / 'token'}") == "s3cret"
    # The operator can move the LLM key; whatever variable holds it stays reserved.
    monkeypatch.setenv("OPSMITH_ANTHROPIC_API_KEY_REF", "env:MY_LLM_KEY")
    with pytest.raises(CredentialRefError):
        check_credentials_ref("env:MY_LLM_KEY")
    check_credentials_ref("env:ANTHROPIC_API_KEY")


@pytest.mark.docker
def test_full_execution_through_the_docker_sandbox(platform, seed, drain) -> None:
    """The real path end to end: agent edit, checks inside a network-less container, gates,
    commit from the trusted snapshot, PR. Skipped where Docker is unavailable."""
    image = "python:3.12-slim"
    if not _docker_ready(image):
        pytest.skip("docker (or the python image) is not available")
    platform.settings.sandbox_backend = "docker"
    platform.settings.sandbox_image = image
    # The sandbox has no network, so checks can only use what the image already contains.
    # The demo repository needs httpx, which a bare Python image lacks; compile instead.
    _set_repo(
        platform,
        seed,
        test_command="python -m py_compile app/payments.py && grep -q 'timeout=10' app/payments.py",
    )
    task_id = make_task(platform, seed)
    approve(platform, seed, task_id)
    drain()
    t = task(platform, task_id)
    assert t.status == "pr_open", t.execution_report
    with platform.db.session() as s:
        run = s.scalars(select(ExecutionRun)).one()
        assert run.sandbox_id.startswith("docker-")
        assert run.checks[0]["exit_code"] == 0
    shown = run_git(["show", "--stat", "--format=%an", _pr_branches(seed)], seed.repo_path)
    assert "Opsmith Bot" in shown
    assert ".opsmith-home" not in shown


def test_disabled_connector_refuses_pushes(client, platform, seed) -> None:
    with platform.db.session() as s:
        s.get(type(seed.log_connector), seed.log_connector.id).enabled = False
    record = {"timestamp": utcnow().isoformat(), "service": "payments-api", "message": "x"}
    resp = client.post(
        f"/api/v1/connectors/{seed.log_connector.id}/push",
        json=[record],
        headers=_h(seed, "service"),
    )
    assert resp.status_code == 409


def test_long_metric_series_keep_a_bounded_stable_signal() -> None:
    short = metric_signal("latency_ms{route=/pay}")
    assert short == "metric:latency_ms{route=/pay}"
    labels = ",".join(f"label{i}=value-{i:04d}" for i in range(40))
    one, two = metric_signal(f"latency_ms{{{labels}}}"), metric_signal(f"latency_ms{{{labels}x}}")
    assert len(one) == len(two) == 200
    assert one != two  # distinct series stay distinct after bounding
    assert one == metric_signal(f"latency_ms{{{labels}}}")  # and stable across runs


# --------------------------------------------------------------------------- executor budgets


class SpendingAgent:
    """Stands in for a model-driven agent: it edits one file and reports what it spent."""

    name = "scripted"
    provider = "openai"
    model = "some-model"

    def __init__(self, cost: float = 0.25) -> None:
        self.cost = cost
        self.budgets: list[AgentBudget] = []

    def run(self, task: AgentTask, ws: Workspace, budget: AgentBudget) -> AgentResult:
        self.budgets.append(budget)
        ws.write_file("app/extra.py", "VALUE = 1\n")
        return AgentResult(True, "edited", [], 1200, 300, self.cost)


def _budgets(platform: Platform, seed: Any, **budgets: Any) -> None:
    with platform.db.session() as s:
        project = s.get(Project, seed.project.id)
        project.settings = {**project.settings, "budgets": budgets}


def test_coding_agent_spending_is_recorded_and_counts_toward_the_daily_budget(
    platform, seed, drain
) -> None:
    agent = SpendingAgent(cost=0.25)
    _use_agent(platform, seed, agent)
    _budgets(platform, seed, max_cost_per_run_usd=2.0, max_cost_per_day_usd=10.0)
    task_id = make_task(platform, seed)
    approve(platform, seed, task_id)
    drain()
    assert task(platform, task_id).status == "pr_open"
    with platform.db.session() as s:
        [usage] = s.scalars(select(UsageRecord).where(UsageRecord.stage == "executor")).all()
        run = s.scalars(select(ExecutionRun)).one()
        assert (usage.provider, usage.model, usage.run_id) == ("openai", "some-model", run.id)
        assert (usage.input_tokens, usage.output_tokens, usage.cost_usd) == (1200, 300, 0.25)
        project = s.get(Project, seed.project.id)
        assert BudgetGuard(s, project).spent_today() == 0.25  # what the daily cap reads
    # The agent was told what it may spend: the per-run cap, since more than that is left today.
    assert agent.budgets[0].max_cost_usd == 2.0


def test_an_exhausted_daily_budget_stops_the_execution_before_any_model_call(
    platform, seed, drain
) -> None:
    agent = SpendingAgent()
    _use_agent(platform, seed, agent)
    _budgets(platform, seed, max_cost_per_day_usd=1.0)
    with platform.db.session() as s:
        s.add(
            UsageRecord(
                tenant_id=seed.tenant.id,
                project_id=seed.project.id,
                stage="inference",
                provider="openai",
                model="m",
                cost_usd=1.0,
            )
        )
    task_id = make_task(platform, seed)
    approve(platform, seed, task_id)
    drain()
    t = task(platform, task_id)
    assert t.status == "in_review"
    assert t.execution_report["error"].startswith("budget:")
    assert agent.budgets == []  # never started
    assert not _pr_branches(seed)


def test_the_agent_may_only_spend_what_is_left_of_the_day(platform, seed, drain) -> None:
    agent = SpendingAgent()
    _use_agent(platform, seed, agent)
    _budgets(platform, seed, max_cost_per_run_usd=5.0, max_cost_per_day_usd=1.0)
    with platform.db.session() as s:
        s.add(
            UsageRecord(
                tenant_id=seed.tenant.id,
                project_id=seed.project.id,
                stage="task",
                provider="openai",
                model="m",
                cost_usd=0.75,
            )
        )
    approve(platform, seed, make_task(platform, seed))
    drain()
    assert agent.budgets[0].max_cost_usd == pytest.approx(0.25)


def test_offline_agent_is_not_limited_by_cost_budgets(platform, seed, drain) -> None:
    """The heuristic agent makes no model call, so an exhausted budget must not block it."""
    _budgets(platform, seed, max_cost_per_day_usd=0.0)
    task_id = make_task(platform, seed)
    approve(platform, seed, task_id)
    drain()
    assert task(platform, task_id).status == "pr_open"
    with platform.db.session() as s:
        assert s.scalars(select(UsageRecord).where(UsageRecord.stage == "executor")).all() == []


def test_unknown_executor_agent_fails_with_a_clear_report(platform, seed, drain) -> None:
    with platform.db.session() as s:
        project = s.get(Project, seed.project.id)
        project.settings = {**project.settings, "executor_agent": "gpt"}
    task_id = make_task(platform, seed)
    approve(platform, seed, task_id)
    drain()
    report = task(platform, task_id).execution_report["error"]
    assert "unknown executor_agent 'gpt'" in report
    assert "heuristic" in report


def test_agent_budget_stops_on_tokens_or_cost() -> None:
    budget = AgentBudget(max_tokens=1000, max_cost_usd=0.5)
    assert budget.exhausted(999, 0.49) is None
    assert budget.exhausted(1000, 0.0) == "Token budget exhausted."
    assert budget.exhausted(10, 0.5) == "Cost budget exhausted."
    assert AgentBudget().exhausted(10, 1_000_000.0) is None  # no cost cap unless one is given
