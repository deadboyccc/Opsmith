"""Execution sandboxes and the confined workspace API exposed to coding agents.

* ``DockerSandbox`` - each command runs in a throwaway container with ``--network none``, a
  read-only root filesystem, CPU/memory/pid limits, all capabilities dropped, a non-root user
  and only the workspace mounted. This is the default and the only isolating backend.
* ``LocalSandbox`` - a plain subprocess with a scrubbed environment, timeouts and output
  caps. It does **not** isolate anything: the command shares the worker's network, filesystem
  and user. It exists for development, tests and the offline demo, and the executor refuses to
  use it for repositories that are not on the ``local`` SCM provider.

Agents never receive a shell: they get ``Workspace`` file operations confined to the
checkout and may only invoke the repository's *configured* check commands by name.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

MAX_OUTPUT = 20_000
MAX_WRITE_BYTES = 512_000
MAX_READ_BYTES = 5 * 1024 * 1024
SANDBOX_BACKENDS = ("docker", "local")
DOCKER_ERROR_EXIT = 125  # `docker run` exits with 125 when the docker command itself fails
# Backends that actually confine what a command can reach. Anything else is development-only.
ISOLATING_BACKENDS = frozenset({"docker"})
# Entries at the top of the checkout that agents may never read, write or list.
RESERVED_DIRS = frozenset({".git", ".opsmith-home"})


class SandboxUnavailable(RuntimeError):
    """The configured sandbox backend cannot run commands on this host."""


@dataclass
class CommandResult:
    command: str
    exit_code: int
    output: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class Sandbox(Protocol):
    root: Path
    sandbox_id: str

    def run(self, command: str, timeout: int = 600) -> CommandResult: ...


def scrubbed_env(root: Path) -> dict[str, str]:
    home = root / ".opsmith-home"
    home.mkdir(exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "CI": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _cap(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT else "...[truncated]...\n" + text[-MAX_OUTPUT:]


class LocalSandbox:
    """Development-only: scrubs the environment but provides no isolation."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.sandbox_id = f"local-{uuid.uuid4().hex[:10]}"

    def run(self, command: str, timeout: int = 600) -> CommandResult:
        # Commands come from repository configuration set by admins, never from the agent.
        # The command gets its own process group so a timeout kills everything it started,
        # not just the shell.
        proc = subprocess.Popen(
            ["sh", "-c", command],
            cwd=self.root,
            env=scrubbed_env(self.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,
        )
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            try:
                out, _ = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:  # a child escaped the group and holds the pipe
                proc.kill()
                out = ""
            return CommandResult(command, 124, _cap(out or ""), timed_out=True)
        return CommandResult(command, proc.returncode, _cap(out or ""))


class DockerSandbox:
    def __init__(self, root: Path, image: str, *, cpus: str = "1", memory: str = "1g") -> None:
        self.root = root.resolve()
        self.image = image
        self.cpus = cpus
        self.memory = memory
        self.sandbox_id = f"docker-{uuid.uuid4().hex[:10]}"
        self._runs = 0

    def docker_command(self, command: str, name: str | None = None) -> list[str]:
        return [
            "docker",
            "run",
            "--rm",
            "--init",
            *(["--name", name] if name else []),
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,exec,nosuid,nodev,size=512m",  # noqa: S108 - a path inside the container
            "--cpus",
            self.cpus,
            "--memory",
            self.memory,
            "--memory-swap",
            self.memory,  # equal to --memory: no swap on top of the limit
            "--pids-limit",
            "512",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-e",
            "HOME=/workspace/.opsmith-home",
            "-e",
            "CI=1",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-v",
            f"{self.root}:/workspace",
            "-w",
            "/workspace",
            "--label",
            f"opsmith.sandbox={self.sandbox_id}",
            self.image,
            "sh",
            "-c",
            # One stream inside the container: Docker forwards stdout and stderr separately,
            # which would otherwise scramble the order of a check's output.
            f"exec 2>&1; {command}",
        ]

    def run(self, command: str, timeout: int = 600) -> CommandResult:
        (self.root / ".opsmith-home").mkdir(exist_ok=True)
        self._runs += 1
        name = f"opsmith-{self.sandbox_id}-{self._runs}"
        try:
            proc = subprocess.run(
                self.docker_command(command, name),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # Killing the client does not stop the container; kill it by name (--rm removes it).
            subprocess.run(["docker", "kill", name], capture_output=True, check=False, timeout=60)
            raw = exc.stdout or ""
            out = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
            return CommandResult(command, 124, _cap(out) or "timed out", timed_out=True)
        if proc.returncode == DOCKER_ERROR_EXIT:
            # `docker run` itself failed (daemon unreachable, image missing, bad mount). That
            # is an infrastructure problem, not a failing check, and must not be reported as one.
            raise SandboxUnavailable(f"docker could not start the sandbox: {proc.stdout[-500:]}")
        return CommandResult(command, proc.returncode, _cap(proc.stdout or ""))


def docker_daemon_error() -> str | None:
    """Why the Docker daemon cannot be used from here, or ``None`` when it can."""
    if shutil.which("docker") is None:
        return "the docker CLI is not on the worker's PATH"
    try:
        probe = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"the docker CLI failed: {exc}"
    if probe.returncode != 0:
        return f"cannot reach the Docker daemon: {probe.stderr.strip()[-300:]}"
    return None


class WorkspaceError(ValueError):
    pass


class Workspace:
    """File operations confined to the checkout. ``.git`` is off limits."""

    def __init__(self, sandbox: Sandbox, checks: dict[str, str], timeout: int = 900) -> None:
        self.sandbox = sandbox
        self.root = sandbox.root
        self.checks = {k: v for k, v in checks.items() if v}
        self.timeout = timeout
        self.check_log: list[CommandResult] = []

    def resolve(self, rel: str) -> Path:
        if not rel or rel.startswith("/") or "\x00" in rel:
            raise WorkspaceError(f"path must be relative to the repository: {rel!r}")
        target = (self.root / rel).resolve()
        if not target.is_relative_to(self.root):
            raise WorkspaceError(f"path escapes the workspace: {rel!r}")
        parts = target.relative_to(self.root).parts
        if parts and parts[0] in RESERVED_DIRS:
            raise WorkspaceError(f"path is not editable: {rel!r}")
        return target

    def list_files(self, rel: str = ".", limit: int = 400) -> list[str]:
        base = self.root if rel in {".", ""} else self.resolve(rel)
        out: list[str] = []
        for path in sorted(base.rglob("*")):
            relp = path.relative_to(self.root)
            if relp.parts and relp.parts[0] in RESERVED_DIRS:
                continue
            if path.is_file():
                out.append(relp.as_posix())
            if len(out) >= limit:
                break
        return out

    def read_file(self, rel: str, max_chars: int = 60_000) -> str:
        path = self.resolve(rel)
        if not path.is_file():
            raise WorkspaceError(f"no such file: {rel}")
        if path.stat().st_size > MAX_READ_BYTES:
            raise WorkspaceError(f"file too large to read: {rel}")
        text = path.read_text(encoding="utf-8", errors="replace")
        return text if len(text) <= max_chars else text[:max_chars] + "\n...[truncated]"

    def write_file(self, rel: str, content: str) -> None:
        if len(content.encode()) > MAX_WRITE_BYTES:
            raise WorkspaceError("file too large")
        path = self.resolve(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def replace_in_file(self, rel: str, old: str, new: str) -> None:
        text = self.read_file(rel, max_chars=10_000_000)
        count = text.count(old)
        if count != 1:
            raise WorkspaceError(f"expected exactly one match in {rel}, found {count}")
        self.write_file(rel, text.replace(old, new, 1))

    def run_check(self, name: str) -> CommandResult:
        if name not in self.checks:
            raise WorkspaceError(f"unknown check {name!r}; available: {sorted(self.checks)}")
        result = self.sandbox.run(self.checks[name], timeout=self.timeout)
        self.check_log.append(result)
        return result


def make_sandbox(backend: str, root: Path, image: str) -> Sandbox:
    if backend == "docker":
        problem = docker_daemon_error()
        if problem:
            raise SandboxUnavailable(
                f"the docker sandbox is not usable: {problem}. See docs/guide/deployment.md "
                "(Execution sandbox) for how to give the worker a sandbox."
            )
        return DockerSandbox(root, image)
    if backend == "local":
        return LocalSandbox(root)
    raise ValueError(f"unknown sandbox backend {backend!r}; expected one of {SANDBOX_BACKENDS}")
