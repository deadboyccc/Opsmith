"""Pluggable LLM provider layer."""

from __future__ import annotations

import logging
from importlib.metadata import entry_points

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
from opsmith.llm.openai_provider import ChatClient, OpenAICompatibleProvider

log = logging.getLogger(__name__)


def default_gateway(settings: Settings) -> LLMGateway:
    """The built-in providers, plus any installed through the ``opsmith.llm_providers``
    entry-point group (each entry point is a callable ``(settings) -> LLMProvider``)."""
    providers: dict[str, LLMProvider] = {
        "heuristic": HeuristicProvider(),
        "anthropic": AnthropicProvider(api_key_ref=settings.anthropic_api_key_ref),
        "openai": OpenAICompatibleProvider(
            ChatClient(settings.openai_base_url, settings.openai_api_key_ref),
            json_mode=settings.openai_json_mode,
        ),
    }
    for ep in entry_points(group="opsmith.llm_providers"):
        try:
            provider = ep.load()(settings)
            providers.setdefault(ep.name, provider)
        except Exception:  # a broken plugin must not take the platform down
            log.exception("failed to load LLM provider plugin", extra={"plugin": ep.name})
    return LLMGateway(providers, process_allowlist=settings.llm_provider_allowlist)


__all__ = [
    "AnthropicProvider",
    "BudgetExceeded",
    "BudgetGuard",
    "ChatClient",
    "HeuristicProvider",
    "LLMError",
    "LLMGateway",
    "LLMOutputError",
    "LLMProvider",
    "LLMRefusal",
    "LLMResult",
    "OpenAICompatibleProvider",
    "ProviderNotAllowed",
    "ProviderRequest",
    "ProviderResponse",
    "default_gateway",
]
