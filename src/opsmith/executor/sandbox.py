"""Execution sandboxes and the confined workspace API exposed to coding agents.

* ``LocalSandbox`` - subprocess with a scrubbed environment (no inherited credentials, HOME
  inside the workspace), hard timeouts and output caps. For dev/CI.
* ``DockerSandbox`` - each command runs in a throwaway container with ``--network none``,
  CPU/memory/pid limits, all capabilities dropped and only the workspace mounted.

Agents never receive a shell: they get ``Workspace`` file operations confined to the
checkout and may only invoke the repository's *configured* check commands by name.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

MAX_OUTPUT = 20_000
MAX_WRITE_BYTES = 512_000


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
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.sandbox_id = f"local-{uuid.uuid4().hex[:10]}"

    def run(self, command: str, timeout: int = 600) -> CommandResult:
        # Commands come from repository configuration set by admins, never from the agent.
        try:
            proc = subprocess.run(
                ["sh", "-c", command],
                cwd=self.root,
                env=scrubbed_env(self.root),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            out = (
                (exc.stdout or b"").decode()
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or "")
            )
            return CommandResult(command, 124, _cap(out), timed_out=True)
        return CommandResult(command, proc.returncode, _cap(proc.stdout + proc.stderr))


class DockerSandbox:
    def __init__(self, root: Path, image: str, *, cpus: str = "1", memory: str = "1g") -> None:
        self.root = root.resolve()
        self.image = image
        self.cpus = cpus
        self.memory = memory
        self.sandbox_id = f"docker-{uuid.uuid4().hex[:10]}"

    def docker_command(self, command: str) -> list[str]:
        return [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--cpus",
            self.cpus,
            "--memory",
            self.memory,
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
            "-v",
            f"{self.root}:/workspace",
            "-w",
            "/workspace",
            "--label",
            f"opsmith.sandbox={self.sandbox_id}",
            self.image,
            "sh",
            "-c",
            command,
        ]

    def run(self, command: str, timeout: int = 600) -> CommandResult:
        try:
            proc = subprocess.run(
                self.docker_command(command),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            ids = subprocess.run(
                ["docker", "ps", "-q", "--filter", f"label=opsmith.sandbox={self.sandbox_id}"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.split()
            if ids:
                subprocess.run(["docker", "kill", *ids], capture_output=True, check=False)
            return CommandResult(command, 124, "timed out", timed_out=True)
        return CommandResult(command, proc.returncode, _cap(proc.stdout + proc.stderr))


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
        if parts and parts[0] in {".git", ".opsmith-home"}:
            raise WorkspaceError(f"path is not editable: {rel!r}")
        return target

    def list_files(self, rel: str = ".", limit: int = 400) -> list[str]:
        base = self.root if rel in {".", ""} else self.resolve(rel)
        out: list[str] = []
        for path in sorted(base.rglob("*")):
            relp = path.relative_to(self.root)
            if relp.parts and relp.parts[0] in {".git", ".opsmith-home"}:
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
        return DockerSandbox(root, image)
    if backend == "local":
        return LocalSandbox(root)
    raise ValueError(f"unknown sandbox backend {backend!r}")


def quote(cmd: list[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)
