"""Thin, explicit wrappers around the git CLI (no shell, scrubbed environment)."""

from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile
from pathlib import Path


class GitError(RuntimeError):
    pass


def git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME") or tempfile.gettempdir(),
        "LANG": "C.UTF-8",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    env.update(extra or {})
    return env


def run_git(
    args: list[str],
    cwd: Path | None = None,
    *,
    token: str | None = None,
    timeout: int = 300,
    author: tuple[str, str] | None = None,
) -> str:
    cmd = ["git"]
    if token:
        # Credentials go in a transient header for this one command only; they are never
        # written to .git/config or exposed to the sandboxed agent.
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        cmd += ["-c", f"http.extraHeader=Authorization: Basic {basic}"]
    cmd += args
    extra: dict[str, str] = {}
    if author:
        name, email = author
        extra = {
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
        }
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=git_env(extra),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} timed out") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        if token:
            stderr = stderr.replace(token, "***")
        raise GitError(f"git {' '.join(args[:2])} failed: {stderr[-800:]}")
    return proc.stdout


def head_sha(repo: Path) -> str:
    return run_git(["rev-parse", "HEAD"], repo).strip()


def changed_files(repo: Path, base: str, head: str) -> list[str]:
    out = run_git(["diff", "--name-only", f"{base}..{head}"], repo)
    return [line for line in out.splitlines() if line.strip()]


def worktree_changes(repo: Path) -> list[str]:
    out = run_git(["status", "--porcelain", "--untracked-files=all"], repo)
    files = []
    for line in out.splitlines():
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path.strip('"'))
    return files


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def staged_added_lines(repo: Path) -> list[tuple[str, int, str]]:
    """``(path, line_no, text)`` for every line the staged diff adds (new files included)."""
    run_git(["add", "-A"], repo)
    diff = run_git(["diff", "--cached", "-U0", "--no-color", "--no-ext-diff"], repo)
    out: list[tuple[str, int, str]] = []
    path: str | None = None
    line_no = 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            path = None if target == "/dev/null" else target.removeprefix("b/")
        elif line.startswith("@@"):
            match = _HUNK.match(line)
            line_no = int(match.group(1)) if match else 0
        elif line.startswith("+") and path is not None:
            out.append((path, line_no, line[1:]))
            line_no += 1
    return out
