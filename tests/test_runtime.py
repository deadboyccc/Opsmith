"""The parts that only run in a deployed process: the long-running command loops, logging,
the object store, the Docker sandbox's failure paths and the ``alembic`` command. Each test
drives the real code with the one external thing (a server, a signal, the docker CLI) replaced.
"""

from __future__ import annotations

import json
import logging
import os
import runpy
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import create_engine, inspect, select

from opsmith import cli, queue
from opsmith.config import Settings, resolve_credential
from opsmith.executor import sandbox as sandbox_module
from opsmith.executor.sandbox import (
    DockerSandbox,
    LocalSandbox,
    SandboxUnavailable,
    Workspace,
    WorkspaceError,
    docker_daemon_error,
    make_sandbox,
)
from opsmith.llm import default_gateway
from opsmith.migrations import SchemaError
from opsmith.models import Job, Notification, Project
from opsmith.notifications import deliver, handle_notify_job, notify
from opsmith.observability import JsonFormatter, configure_logging
from opsmith.storage import LocalObjectStore

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path: Path, monkeypatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("OPSMITH_DATA_DIR", str(data))
    monkeypatch.setenv("OPSMITH_LOG_JSON", "false")
    for name in ("OPSMITH_DATABASE_URL", "OPSMITH_SANDBOX_BACKEND", "OPSMITH_LOCAL_ROOT"):
        monkeypatch.delenv(name, raising=False)
    cli._STOP.clear()
    yield data
    cli._STOP.clear()


# --------------------------------------------------------------------------- command loops


def test_api_command_serves_the_application(env: Path, monkeypatch) -> None:
    served: dict[str, Any] = {}

    def fake_run(app: Any, **options: Any) -> None:
        served.update(options, app=app)

    monkeypatch.setattr("uvicorn.run", fake_run)
    assert cli.main(["api", "--host", "127.0.0.1", "--port", "8123"]) == 0
    assert isinstance(served["app"], FastAPI)
    assert (served["host"], served["port"]) == ("127.0.0.1", 8123)
    assert served["log_config"] is None  # Opsmith's own log format stays in place


def test_run_command_starts_the_loops_and_stops_them_when_the_server_exits(
    env: Path, monkeypatch
) -> None:
    started: list[str] = []
    monkeypatch.setattr(cli, "_scheduler_loop", lambda *_a: started.append("scheduler"))
    monkeypatch.setattr(cli, "_worker_loop", lambda *_a: started.append("worker"))
    monkeypatch.setattr("uvicorn.run", lambda _app, **_options: None)
    assert cli.main(["run"]) == 0
    assert cli._STOP.is_set()
    assert sorted(started) == ["scheduler", "worker"]


def test_worker_loop_survives_an_error_and_keeps_working(env: Path, monkeypatch, caplog) -> None:
    """A database that is briefly unreachable must not end the worker for good."""
    calls: dict[str, Any] = {"recover": 0, "waits": []}
    real_recover = queue.Worker.recover_expired_leases

    def flaky_recover(self: queue.Worker) -> int:
        calls["recover"] += 1
        if calls["recover"] == 1:
            raise RuntimeError("database is gone")
        return real_recover(self)

    def wait(seconds: float) -> bool:
        calls["waits"].append(seconds)
        if len(calls["waits"]) == 2:
            cli._STOP.set()
        return cli._STOP.is_set()

    monkeypatch.setattr(queue.Worker, "recover_expired_leases", flaky_recover)
    monkeypatch.setattr(cli._STOP, "wait", wait)
    with caplog.at_level(logging.ERROR, logger="opsmith.cli"):
        cli._worker_loop(Settings.from_env(), poll=0.5, once=False)
    assert "worker loop error; retrying" in caplog.text
    assert calls["waits"] == [cli._ERROR_BACKOFF_SECONDS, 0.5]  # backed off, then polled
    calls["recover"] = 0
    cli._STOP.clear()
    with pytest.raises(RuntimeError, match="database is gone"):  # --once reports it instead
        cli._worker_loop(Settings.from_env(), poll=0.5, once=True)


