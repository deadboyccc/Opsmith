"""SCM integrations (branch push, PR creation, PR/CI status). No merge capability."""

from __future__ import annotations

from opsmith.scm.base import PullRequestRef, PullRequestStatus, SCMError, SCMProvider
from opsmith.scm.github import GitHubSCM

__all__ = [
    "GitHubSCM",
    "PullRequestRef",
    "PullRequestStatus",
    "SCMError",
    "SCMProvider",
    "SCMRegistry",
]


class SCMRegistry:
    def __init__(self, providers: dict[str, SCMProvider]) -> None:
        self.providers = providers

    def for_provider(self, name: str) -> SCMProvider:
        try:
            return self.providers[name]
        except KeyError:
            raise SCMError(f"no SCM provider configured for {name!r}") from None
