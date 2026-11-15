"""Pluggable LLM provider layer."""

from __future__ import annotations

from opsmith.config import Settings
from opsmith.llm.anthropic_provider import AnthropicProvider
from opsmith.llm.base import (
    BudgetExceeded,
    LLMError,
    LLMOutputError,
    LLMProvider,
    LLMRefusal,
    ProviderNotAllowed,
    ProviderRequest,
    ProviderResponse,
)
from opsmith.llm.gateway import BudgetGuard, LLMGateway, LLMResult
from opsmith.llm.heuristic import HeuristicProvider


def default_gateway(settings: Settings) -> LLMGateway:
    providers: dict[str, LLMProvider] = {"heuristic": HeuristicProvider()}
    providers["anthropic"] = AnthropicProvider(api_key_ref=settings.anthropic_api_key_ref)
    return LLMGateway(providers, process_allowlist=settings.llm_provider_allowlist)


__all__ = [
    "AnthropicProvider",
    "BudgetExceeded",
    "BudgetGuard",
    "HeuristicProvider",
    "LLMError",
    "LLMGateway",
    "LLMOutputError",
    "LLMProvider",
    "LLMRefusal",
    "LLMResult",
    "ProviderNotAllowed",
    "ProviderRequest",
    "ProviderResponse",
    "default_gateway",
]
