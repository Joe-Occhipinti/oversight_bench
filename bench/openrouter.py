"""Direct OpenRouter calls made from the host (the reflection call, the calibration): API key, provider check, and a
POST with retries on transient failures."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

import httpx

from bench.config import REPO_ROOT

API_URL = "https://openrouter.ai/api/v1/chat/completions"
ENV_FILE = REPO_ROOT / ".env"
KEY_NAME = "OPENROUTER_API_KEY"

HTTP_TRIES = 6  # on 408 / 429 / 5xx / network errors / error bodies
BACKOFF_S = 2.0  # wait before HTTP retry n (0-based) = BACKOFF_S * 2**n
RETRYABLE_STATUS = {408, 429}
TIMEOUT_S = 600.0


class OpenRouterError(RuntimeError):
    """A failure that a retry does not fix (bad key, unknown model, bad request)."""


def env_value(key: str, env_file: Path | None = None) -> str | None:
    """`key` from the environment, else from the .env file. None if absent."""
    env_file = ENV_FILE if env_file is None else env_file
    if os.environ.get(key):
        return os.environ[key]
    if not env_file.is_file():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            return value.strip().strip("'\"") or None
    return None


def load_api_key(env_file: Path | None = None) -> str | None:
    """OPENROUTER_API_KEY from the environment, else from the .env file. None if absent."""
    return env_value(KEY_NAME, env_file)


def _slug(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def served_by_configured(response_provider: Any, provider_config: Any) -> bool:
    """True when the provider named in the response is one of the configured providers (`order`)."""
    order = provider_config.get("order", []) if isinstance(provider_config, dict) else [provider_config]
    slugs = [_slug(str(p).split("/")[0]) for p in order if p]
    if not slugs:
        return True
    return bool(response_provider) and any(_slug(response_provider).startswith(s) for s in slugs)


def call_record(**fields: Any) -> dict:
    """One entry of a call log: what was sent back, or why the call failed."""
    record = {"status_code": None, "response": None, "error": None}
    record.update(fields)
    return record


async def post_chat(
    client: httpx.AsyncClient,
    body: dict,
    api_key: str,
    calls: list[dict],
    *,
    tries: int = HTTP_TRIES,
    backoff_s: float = BACKOFF_S,
) -> dict | None:
    """POST `body` to the chat completions API, retrying transient failures; append every attempt to `calls`.

    Returns the response JSON (with `choices`), or None when the retries are exhausted. Raises OpenRouterError on
    an HTTP status that a retry does not fix.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    for n in range(tries):
        if n:
            await asyncio.sleep(backoff_s * 2 ** (n - 1))
        try:
            resp = await client.post(API_URL, json=body, headers=headers, timeout=TIMEOUT_S)
        except httpx.TransportError as e:
            calls.append(call_record(error=f"transport error: {type(e).__name__}: {e}"))
            continue
        if resp.status_code != 200:
            calls.append(call_record(status_code=resp.status_code, response=resp.text[:2000], error=f"HTTP {resp.status_code}"))
            if resp.status_code in RETRYABLE_STATUS or resp.status_code >= 500:
                continue
            raise OpenRouterError(f"OpenRouter HTTP {resp.status_code}: {resp.text[:500]}")
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict) or not data.get("choices"):
            # OpenRouter can return 200 with an error body when the provider fails.
            calls.append(call_record(status_code=200, response=resp.text[:2000], error="no choices in response"))
            continue
        calls.append(call_record(status_code=200, response=data))
        return data
    return None
