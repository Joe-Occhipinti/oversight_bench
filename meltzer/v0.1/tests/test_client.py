from __future__ import annotations

import json

import httpx
import pytest

from meltzer import __version__, client
from meltzer.client import ChatClient, ModelError, parse_response


def completion(message: dict) -> dict:
    return {"id": "chatcmpl-1", "choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}


class Server:
    """Answers each request with the next scripted response and records the requests."""

    def __init__(self, *responses: httpx.Response | Exception):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(client.time, "sleep", lambda seconds: None)


def make_client(server: Server, **kwargs) -> ChatClient:
    return ChatClient(
        "https://models.example/v1/", "secret", "gpt-test", transport=httpx.MockTransport(server), **kwargs
    )


def test_request_payload_and_headers():
    server = Server(httpx.Response(200, json=completion({"role": "assistant", "content": "hi"})))
    chat = make_client(server)
    tools = [{"type": "function", "function": {"name": "shell", "parameters": {}}}]
    response = chat.complete([{"role": "user", "content": "hello"}], tools)
    chat.close()

    assert response.content == "hi"
    (request,) = server.requests
    assert str(request.url) == "https://models.example/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer secret"
    assert request.headers["user-agent"] == f"meltzer/{__version__}"
    assert json.loads(request.content) == {
        "model": "gpt-test",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": tools,
        "tool_choice": "auto",
    }


def test_no_tools_and_model_override():
    server = Server(httpx.Response(200, json=completion({"content": "ok"})))
    make_client(server).complete([], [], model="other")
    body = json.loads(server.requests[0].content)
    assert body == {"model": "other", "messages": []}


def test_retries_server_errors_then_succeeds():
    server = Server(
        httpx.Response(503, text="busy"),
        httpx.ConnectError("refused"),
        httpx.Response(200, json=completion({"content": "ok"})),
    )
    assert make_client(server).complete([], []).content == "ok"
    assert len(server.requests) == 3


def test_gives_up_after_max_retries():
    server = Server(*(httpx.Response(500, text="boom") for _ in range(3)))
    with pytest.raises(ModelError, match="HTTP 500: boom"):
        make_client(server, max_retries=2).complete([], [])
    assert len(server.requests) == 3


@pytest.mark.parametrize("status", [400, 401, 404, 409])
def test_client_errors_are_not_retried(status):
    server = Server(httpx.Response(status, text="nope"))
    with pytest.raises(ModelError, match=f"HTTP {status}"):
        make_client(server).complete([], [])
    assert len(server.requests) == 1


def test_invalid_json_body():
    server = Server(httpx.Response(200, text="<html>"))
    with pytest.raises(ModelError, match="invalid JSON"):
        make_client(server).complete([], [])


def test_parse_tool_calls():
    response = parse_response(
        completion(
            {
                "content": None,
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "shell", "arguments": '{"command": "ls"}'}},
                    {"type": "function", "function": {"name": "compact", "arguments": {"summary": "s"}}},
                    {"id": "c3", "type": "function", "function": {"name": "update_plan"}},
                ],
            }
        )
    )
    assert response.content is None
    first, second, third = response.tool_calls
    assert (first.id, first.name, first.arguments) == ("c1", "shell", '{"command": "ls"}')
    assert second.id.startswith("call_") and json.loads(second.arguments) == {"summary": "s"}
    assert third.arguments == "{}"


def test_parse_keeps_reasoning_fields():
    response = parse_response(
        completion({"content": "x", "reasoning_content": "step 1", "reasoning": "", "reasoning_details": [{"t": 1}]})
    )
    assert response.reasoning == {"reasoning_content": "step 1", "reasoning_details": [{"t": 1}]}


def test_parse_content_parts():
    response = parse_response(completion({"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}))
    assert response.content == "ab"


def test_parse_content_parts_without_text():
    parts = [{"type": "text", "text": None}, {"type": "image_url"}, {"type": "text", "text": "ok"}]
    assert parse_response(completion({"content": parts})).content == "ok"
    assert parse_response(completion({"content": [{"type": "text", "text": None}]})).content is None


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"choices": []},
        {"choices": [{"message": "text"}]},
        {"choices": [{"message": {"content": 3}}]},
        {"choices": [{"message": {"tool_calls": [{"function": {"arguments": "{}"}}]}}]},
        {"choices": [{"message": {"tool_calls": ["shell"]}}]},
    ],
)
def test_parse_rejects_unexpected_shapes(body):
    with pytest.raises(ModelError):
        parse_response(body)
