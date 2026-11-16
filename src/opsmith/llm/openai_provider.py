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
import time
from collections.abc import Callable
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
_THINK = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)


# Transient answers worth asking again for, and how: two more tries, 1 s then 2 s (or what the
# endpoint's Retry-After says), never more than half a minute each.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRIES = 2
RETRY_BASE_SECONDS = 1.0
RETRY_MAX_SECONDS = 30.0
CHARS_PER_TOKEN = 4  # the usual rough ratio, used only when an endpoint reports no usage


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
        self.sleep: Callable[[float], None] = time.sleep  # replaced in tests

    def _headers(self) -> dict[str, str]:
        # Resolved per call, so a rotated key is picked up; local servers need none.
        key = resolve_credential(self._api_key_ref) if self._api_key_ref else None
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _post_once(self, payload: dict[str, Any]) -> httpx.Response:
        url = f"{self.base_url}/chat/completions"
        try:
            if self._http is not None:
                return self._http.post(url, json=payload, headers=self._headers())
            with httpx.Client(timeout=_TIMEOUT) as client:
                return client.post(url, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise LLMError(f"model endpoint unreachable: {type(exc).__name__}") from exc

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        """One request, repeated a bounded number of times while the endpoint is busy or
        cannot be reached. Without this a single 429 fails the stage; for inference that is
        the whole slot. A timeout while reading is not repeated: the request may be running."""
        for attempt in range(MAX_RETRIES + 1):
            last = attempt == MAX_RETRIES
            try:
                response = self._post_once(payload)
            except LLMError as exc:
                if last or not isinstance(exc.__cause__, httpx.ConnectError | httpx.ConnectTimeout):
                    raise
                delay = RETRY_BASE_SECONDS * 2**attempt
            else:
                if last or response.status_code not in RETRY_STATUSES:
                    return response
                delay = _retry_after(response) or RETRY_BASE_SECONDS * 2**attempt
            self.sleep(min(delay, RETRY_MAX_SECONDS))
        raise AssertionError("unreachable")  # pragma: no cover - the loop always returns

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
        choice = body["choices"][0] if isinstance(body["choices"], list) else None
        if not isinstance(choice, dict) or not isinstance(choice.get("message") or {}, dict):
            raise LLMError("model endpoint returned no usable choice")
        if not isinstance(body.get("usage"), dict):
            # No usage reported. Zero tokens would make the call free for the budgets, so
            # it is estimated from the size of what was sent and what came back.
            sent = sum(len(str(m.get("content") or "")) for m in payload.get("messages", []))
            received = len(text_of(choice.get("message") or {}))
            body["usage"] = {
                "prompt_tokens": sent // CHARS_PER_TOKEN + 1,
                "completion_tokens": received // CHARS_PER_TOKEN + 1,
            }
        return body


def _error_text(response: httpx.Response) -> str:
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return response.text[:300]
    if isinstance(error, dict):
        return str(error.get("message", ""))[:300]
    return str(error)[:300]


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds the endpoint asks to wait, when it says so as a plain number."""
    try:
        seconds = float(response.headers.get("retry-after", ""))
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _count(value: Any) -> int:
    """A token count as an endpoint may write it: 12, 12.0, "12", or nothing."""
    try:
        return max(int(float(value)), 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def usage_of(body: dict[str, Any]) -> tuple[int, int]:
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return 0, 0
    return _count(usage.get("prompt_tokens")), _count(usage.get("completion_tokens"))


def text_of(message: dict[str, Any]) -> str:
    """The text of a message whose content is a string or a list of typed parts."""
    content = message.get("content")
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "") if isinstance(part, dict) else str(part) for part in content
        )
    return str(content or "")


def strip_fence(text: str) -> str:
    """Unwrap JSON from what models add around it when the server does not enforce the
    format: a leading ``<think>...</think>`` block (reasoning models) and a Markdown fence."""
    text = _THINK.sub("", text, count=1)
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
            # A server without json_schema support rejects the request as invalid. Try the
            # simpler mode once. The switch is only remembered if that attempt works: a
            # request rejected for another reason (too long, unknown model) fails here again
            # and must not cost every later call its schema enforcement.
            if self.json_mode != "json_schema" or exc.status not in {400, 404, 422, 501}:
                raise
            self.json_mode = "json_object"
            try:
                body = self.client.chat(self._payload(request), max_tokens=request.max_tokens)
            except EndpointError:
                self.json_mode = "json_schema"
                raise exc from None
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
        billed.data = strip_fence(text_of(message))
        return billed
