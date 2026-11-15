"""Claude provider using the official ``anthropic`` SDK (optional extra: ``opsmith[anthropic]``).

Structured stages use ``client.messages.parse(output_format=<pydantic model>)`` so the API
constrains the response to the stage schema; the gateway validates it again regardless.
Server-side refusal fallbacks are enabled by default (``OPSMITH_ANTHROPIC_FALLBACKS``).
"""

from __future__ import annotations

import os
from typing import Any

from opsmith.config import resolve_credential
from opsmith.llm.base import LLMError, LLMOutputError, LLMRefusal, ProviderRequest, ProviderResponse

FALLBACK_BETA = "server-side-fallback-2026-07-01"


def make_client(api_key_ref: str | None = None) -> Any:
    try:
        import anthropic  # noqa: PLC0415 - optional dependency
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise LLMError("install the 'anthropic' extra: pip install 'opsmith[anthropic]'") from exc
    key = resolve_credential(api_key_ref) if api_key_ref else None
    # With no explicit key the SDK resolves ANTHROPIC_API_KEY / auth token / ant profile.
    return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()


def fallback_kwargs() -> dict[str, Any]:
    mode = os.environ.get("OPSMITH_ANTHROPIC_FALLBACKS", "default")
    if mode in {"off", "false", "0", ""}:
        return {}
    return {"extra_headers": {"anthropic-beta": FALLBACK_BETA}, "extra_body": {"fallbacks": mode}}


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, client: Any | None = None, api_key_ref: str | None = None) -> None:
        self._client = client
        self._api_key_ref = api_key_ref

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = make_client(self._api_key_ref)
        return self._client

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        response = self.client.messages.parse(
            model=request.model,
            max_tokens=request.max_tokens,
            system=request.system,
            messages=[{"role": "user", "content": request.user}],
            output_format=request.schema,
            output_config={"effort": request.effort},
            **fallback_kwargs(),
        )
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise LLMRefusal(f"model declined stage {request.stage} (category={category})")
        if response.stop_reason == "max_tokens":
            raise LLMOutputError(f"stage {request.stage}: output truncated at max_tokens")
        usage = response.usage
        parsed = getattr(response, "parsed_output", None)
        if parsed is not None:
            data: dict[str, Any] | str = parsed.model_dump(mode="json")
        else:
            data = next((b.text for b in response.content if b.type == "text"), "")
        return ProviderResponse(
            data=data,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            model=str(getattr(response, "model", request.model) or request.model),
        )
