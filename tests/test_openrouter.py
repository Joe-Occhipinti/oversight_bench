"""bench.openrouter: API key, provider check, POST with retries (HTTP mocked)."""

from __future__ import annotations

import json

import anyio
import httpx
import pytest

from bench import openrouter as O

OK = {"id": "r1", "provider": "Wafer", "choices": [{"message": {"content": "text"}}]}


def test_load_api_key_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("# c\nexport OPENROUTER_API_KEY='sk-or-abc'\n", encoding="utf-8")
    assert O.load_api_key(env) == "sk-or-abc"
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    assert O.load_api_key(env) == "from-env"
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert O.load_api_key(tmp_path / "missing") is None


@pytest.mark.parametrize(
    "served, config, expected",
    [
        ("Wafer", {"order": ["wafer"]}, True),
        ("Together", {"order": ["wafer"]}, False),
        (None, {"order": ["wafer"]}, False),
        ("Anything", {"order": []}, True),
        ("DeepInfra", {"order": ["deepinfra/fp8"]}, True),
    ],
)
def test_served_by_configured(served, config, expected):
    assert O.served_by_configured(served, config) is expected


def post(responses):
    """Run post_chat against a transport that returns `responses` in order; return (result, calls, bodies)."""
    bodies = []
    it = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        item = next(it)
        if isinstance(item, Exception):
            raise item
        return item

    calls: list[dict] = []

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await O.post_chat(client, {"model": "m"}, "key", calls, backoff_s=0)

    return anyio.run(go), calls, bodies


def test_post_returns_the_response_and_logs_the_call():
    data, calls, bodies = post([httpx.Response(200, json=OK)])
    assert data == OK and bodies == [{"model": "m"}]
    assert calls == [{"status_code": 200, "response": OK, "error": None}]


def test_post_retries_transient_failures():
    data, calls, _ = post([
        httpx.Response(503, text="down"),
        httpx.Response(429, text="slow"),
        httpx.ConnectError("no route"),
        httpx.Response(200, json={"error": "provider failed"}),
        httpx.Response(200, json=OK),
    ])
    assert data == OK
    assert [c["error"] for c in calls[:4]] == ["HTTP 503", "HTTP 429", "transport error: ConnectError: no route", "no choices in response"]


def test_post_gives_up_after_the_retries():
    data, calls, _ = post([httpx.Response(500, text="x")] * O.HTTP_TRIES)
    assert data is None and len(calls) == O.HTTP_TRIES


def test_post_raises_on_a_status_a_retry_does_not_fix():
    with pytest.raises(O.OpenRouterError, match="HTTP 401"):
        post([httpx.Response(401, text="bad key")])