def test_scheduler_loop_survives_an_error(env: Path, monkeypatch, caplog) -> None:
    from opsmith.scheduler import Scheduler  # noqa: PLC0415

    ticks = {"n": 0}

    def tick(self: Scheduler, now: Any = None) -> list[str]:
        ticks["n"] += 1
        if ticks["n"] == 1:
            raise RuntimeError("database is gone")
        return ["job_1"]

    def wait(_seconds: float) -> bool:
        if ticks["n"] >= 2:
            cli._STOP.set()
        return cli._STOP.is_set()

    monkeypatch.setattr(Scheduler, "tick", tick)
    monkeypatch.setattr(cli._STOP, "wait", wait)
    with caplog.at_level(logging.INFO, logger="opsmith.cli"):
        cli._scheduler_loop(Settings.from_env(), interval=1.0, once=False)
    assert "scheduler tick failed; retrying" in caplog.text
    assert "scheduled jobs" in caplog.text
    ticks["n"] = 0
    cli._STOP.clear()
    with pytest.raises(RuntimeError):
        cli._scheduler_loop(Settings.from_env(), interval=1.0, once=True)


def test_a_stop_signal_ends_the_loops() -> None:
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    cli._STOP.clear()
    try:
        cli._install_signal_handlers()
        os.kill(os.getpid(), signal.SIGTERM)
        assert cli._STOP.wait(5)
    finally:
        for sig, handler in before.items():
            signal.signal(sig, handler)
        cli._STOP.clear()


def test_a_schema_problem_is_a_message_not_a_traceback(env: Path, monkeypatch) -> None:
    def refuse(*_args: Any) -> None:
        raise SchemaError("the database was migrated by a newer release")

    monkeypatch.setattr(cli, "_scheduler_loop", refuse)
    with pytest.raises(SystemExit, match="database schema: the database was migrated"):
        cli.main(["scheduler", "--once"])


def test_demo_command_reports_a_verified_task(env: Path, tmp_path: Path, capsys) -> None:
    assert cli.main(["demo", "--dir", str(tmp_path / "demo")]) == 0
    out = capsys.readouterr().out
    assert '"final_status": "verified"' in out
    assert "sign in at http://127.0.0.1:8000/ui" in out


def test_the_module_is_runnable(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["opsmith", "--version"])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("opsmith", run_name="__main__")
    assert stopped.value.code == 0
    assert "opsmith" in capsys.readouterr().out


# --------------------------------------------------------------------------- logging


def test_json_logs_carry_extras_and_exceptions() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.getLogger("opsmith.test").makeRecord(
            "opsmith.test", logging.ERROR, __file__, 1, "job %s failed", ("j1",), sys.exc_info()
        )
    record.job_id = "job_1"
    record._private = "left out"
    line = json.loads(JsonFormatter().format(record))
    assert (line["level"], line["logger"], line["msg"]) == (
        "error",
        "opsmith.test",
        "job j1 failed",
    )
    assert line["job_id"] == "job_1"
    assert "_private" not in line
    assert "ValueError: boom" in line["exc"]


def test_configure_logging_replaces_the_handlers() -> None:
    root = logging.getLogger()
    before, level = root.handlers[:], root.level
    try:
        configure_logging("debug", json_logs=True)
        assert root.level == logging.DEBUG
        assert [type(h.formatter) for h in root.handlers] == [JsonFormatter]
        configure_logging("WARNING", json_logs=False)
        assert root.level == logging.WARNING
        assert not isinstance(root.handlers[0].formatter, JsonFormatter)
        assert logging.getLogger("alembic").level == logging.WARNING
    finally:
        root.handlers[:] = before
        root.setLevel(level)


# --------------------------------------------------------------------------- object store


