"""Local git provider for development, demos and tests.

``Repository.url`` is a filesystem path to a git repository. Pull requests are recorded in a
JSON file under the data dir and CI runs the repository's ``test_command`` on the PR branch
in a scrubbed sandbox, so the full loop (branch -> PR -> CI -> human merge) runs offline.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

from opsmith.db import utcnow
from opsmith.executor.sandbox import LocalSandbox
from opsmith.gitutil import run_git
from opsmith.models import Repository
from opsmith.scm.base import (
    PullRequestRef,
    PullRequestStatus,
    SCMError,
    SCMProvider,
    check_pushable,
)

_LOCK = threading.Lock()


class LocalSCM(SCMProvider):
    name = "local"

    def __init__(self, state_dir: Path, run_ci: bool = True) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.run_ci = run_ci

    def _state_file(self, repo: Repository) -> Path:
        return self.state_dir / f"{repo.id}.json"

    def _load(self, repo: Repository) -> list[dict[str, Any]]:
        path = self._state_file(repo)
        return json.loads(path.read_text()) if path.exists() else []

    def _save(self, repo: Repository, prs: list[dict[str, Any]]) -> None:
        self._state_file(repo).write_text(json.dumps(prs, indent=2))

    def checkout(self, repo: Repository, dest: Path) -> Path:
        if (dest / ".git").exists():
            run_git(["fetch", repo.url, repo.default_branch], dest)
            run_git(["reset", "--hard", "FETCH_HEAD"], dest)
            run_git(["clean", "-fdx"], dest)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            run_git(["clone", "--branch", repo.default_branch, repo.url, str(dest)])
        return dest

    def push_branch(
        self, repo: Repository, workdir: Path, branch: str, *, git_dir: Path | None = None
    ) -> None:
        check_pushable(repo, branch)
        run_git(
            ["push", "--force", repo.url, f"HEAD:refs/heads/{branch}"], workdir, git_dir=git_dir
        )

    def create_pull_request(
        self, repo: Repository, *, branch: str, title: str, body: str
    ) -> PullRequestRef:
        with _LOCK:
            prs = self._load(repo)
            for pr in prs:
                if pr["branch"] == branch and pr["state"] == "open":
                    return PullRequestRef(pr["number"], pr["url"])
            number = len(prs) + 1
            sha = run_git(["rev-parse", f"refs/heads/{branch}"], Path(repo.url)).strip()
            pr = {
                "number": number,
                "url": f"local://{repo.name}/pull/{number}",
                "branch": branch,
                "base": repo.default_branch,
                "title": title,
                "body": body,
                "state": "open",
                "head_sha": sha,
                "ci": "pending",
                "ci_log": "",
                "created_at": utcnow().isoformat(),
            }
            prs.append(pr)
            self._save(repo, prs)
        if self.run_ci:
            self._run_ci(repo, number)
        return PullRequestRef(number, pr["url"])

    def _run_ci(self, repo: Repository, number: int) -> None:
        prs = self._load(repo)
        pr = prs[number - 1]
        tmp = Path(tempfile.mkdtemp(prefix="opsmith-ci-"))
        try:
            run_git(["clone", "--branch", pr["branch"], repo.url, str(tmp / "src")])
            results = []
            for name, cmd in (
                ("build", repo.build_command),
                ("lint", repo.lint_command),
                ("test", repo.test_command),
            ):
                if cmd:
                    res = LocalSandbox(tmp / "src").run(cmd, timeout=900)
                    results.append((name, res.exit_code, res.output[-2000:]))
            ok = all(code == 0 for _, code, _ in results)
            pr["ci"] = "success" if ok else "failure"
            pr["ci_log"] = "\n".join(f"[{n}] exit={c}\n{o}" for n, c, o in results)[-6000:]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        with _LOCK:
            prs = self._load(repo)
            prs[number - 1].update(ci=pr["ci"], ci_log=pr["ci_log"])
            self._save(repo, prs)

    def get_pull_request(self, repo: Repository, number: int) -> PullRequestStatus:
        prs = self._load(repo)
        if not 0 < number <= len(prs):
            raise SCMError(f"PR #{number} not found")
        pr = prs[number - 1]
        return PullRequestStatus(
            state=pr["state"],
            ci=pr["ci"],
            head_sha=pr["head_sha"],
            review_comments=len(pr.get("comments", [])),
        )

    # ---------------------------------------------------- human actions (outside the platform)
    def human_merge(self, repo: Repository, number: int) -> None:
        """Simulates a *human* merging in the SCM UI. Not part of ``SCMProvider``: the
        platform itself has no code path that merges."""
        with _LOCK:
            prs = self._load(repo)
            pr = prs[number - 1]
            src = Path(repo.url)
            run_git(
                ["merge", "--no-ff", "-m", f"Merge PR #{number}", pr["branch"]],
                src,
                author=("Human Reviewer", "reviewer@example.com"),
            )
            pr["state"] = "merged"
            self._save(repo, prs)

    def human_close(self, repo: Repository, number: int) -> None:
        with _LOCK:
            prs = self._load(repo)
            prs[number - 1]["state"] = "closed"
            self._save(repo, prs)
