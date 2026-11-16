"""Claude provider using the official ``anthropic`` SDK (optional extra: ``opsmith[anthropic]``).

Structured stages send the stage schema as ``output_config.format`` (the same request body
``client.messages.parse(output_format=...)`` builds), so the API constrains the response to it.
They call ``messages.create``, not ``messages.parse``: ``parse`` validates the reply inside the
SDK and raises before the stop reason or the billed usage can be read, so a refusal, a
truncation or a value outside a schema bound would skip the usage ledger and the gateway's
corrective retry. The gateway validates the text instead.
Server-side refusal fallbacks are enabled by default (``OPSMITH_ANTHROPIC_FALLBACKS``).
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel

from opsmith.config import resolve_credential
from opsmith.llm.base import LLMError, LLMOutputError, LLMRefusal, ProviderRequest, ProviderResponse

FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _sdk() -> Any:
    try:
        import anthropic  # noqa: PLC0415 - optional dependency
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise LLMError("install the 'anthropic' extra: pip install 'opsmith[anthropic]'") from exc
    return anthropic


def output_format(schema: type[BaseModel]) -> dict[str, Any]:
    """The ``output_config.format`` that ``messages.parse(output_format=schema)`` would send."""
    return {"type": "json_schema", "schema": _sdk().transform_schema(schema.model_json_schema())}


def make_client(api_key_ref: str | None = None) -> Any:
    anthropic = _sdk()
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
        response = self.client.messages.create(
            model=request.model,
            max_tokens=request.max_tokens,
            system=request.system,
            messages=[{"role": "user", "content": request.user}],
            output_config={"effort": request.effort, "format": output_format(request.schema)},
            **fallback_kwargs(),
        )
        usage = getattr(response, "usage", None)
        billed = ProviderResponse(
            data="",
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            model=str(getattr(response, "model", request.model) or request.model),
        )
        if response.stop_reason in {"refusal", "max_tokens"}:
            # The API billed these responses; the error carries the usage to the gateway.
            error: LLMError
            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                error = LLMRefusal(f"model declined stage {request.stage} (category={category})")
            else:
                error = LLMOutputError(f"stage {request.stage}: output truncated at max_tokens")
            error.usage = billed
            raise error
        # Validated by the gateway, which asks once more when the text does not fit the schema.
        billed.data = next((b.text for b in response.content if b.type == "text"), "")
        return billed