def test_object_store_round_trip_and_confinement(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    assert store.put_jsonl("t/p/logs/a.jsonl", [{"a": 1}, {"b": "x"}]) == 2
    assert list(store.iter_jsonl("t/p/logs/a.jsonl")) == [{"a": 1}, {"b": "x"}]
    store.put_text("t/notes.txt", "hello")
    assert store.get_text("t/notes.txt") == "hello"
    for key in ("../outside.jsonl", "/etc/passwd", "t/../../x"):
        with pytest.raises(ValueError, match="escapes the store"):
            store.put_text(key, "x")


def test_a_failed_write_leaves_nothing_behind(tmp_path: Path) -> None:
    """A write that failed half-way left its ``.tmp`` file in the store for good."""
    store = LocalObjectStore(tmp_path / "objects")

    def records() -> Any:
        yield {"ok": 1}
        raise RuntimeError("source went away")

    with pytest.raises(RuntimeError, match="source went away"):
        store.put_jsonl("t/a.jsonl", records())
    assert not list((tmp_path / "objects").rglob("*.tmp"))
    assert not (tmp_path / "objects" / "t" / "a.jsonl").exists()


# --------------------------------------------------------------------------- sandbox


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> Any:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_docker_that_cannot_start_a_container_is_not_a_failed_check(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        sandbox_module.subprocess,
        "run",
        lambda *_a, **_k: _completed(125, "docker: Cannot connect to the Docker daemon"),
    )
    with pytest.raises(SandboxUnavailable, match="Cannot connect"):
        DockerSandbox(tmp_path, "img").run("pytest")


def test_a_docker_timeout_kills_the_container_by_name(tmp_path: Path, monkeypatch) -> None:
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **options: Any) -> Any:
        commands.append(cmd)
        if cmd[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(cmd, options["timeout"], output=b"partial output")
        return _completed(0)

    monkeypatch.setattr(sandbox_module.subprocess, "run", fake_run)
    box = DockerSandbox(tmp_path, "img")
    result = box.run("sleep 999", timeout=1)
    assert (result.exit_code, result.timed_out, result.output) == (124, True, "partial output")
    assert not result.ok
    name = commands[0][commands[0].index("--name") + 1]
    assert commands[1] == ["docker", "kill", name]
    assert name.startswith(f"opsmith-{box.sandbox_id}-")


def test_docker_daemon_problems_are_named(monkeypatch) -> None:
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda _name: None)
    assert "not on the worker's PATH" in (docker_daemon_error() or "")
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        sandbox_module.subprocess,
        "run",
        lambda *_a, **_k: _completed(1, stderr="permission denied"),
    )
    assert "cannot reach the Docker daemon: permission denied" in (docker_daemon_error() or "")

    def broken(*_a: Any, **_k: Any) -> Any:
        raise OSError("exec format error")

    monkeypatch.setattr(sandbox_module.subprocess, "run", broken)
    assert "the docker CLI failed" in (docker_daemon_error() or "")
    monkeypatch.setattr(sandbox_module.subprocess, "run", lambda *_a, **_k: _completed(0, "29.0"))
    assert docker_daemon_error() is None
    assert isinstance(make_sandbox("docker", Path("."), "img"), DockerSandbox)


def test_an_unknown_sandbox_backend_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown sandbox backend"):
        make_sandbox("chroot", tmp_path, "img")


