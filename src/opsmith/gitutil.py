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


# Applied to every command. Hooks, fsmonitor and external diff/pager programs are the ways a
# repository can make git execute code, so they are switched off regardless of repo config.
_SAFE_CONFIG = (
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "protocol.ext.allow=never",
)


def run_git(
    args: list[str],
    cwd: Path | None = None,
    *,
    token: str | None = None,
    timeout: int = 300,
    author: tuple[str, str] | None = None,
    git_dir: Path | None = None,
    errors: str = "strict",
) -> str:
    """Run one git command.

    ``git_dir`` pins the repository metadata to a directory other than ``<cwd>/.git`` (with
    ``cwd`` as the work tree). The executor uses it so that nothing a sandboxed command wrote
    into the checkout's own ``.git`` (config, hooks, filters) is ever read by the platform.

    Output is decoded as strict UTF-8 by default, so a file name is never silently altered.
    ``errors="replace"`` is for output that contains file contents, such as a ``--text`` diff.
    """
    cmd = ["git"]
    for item in _SAFE_CONFIG:
        cmd += ["-c", item]
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
    if git_dir is not None:
        if cwd is None:
            raise GitError("git_dir requires cwd (the work tree)")
        extra["GIT_DIR"] = str(git_dir)
        extra["GIT_WORK_TREE"] = str(cwd)
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            errors=errors,
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


def head_sha(repo: Path, *, git_dir: Path | None = None) -> str:
    return run_git(["rev-parse", "HEAD"], repo, git_dir=git_dir).strip()


def changed_files(repo: Path, base: str, head: str) -> list[str]:
    out = run_git(["diff", "--name-only", "-z", f"{base}..{head}"], repo)
    return [name for name in out.split("\0") if name]


def worktree_changes(repo: Path, *, git_dir: Path | None = None) -> list[str]:
    """Every path that differs from HEAD, including untracked files.

    Parsed from the NUL-delimited porcelain format, where paths are verbatim. The line-based
    format quotes unusual names and writes renames as ``old -> new``, so a file literally named
    ``.github/x -> y`` would be reported as ``y`` and slip past the protected-path gate.
    Both sides of a rename are reported: the old path is changed too (it is deleted).
    """
    out = run_git(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"], repo, git_dir=git_dir
    )
    fields = out.split("\0")
    files: list[str] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        files.append(path)
        if "R" in status or "C" in status:  # the original path follows as its own field
            if i < len(fields) and fields[i]:
                files.append(fields[i])
            i += 1
    return files


def nested_git_entries(root: Path) -> list[str]:
    """Every entry named ``.git`` below ``root``, other than ``root/.git`` itself.

    A nested repository brings its own configuration, and git reads it when it descends into
    a submodule: a filter driver defined there runs for whoever calls ``git status`` in the
    parent. Opsmith checks out without submodules, so any such entry in a workspace was
    written by sandboxed code or the agent. Symbolic links are reported, never followed.
    """
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath).relative_to(root)
        top = here == Path(".")
        found += [
            (here / name).as_posix()
            for name in (*dirnames, *filenames)
            if name == ".git" and not top
        ]
        dirnames[:] = [d for d in dirnames if d != ".git"]
    return sorted(found)


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def staged_added_lines(repo: Path, *, git_dir: Path | None = None) -> list[tuple[str, int, str]]:
    """``(path, line_no, text)`` for every line the staged diff adds (new files included).

    File headers are only recognised between a ``diff --git`` line and the first hunk, so an
    added line whose own text starts with ``++ `` (rendered ``+++ ...``) is still scanned.
    """
    run_git(["add", "-A"], repo, git_dir=git_dir)
    diff = run_git(
        # --text: a "-diff" or "binary" attribute would otherwise print "Binary files differ"
        # instead of the added lines, and hide them from the credential scan.
        ["diff", "--cached", "-U0", "--text", "--no-color", "--no-ext-diff", "--no-textconv"],
        repo,
        git_dir=git_dir,
        errors="replace",  # binary files are printed too; their bytes need not be UTF-8
    )
    out: list[tuple[str, int, str]] = []
    path: str | None = None
    line_no = 0
    in_header = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_header, path = True, None
        elif in_header and line.startswith("+++ "):
            target = line[4:].rstrip("\t")  # git appends a tab when the name has spaces
            path = None if target == "/dev/null" else target.removeprefix("b/")
        elif line.startswith("@@"):
            in_header = False
            match = _HUNK.match(line)
            line_no = int(match.group(1)) if match else 0
        elif not in_header and line.startswith("+") and path is not None:
            out.append((path, line_no, line[1:]))
            line_no += 1
    return out
