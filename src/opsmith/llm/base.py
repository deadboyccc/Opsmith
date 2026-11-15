"""Provider-neutral LLM interfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel


class LLMError(RuntimeError):
    pass


class LLMOutputError(LLMError):
    """The model's output failed schema validation (after the allowed retries)."""


class LLMRefusal(LLMError):
    pass


class BudgetExceeded(LLMError):
    pass


class ProviderNotAllowed(LLMError):
    pass


@dataclass
class ProviderRequest:
    stage: str
    prompt_id: str
    model: str
    system: str
    user: str
    schema: type[BaseModel]
    # Structured (already redacted) inputs. Deterministic providers read these instead of
    # parsing ``user``; remote providers only ever see ``system``/``user``.
    variables: dict[str, Any] = field(default_factory=dict)
    max_tokens: int = 16000
    effort: str = "medium"


@dataclass
class ProviderResponse:
    data: dict[str, Any] | str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    cache_read_tokens: int = 0


class LLMProvider(Protocol):
    name: str

    def complete(self, request: ProviderRequest) -> ProviderResponse: ...