def test_workspace_listing_and_limits(tmp_path: Path) -> None:
    for rel in ("a.py", "pkg/b.py", ".git/config", ".opsmith-home/x", "pkg/sub/.git/HEAD"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    ws = Workspace(LocalSandbox(tmp_path), {"test": "true", "lint": ""})
    assert ws.list_files() == ["a.py", "pkg/b.py"]  # git metadata and the sandbox home never
    assert ws.list_files("pkg") == ["pkg/b.py"]
    assert ws.list_files(limit=1) == ["a.py"]
    assert sorted(ws.checks) == ["test"]  # a check that is not configured is not offered
    with pytest.raises(WorkspaceError, match="no such file"):
        ws.read_file("missing.py")
    with pytest.raises(WorkspaceError, match="file too large"):
        ws.write_file("big.txt", "x" * (sandbox_module.MAX_WRITE_BYTES + 1))
    with pytest.raises(WorkspaceError, match="unknown check"):
        ws.run_check("deploy")
    with pytest.raises(WorkspaceError, match="expected exactly one match"):
        ws.replace_in_file("a.py", "not there", "y")
    assert ws.read_file("a.py", max_chars=0).endswith("...[truncated]")
    assert ws.run_check("test").ok
    assert len(ws.check_log) == 1


# --------------------------------------------------------------------------- plugins


def test_a_broken_provider_plugin_does_not_take_the_platform_down(monkeypatch, caplog) -> None:
    from opsmith import llm  # noqa: PLC0415
    from opsmith.llm import HeuristicProvider  # noqa: PLC0415

    def broken() -> Any:
        raise ImportError("missing dependency")

    plugins = [
        SimpleNamespace(name="acme", load=lambda: lambda _settings: HeuristicProvider()),
        SimpleNamespace(name="broken", load=broken),
        SimpleNamespace(name="heuristic", load=lambda: lambda _settings: "an impostor"),
    ]
    monkeypatch.setattr(llm, "entry_points", lambda group: plugins)
    with caplog.at_level(logging.ERROR, logger="opsmith.llm"):
        gateway = default_gateway(Settings(sandbox_backend="local"))
    assert "acme" in gateway.providers
    assert "broken" not in gateway.providers
    assert isinstance(gateway.providers["heuristic"], HeuristicProvider)  # built-ins win
    assert "failed to load LLM provider plugin" in caplog.text


def test_credential_references(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SOME_TOKEN", "from-env")
    secret = tmp_path / "token"
    secret.write_text("from-file\n")
    assert resolve_credential("env:SOME_TOKEN") == "from-env"
    assert resolve_credential("env:NOT_SET_ANYWHERE") is None
    assert resolve_credential(f"file:{secret}") == "from-file"
    assert resolve_credential(f"file:{tmp_path / 'missing'}") is None
    assert resolve_credential(None) is None
    with pytest.raises(ValueError, match="unsupported credentials reference scheme"):
        resolve_credential("vault:secret/x")


# --------------------------------------------------------------------------- notifications


def test_a_notification_that_vanished_is_a_permanent_failure(platform, seed) -> None:
    with platform.db.session() as s:
        job = Job(tenant_id=seed.tenant.id, kind="notify", idempotency_key="n", payload={})
        job.payload = {"notification_id": "ntf_missing"}
        with pytest.raises(queue.PermanentFailure, match="vanished"):
            handle_notify_job(s, job)


def test_notifications_without_a_project_are_logged(platform, seed, caplog) -> None:
    with platform.db.session() as s:
        orphan = notify(s, tenant_id=seed.tenant.id, project_id=None, event="job.dead", subject="x")
        gone = notify(
            s, tenant_id=seed.tenant.id, project_id="prj_deleted", event="job.dead", subject="y"
        )
        with caplog.at_level(logging.INFO, logger="opsmith.notifications"):
            assert deliver(s, orphan) == [{"channel": "log", "ok": True}]
            assert deliver(s, gone) == [{"channel": "log", "ok": True}]
        assert (orphan.status, gone.status) == ("sent", "sent")
    assert caplog.text.count("notification") >= 2


def test_a_webhook_that_cannot_be_reached_is_retried_by_the_queue(platform, seed) -> None:
    import httpx  # noqa: PLC0415

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with platform.db.session() as s:
        project = s.get(Project, seed.project.id)
        channel = {"type": "webhook", "url": "https://hooks.example/x", "events": ["task.*"]}
        project.settings = {**project.settings, "notifications": [channel]}
    http = httpx.Client(transport=httpx.MockTransport(refuse))
    with platform.db.session() as s:
        note = notify(
            s,
            tenant_id=seed.tenant.id,
            project_id=seed.project.id,
            event="task.pr_open",
            subject="s",
        )
        with pytest.raises(RuntimeError, match="1 notification channel"):
            deliver(s, note, http, resolver=lambda _host: ["93.184.216.34"])
        assert note.deliveries == [{"channel": "webhook", "ok": False}]
        # An event the channel did not subscribe to is not sent anywhere.
        other = notify(
            s, tenant_id=seed.tenant.id, project_id=seed.project.id, event="job.dead", subject="s"
        )
        assert deliver(s, other, http) == []
        assert s.scalars(select(Notification)).all()


# --------------------------------------------------------------------------- alembic command


def _alembic_config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def test_the_alembic_command_migrates_the_configured_database(tmp_path: Path, monkeypatch) -> None:
    """``alembic upgrade head`` (what a contributor runs) uses ``OPSMITH_DATABASE_URL``."""
    url = f"sqlite:///{tmp_path / 'cli.db'}"
    monkeypatch.setenv("OPSMITH_DATABASE_URL", url)
    monkeypatch.setenv("OPSMITH_DATA_DIR", str(tmp_path / "data"))
    command.upgrade(_alembic_config(), "head")
    engine = create_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert {"tasks", "reviews", "jobs", "alembic_version"} <= tables


def test_offline_migrations_are_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPSMITH_DATA_DIR", str(tmp_path / "data"))
    with pytest.raises(SystemExit, match="offline"):
        command.upgrade(_alembic_config(), "head", sql=True)
