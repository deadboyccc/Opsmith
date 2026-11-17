"""SCM provider interface.

Deliberately has **no merge operation**: Opsmith can push a branch and open a PR, never
merge it (spec 3, 8). Tokens are scoped to contents:write on branches + pull-requests:write.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

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
# A branch name as plain path segments. It starts with a letter or digit, so it can never be
# read as an option by git, and holds nothing git or a shell treats specially.
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")


def branch_problem(name: object) -> str | None:
    """Why ``name`` cannot be used as a default branch, or ``None`` when it can.

    The name is handed to ``git clone`` and ``git fetch`` on the worker. Unchecked, a value
    such as ``--upload-pack=<command>`` is an option to git, which runs the command."""
    if (
        not isinstance(name, str)
        or not _BRANCH.fullmatch(name)
        or ".." in name
        or "//" in name
        or name.endswith(("/", ".", ".lock"))
    ):
        return (
            "default_branch must be a plain branch name: letters, digits, '.', '_', '-' and "
            "'/', starting with a letter or digit, at most 100 characters"
        )
    return None


def remote_problem(provider: str, url: object) -> str | None:
    """Why ``url`` cannot be the address of a repository of ``provider``, or ``None``.

    A ``github`` repository is fetched with git over the network, so its address must be an
    https URL: a path or ``file://`` URL would read any repository on the worker (past
    ``OPSMITH_LOCAL_ROOT``, which confines only the ``local`` provider), ``http://`` would send
    the token in clear text, and a value starting with ``-`` would be an option to git. A
    ``local`` repository is a path, confined when it is used."""
    if not isinstance(url, str) or not url or len(url) > 500:
        return "url must be a string of at most 500 characters"
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return "url must not contain whitespace or control characters"
    if provider != "github":
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
        has_credentials = parts.username is not None or parts.password is not None
    except ValueError:
        return "url is not a valid URL"
    if parts.scheme != "https" or not host or not parts.path.strip("/"):
        return "a github repository needs an https:// clone URL"
    if has_credentials:
        return "url must not contain credentials; use credentials_ref"
    return None


def repository_problem(provider: str, url: object, default_branch: object) -> str | None:
    return remote_problem(provider, url) or branch_problem(default_branch)


def check_repository(repo: Repository) -> None:
    """Raise unless the repository's address and branch are safe to hand to git. Checked by
    the providers on every use, whatever wrote the row."""
    problem = repository_problem(repo.scm_provider, repo.url, repo.default_branch)
    if problem:
        raise SCMError(f"repository {repo.name}: {problem}")


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
