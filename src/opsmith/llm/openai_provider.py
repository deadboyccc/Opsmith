"""Provider for any OpenAI-compatible Chat Completions endpoint.

That covers OpenAI itself and everything that speaks the same HTTP API: Azure OpenAI, Ollama,
vLLM, LM Studio, llama.cpp's server, LiteLLM, OpenRouter, Groq, Mistral, Together and others.
It is plain HTTP (``httpx``): no vendor SDK, nothing to install.

The endpoint is chosen by the operator (``OPSMITH_OPENAI_BASE_URL`` and
``OPSMITH_OPENAI_API_KEY_REF``), never by project configuration, so a project cannot point
model calls at a URL of its choosing.

Servers differ in what they accept, so the client adapts once and remembers:

* structured output is requested as ``response_format: json_schema``; a server that rejects
  that gets ``json_object`` plus the schema in the prompt instead;
* the output limit is sent as ``max_tokens``; a server that insists on
  ``max_completion_tokens`` gets that instead.

Whatever the server returns, the gateway validates it against the stage schema again.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from opsmith.config import resolve_credential
from opsmith.llm.base import (
    LLMError,
    LLMOutputError,
    LLMRefusal,
    ProviderRequest,
    ProviderResponse,
)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
JSON_MODES = ("json_schema", "json_object")
# Model calls can take minutes; the connect timeout stays short so a wrong URL fails fast.
_TIMEOUT = httpx.Timeout(600.0, connect=10.0)
_FENCE = re.compile(r"^\s*```(?:json)?\s*\n(.*?)\n?```\s*$", re.DOTALL)


class EndpointError(LLMError):
    """The endpoint answered with an HTTP error."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"model endpoint returned HTTP {status}: {detail}")
        self.status = status


class ChatClient:
    """``POST {base_url}/chat/completions`` with the compatibility fallbacks described above.

    Shared by the structured-stage provider and the coding agent.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key_ref: str | None = None,
        *,
        http: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key_ref = api_key_ref
        self._http = http
        self._limit_param = "max_tokens"

    def _headers(self) -> dict[str, str]:
        # Resolved per call, so a rotated key is picked up; local servers need none.
        key = resolve_credential(self._api_key_ref) if self._api_key_ref else None
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        url = f"{self.base_url}/chat/completions"
        try:
            if self._http is not None:
                return self._http.post(url, json=payload, headers=self._headers())
            with httpx.Client(timeout=_TIMEOUT) as client:
                return client.post(url, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise LLMError(f"model endpoint unreachable: {type(exc).__name__}") from exc

    def chat(self, payload: dict[str, Any], *, max_tokens: int) -> dict[str, Any]:
        """Send one request and return the decoded response body."""
        response = self._post({**payload, self._limit_param: max_tokens})
        if (
            response.status_code == 400
            and self._limit_param == "max_tokens"
            and "max_completion_tokens" in response.text
        ):
            self._limit_param = "max_completion_tokens"
            response = self._post({**payload, self._limit_param: max_tokens})
        if response.status_code >= 400:
            raise EndpointError(response.status_code, _error_text(response))
        try:
            body = response.json()
        except ValueError as exc:
            raise LLMError("model endpoint returned a body that is not JSON") from exc
        if not isinstance(body, dict) or not body.get("choices"):
            raise LLMError("model endpoint returned no choices")
        return body


def _error_text(response: httpx.Response) -> str:
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return response.text[:300]
    if isinstance(error, dict):
        return str(error.get("message", ""))[:300]
    return str(error)[:300]


def usage_of(body: dict[str, Any]) -> tuple[int, int]:
    usage = body.get("usage") or {}
    return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)


def strip_fence(text: str) -> str:
    """Models that ignore the response format often wrap JSON in a Markdown code fence."""
    match = _FENCE.match(text)
    return match.group(1) if match else text


class OpenAICompatibleProvider:
    name = "openai"

    def __init__(self, client: ChatClient, *, json_mode: str = "json_schema") -> None:
        if json_mode not in JSON_MODES:
            raise ValueError(f"json_mode must be one of {JSON_MODES}")
        self.client = client
        self.json_mode = json_mode

    def _payload(self, request: ProviderRequest) -> dict[str, Any]:
        schema = request.schema.model_json_schema()
        user = request.user
        if self.json_mode == "json_schema":
            response_format: dict[str, Any] = {
                "type": "json_schema",
                "json_schema": {"name": request.schema.__name__, "schema": schema},
            }
        else:
            response_format = {"type": "json_object"}
            user += (
                "\n\nReturn one JSON object that validates against this JSON Schema:\n"
                + json.dumps(schema, separators=(",", ":"))
            )
        return {
            "model": request.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": user},
            ],
            "response_format": response_format,
        }

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        try:
            body = self.client.chat(self._payload(request), max_tokens=request.max_tokens)
        except EndpointError as exc:
            # A server without json_schema support rejects the request as invalid; fall back
            # once and keep using the simpler mode for the rest of this process.
            if self.json_mode != "json_schema" or exc.status not in {400, 404, 422, 501}:
                raise
            self.json_mode = "json_object"
            body = self.client.chat(self._payload(request), max_tokens=request.max_tokens)
        choice = body["choices"][0]
        message = choice.get("message") or {}
        tokens_in, tokens_out = usage_of(body)
        billed = ProviderResponse(
            data="",
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            model=str(body.get("model") or request.model),
        )
        reason = choice.get("finish_reason")
        error: LLMError | None = None
        if message.get("refusal") or reason == "content_filter":
            error = LLMRefusal(f"model declined stage {request.stage}")
        elif reason == "length":
            error = LLMOutputError(f"stage {request.stage}: output truncated at max_tokens")
        if error is not None:
            error.usage = billed  # the call was still billed; the gateway records it
            raise error
        billed.data = strip_fence(str(message.get("content") or ""))
        return billed
