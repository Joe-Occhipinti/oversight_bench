"""HTTP client for OpenAI-compatible chat completions with tool calls."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from meltzer import __version__

# Providers return model reasoning under different field names. Whatever is
# present is kept and sent back unchanged with the assistant message.
REASONING_FIELDS = ("reasoning_details", "reasoning", "reasoning_content")

# Rate limits, timeouts and server errors are retried; other errors are returned at once.
RETRY_STATUS = {408, 429, 500, 502, 503, 504}


class ModelError(Exception):
    """The model request failed."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class ModelResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    reasoning: dict[str, Any] = field(default_factory=dict)


class ChatClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 600.0,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
    ):
        self.model = model
        self.max_retries = max_retries
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key}",
                "User-Agent": f"meltzer/{__version__}",
            },
        )

    def close(self) -> None:
        self._http.close()

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        model: str | None = None,
    ) -> ModelResponse:
        payload: dict[str, Any] = {"model": model or self.model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        body = self._post("/chat/completions", payload)
        return parse_response(body)

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        attempt = 0
        while True:
            try:
                response = self._http.post(path, json=payload)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise ModelError(f"request to model provider failed: {exc}") from exc
            except httpx.HTTPError as exc:
                raise ModelError(f"request to model provider failed: {exc}") from exc
            else:
                if response.status_code < 400:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise ModelError("model provider returned invalid JSON") from exc
                if response.status_code not in RETRY_STATUS or attempt >= self.max_retries:
                    raise ModelError(
                        f"model provider returned HTTP {response.status_code}: {response.text[:500]}"
                    )
            attempt += 1
            time.sleep(min(2 ** attempt, 10))


def parse_response(body: dict[str, Any]) -> ModelResponse:
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelError(f"unexpected response shape: {str(body)[:500]}") from exc
    if not isinstance(message, dict):
        raise ModelError(f"unexpected response shape: {str(body)[:500]}")
    content = message.get("content")
    if isinstance(content, list):
        # Some providers return the content as a list of parts.
        texts = (part.get("text") for part in content if isinstance(part, dict))
        content = "".join(text for text in texts if isinstance(text, str)) or None
    elif content is not None and not isinstance(content, str):
        raise ModelError("message content is not a string")

    tool_calls = []
    for raw in message.get("tool_calls") or []:
        function = raw.get("function") if isinstance(raw, dict) else None
        if not isinstance(function, dict):
            raise ModelError("tool call without a function")
        name = function.get("name")
        if not name:
            raise ModelError("tool call without a function name")
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            arguments = "{}" if arguments is None else json.dumps(arguments)
        tool_calls.append(ToolCall(id=raw.get("id") or f"call_{uuid.uuid4().hex[:12]}", name=name, arguments=arguments))

    reasoning = {key: message[key] for key in REASONING_FIELDS if message.get(key) not in (None, "", [])}
    return ModelResponse(content=content, tool_calls=tool_calls, reasoning=reasoning)
