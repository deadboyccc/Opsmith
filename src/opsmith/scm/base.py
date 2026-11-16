"""SCM provider interface.

Deliberately has **no merge operation**: Opsmith can push a branch and open a PR, never
merge it (spec 3, 8). Tokens are scoped to contents:write on branches + pull-requests:write.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from opsmith.models import Repository


@dataclass
class PullRequestRef:
    number: int
    url: str


@dataclass
class PullRequestStatus:
    state: str  # open | merged | closed
    ci: str  # pending | success | failure | none
    head_sha: str | None = None
    review_comments: int = 0


class SCMError(RuntimeError):
    pass


BRANCH_PREFIX = "opsmith/"


def check_pushable(repo: Repository, branch: str) -> None:
    """Opsmith only ever writes to its own branch namespace, never the default branch."""
    if branch == repo.default_branch:
        raise SCMError("refusing to push to the default branch")
    if not branch.startswith(BRANCH_PREFIX):
        raise SCMError(f"refusing to push outside the {BRANCH_PREFIX!r} branch namespace")


class SCMProvider(ABC):
    name: str

    @abstractmethod
    def checkout(self, repo: Repository, dest: Path) -> Path:
        """Clone (or refresh) the default branch into ``dest`` for read-only analysis."""

    @abstractmethod
    def push_branch(
        self, repo: Repository, workdir: Path, branch: str, *, git_dir: Path | None = None
    ) -> None:
        """Push ``HEAD`` to ``branch``. Implementations must refuse the default branch and
        anything outside the ``opsmith/`` namespace. ``git_dir`` is the trusted repository
        metadata to push from when it is not ``<workdir>/.git``."""

    @abstractmethod
    def create_pull_request(
        self, repo: Repository, *, branch: str, title: str, body: str
    ) -> PullRequestRef: ...

    @abstractmethod
    def get_pull_request(self, repo: Repository, number: int) -> PullRequestStatus: ...
