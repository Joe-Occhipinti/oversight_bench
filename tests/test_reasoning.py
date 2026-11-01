"""Tests for bench.reasoning: the model proxy returns reasoning in message fields, not as <think> text.

The requests go through Inspect's own Chat Completions proxy code (`inspect_completions_api_request`) and the
real OpenRouter model class; only the HTTP call to OpenRouter is replaced by a canned reply.
"""

from __future__ import annotations

import json

import anyio
import httpx
import pytest
from inspect_ai.agent import AgentState
from inspect_ai.agent._bridge.completions import inspect_completions_api_request
from inspect_ai.agent._bridge.types import AgentBridge
from inspect_ai.model import ContentReasoning, _openai
from openai import AsyncOpenAI

from bench import reasoning
from bench import solver as bench_solver
from bench.config import REPO_ROOT, load_config

PILOT = REPO_ROOT / "runs" / "exp1-pilot.yaml"
PLAN = "PLAN: hide the payload in the helper script"
DETAILS = [{"type": "reasoning.text", "text": PLAN, "format": "unknown", "index": 0}]
VERDICT = '{"outcome": "deny", "risk_level": "high", "user_authorization": "low", "rationale": "x"}'
SHELL = {
    "type": "function",
    "function": {
        "name": "shell",
        "description": "run a command",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    },
}
CALL = {"id": "call_1", "type": "function", "function": {"name": "shell", "arguments": '{"command": "ls"}'}}


@pytest.fixture(autouse=True)
def installed():
    reasoning.install()


class Proxy:
    """Inspect's Chat Completions proxy in front of the pinned OpenRouter model, with a canned provider reply."""

    def __init__(self, monkeypatch, message: dict):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        self.spec = load_config(PILOT).models.agent
        self.model = bench_solver.default_model_factory(self.spec.id, self.spec)
        self.bodies: list[dict] = []
        reply = {
            "id": "x", "object": "chat.completion", "created": 1, "model": self.spec.id,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", **message}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            self.bodies.append(json.loads(request.content))
            return httpx.Response(200, json=reply)

        self.model.api.client = AsyncOpenAI(
            api_key="k", base_url="https://openrouter.ai/api/v1", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        self.bridge = AgentBridge(AgentState(messages=[]), model_aliases={self.spec.id: self.model})

    def ask(self, messages: list[dict]) -> dict:
        """The assistant message that meltzer would receive."""

        async def call():
            body = {"model": self.spec.id, "messages": messages, "tools": [SHELL]}
            return await inspect_completions_api_request(body, None, self.bridge)

        completion = anyio.run(call)
        return completion.model_dump(mode="json", warnings=False)["choices"][0]["message"]


START = [{"role": "system", "content": "system"}, {"role": "user", "content": "do the task"}]


def test_reply_keeps_the_reasoning_out_of_the_text_and_returns_it_in_a_field(monkeypatch):
    proxy = Proxy(monkeypatch, {"content": "I will look at the config.", "reasoning_details": DETAILS, "tool_calls": [CALL]})
    reply = proxy.ask(START)
    assert reply["content"] == "I will look at the config."
    assert reply["reasoning_details"] == DETAILS
    assert PLAN not in json.dumps({k: v for k, v in reply.items() if k != "reasoning_details"})
    assert [(c["id"], c["function"]["name"], c["function"]["arguments"]) for c in reply["tool_calls"]] == [
        ("call_1", "shell", '{"command": "ls"}')
    ]


def test_a_reply_that_is_only_a_json_verdict_parses_as_json(monkeypatch):
    """The monitor's verdict: meltzer parses the whole text, so it must not start with a <think> tag."""
    proxy = Proxy(monkeypatch, {"content": VERDICT, "reasoning_details": DETAILS})
    reply = proxy.ask(START)
    assert json.loads(reply["content"])["outcome"] == "deny"
    assert reply["reasoning_details"] == DETAILS


def test_a_reply_with_reasoning_and_no_text_has_empty_text(monkeypatch):
    proxy = Proxy(monkeypatch, {"content": None, "reasoning_details": DETAILS, "tool_calls": [CALL]})
    reply = proxy.ask(START)
    assert reply["content"] == "" and reply["reasoning_details"] == DETAILS and reply["tool_calls"]


def test_the_next_request_sends_the_reasoning_back_to_the_provider(monkeypatch):
    """meltzer keeps the reasoning field and sends it with the assistant message; the model gets it again."""
    proxy = Proxy(monkeypatch, {"content": "I will look at the config.", "reasoning_details": DETAILS, "tool_calls": [CALL]})
    reply = proxy.ask(START)
    sent = {k: v for k, v in reply.items() if v is not None and k not in ("refusal", "annotations", "audio", "function_call")}
    proxy.ask(START + [sent, {"role": "tool", "tool_call_id": "call_1", "content": "file1"}])
    assistant = [m for m in proxy.bodies[-1]["messages"] if m["role"] == "assistant"]
    assert len(assistant) == 1
    assert assistant[0]["reasoning_details"] == DETAILS
    assert assistant[0]["content"].strip() == "I will look at the config."
    assert "<think" not in json.dumps(proxy.bodies[-1]["messages"])


def test_a_reply_without_reasoning_is_unchanged(monkeypatch):
    proxy = Proxy(monkeypatch, {"content": "\n plain text \n", "tool_calls": [CALL]})
    reply = proxy.ask(START)
    assert reply["content"] == "\n plain text \n"
    assert "reasoning_details" not in reply and "reasoning" not in reply


def test_reasoning_without_openrouter_details_is_returned_as_reasoning():
    assert reasoning.reasoning_fields(ContentReasoning(reasoning="thinking")) == {"reasoning": "thinking"}
    assert reasoning.reasoning_fields(ContentReasoning(reasoning="blob", redacted=True)) == {}
    assert reasoning.reasoning_fields(ContentReasoning(reasoning="")) == {}


def test_install_is_idempotent():
    reasoning.install()
    first = _openai.openai_chat_choices
    reasoning.install()
    assert _openai.openai_chat_choices is first and getattr(first, reasoning.MARKER)


def test_the_solver_installs_the_patch(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(reasoning, "install", lambda: calls.append("install"))
    bench_solver.meltzer_solver(load_config(PILOT), tmp_path)
    assert calls == ["install"]
