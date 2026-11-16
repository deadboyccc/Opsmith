"""GitHub provider (REST API v3 + git over HTTPS).

Required fine-grained token permissions: Contents read/write (branch push), Pull requests
read/write, Checks/Commit statuses read. No admin/merge permission is ever needed. Protect
the default branch so the token cannot push to it directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from opsmith.config import CredentialRefError, resolve_tenant_credential
from opsmith.gitutil import run_git
from opsmith.models import Repository
from opsmith.scm.base import (
    PullRequestRef,
    PullRequestStatus,
    SCMError,
    SCMProvider,
    check_pushable,
)


class GitHubSCM(SCMProvider):
    name = "github"

    def __init__(
        self, http: httpx.Client | None = None, api_url: str = "https://api.github.com"
    ) -> None:
        self._http = http
        self.api_url = api_url.rstrip("/")

    def _token(self, repo: Repository) -> str:
        try:
            token = resolve_tenant_credential(repo.credentials_ref)
        except CredentialRefError as exc:
            raise SCMError(str(exc)) from exc
        if not token:
            raise SCMError(f"no token resolvable from {repo.credentials_ref!r} for {repo.name}")
        return token

    def _client(self, repo: Repository) -> httpx.Client:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {self._token(repo)}",
            "User-Agent": "opsmith",
        }
        if self._http is not None:
            self._http.headers.update(headers)
            return self._http
        return httpx.Client(base_url=self.api_url, headers=headers, timeout=30)

    def _request(self, repo: Repository, method: str, path: str, **kw: Any) -> httpx.Response:
        client = self._client(repo)
        try:
            url = path if self._http is None else f"{self.api_url}{path}"
            return client.request(method, url, **kw)
        except httpx.HTTPError as exc:
            raise SCMError(f"GitHub API {method} {path} failed: {exc}") from exc
        finally:
            if self._http is None:
                client.close()

    @staticmethod
    def _full_name(repo: Repository) -> str:
        if repo.full_name:
            return repo.full_name
        tail = repo.url.rstrip("/").removesuffix(".git").split("github.com/")[-1]
        if tail.count("/") != 1:
            raise SCMError(f"cannot derive owner/repo from {repo.url}")
        return tail

    # ------------------------------------------------------------------ git
    def checkout(self, repo: Repository, dest: Path) -> Path:
        token = self._token(repo)
        if (dest / ".git").exists():
            run_git(["fetch", "--depth", "200", repo.url, repo.default_branch], dest, token=token)
            run_git(["reset", "--hard", "FETCH_HEAD"], dest)
            run_git(["clean", "-fdx"], dest)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            run_git(
                ["clone", "--depth", "200", "--branch", repo.default_branch, repo.url, str(dest)],
                token=token,
            )
        return dest

    def push_branch(
        self, repo: Repository, workdir: Path, branch: str, *, git_dir: Path | None = None
    ) -> None:
        check_pushable(repo, branch)
        # Forced: the branch belongs to Opsmith, and a retry after a failed PR creation must be
        # able to replace the commit an earlier attempt pushed.
        run_git(
            ["push", "--force", repo.url, f"HEAD:refs/heads/{branch}"],
            workdir,
            token=self._token(repo),
            git_dir=git_dir,
        )

    # ------------------------------------------------------------------ pull requests
    def create_pull_request(
        self, repo: Repository, *, branch: str, title: str, body: str
    ) -> PullRequestRef:
        fn = self._full_name(repo)
        resp = self._request(
            repo,
            "POST",
            f"/repos/{fn}/pulls",
            json={
                "title": title,
                "head": branch,
                "base": repo.default_branch,
                "body": body,
                "maintainer_can_modify": True,
            },
        )
        if resp.status_code == 422 and "already exists" in resp.text:
            owner = fn.split("/")[0]
            existing = self._request(
                repo,
                "GET",
                f"/repos/{fn}/pulls",
                params={"head": f"{owner}:{branch}", "state": "open"},
            )
            items = existing.json() if existing.status_code == 200 else []
            if items:
                return PullRequestRef(number=items[0]["number"], url=items[0]["html_url"])
        if resp.status_code not in (200, 201):
            raise SCMError(f"create PR failed ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()
        return PullRequestRef(number=int(data["number"]), url=str(data["html_url"]))

    def get_pull_request(self, repo: Repository, number: int) -> PullRequestStatus:
        fn = self._full_name(repo)
        resp = self._request(repo, "GET", f"/repos/{fn}/pulls/{number}")
        if resp.status_code != 200:
            raise SCMError(f"get PR failed ({resp.status_code}): {resp.text[:300]}")
        pr = resp.json()
        state = (
            "merged" if pr.get("merged") else ("open" if pr.get("state") == "open" else "closed")
        )
        sha = (pr.get("head") or {}).get("sha")
        return PullRequestStatus(
            state=state,
            ci=self._ci(repo, fn, sha) if sha else "none",
            head_sha=sha,
            review_comments=int(pr.get("review_comments", 0)),
        )

    def _ci(self, repo: Repository, fn: str, sha: str) -> str:
        runs_resp = self._request(repo, "GET", f"/repos/{fn}/commits/{sha}/check-runs")
        runs = runs_resp.json().get("check_runs", []) if runs_resp.status_code == 200 else []
        if runs:
            if any(r.get("status") != "completed" for r in runs):
                return "pending"
            bad = {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}
            return "failure" if any(r.get("conclusion") in bad for r in runs) else "success"
        status = self._request(repo, "GET", f"/repos/{fn}/commits/{sha}/status")
        if status.status_code == 200 and status.json().get("total_count", 0):
            return {"success": "success", "failure": "failure", "error": "failure"}.get(
                status.json().get("state", ""), "pending"
            )
        return "none"
