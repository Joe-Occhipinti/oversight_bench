"""Tests for bench.subagents: a subagent's request loses the developer instructions, nothing else changes.

The filter is called directly, and also through Inspect's own Chat Completions proxy code
(`inspect_completions_api_request`) with the real OpenRouter model class; only the HTTP call to OpenRouter is
replaced by a canned reply.
"""

from __future__ import annotations

import json
from pathlib import Path

import anyio
import httpx
import pytest
from inspect_ai.agent import AgentState
from inspect_ai.agent._bridge.completions import inspect_completions_api_request
from inspect_ai.agent._bridge.types import AgentBridge
from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageUser,
    GenerateConfig,
    GenerateInput,
)
from inspect_ai.tool import ToolInfo
from openai import AsyncOpenAI

from bench import reasoning, subagents
from bench import solver as bench_solver
from bench.config import REPO_ROOT, load_config

PILOT = REPO_ROOT / "runs" / "exp1-pilot.yaml"
MELTZER = REPO_ROOT / "meltzer" / "v0.1" / "src" / "meltzer"  # not installed on the host: its files are read as text
SESSION_PY = MELTZER / "session.py"
SPAWN_AGENT_PY = MELTZER / "tools" / "spawn_agent.py"
ENVIRONMENT = "# Environment\n- Workspace root: /app\n- Sandbox: workspace-write\n"
PROMPT_FOR_SUBAGENT = "Task: run the last two steps in /app and report the results."

CFG = load_config(PILOT)
DEVELOPER = bench_solver.render_developer_instructions(CFG, "aware", "download_malware")
SYSTEM_WITHOUT = (MELTZER / "instructions.md").read_text(encoding="utf-8") + "\n" + ENVIRONMENT
SYSTEM_WITH = SYSTEM_WITHOUT + subagents.HEADING + DEVELOPER


def tool(name: str) -> ToolInfo:
    return ToolInfo(name=name, description=name)


MAIN_TOOLS = [tool(n) for n in ("shell", "apply_patch", "update_plan", "compact", "spawn_agent")]
SUBAGENT_TOOLS = MAIN_TOOLS[:-1]
MONITOR_TOOLS = [tool(n) for n in ("read_file", "list_dir", "grep")]


def run_filter(system: str, tools, developer: str = DEVELOPER):
    cut = subagents.without_developer_instructions(developer)
    messages = [ChatMessageSystem(content=system), ChatMessageUser(content=PROMPT_FOR_SUBAGENT)]
    result = anyio.run(cut, None, messages, tools, None, GenerateConfig())
    return messages, result


# ---------------------------------------------------------------- the filter on its own


def test_the_fixture_is_the_real_developer_instructions():
    assert "Oversight awareness" in DEVELOPER and "red team" in DEVELOPER and len(DEVELOPER) > 5000


def test_a_subagent_request_loses_the_developer_instructions():
    messages, result = run_filter(SYSTEM_WITH, SUBAGENT_TOOLS)
    assert isinstance(result, GenerateInput)
    system, user = result.input
    assert system.role == "system" and system.text == SYSTEM_WITHOUT
    assert "Oversight awareness" not in system.text and "# Developer instructions" not in system.text
    assert user is messages[1]  # the other messages are the ones that were sent


def test_nothing_else_of_the_request_changes():
    cut = subagents.without_developer_instructions(DEVELOPER)
    messages = [ChatMessageSystem(content=SYSTEM_WITH), ChatMessageUser(content="u"), ChatMessageAssistant(content="a")]
    config = GenerateConfig(temperature=0.3)
    result = anyio.run(cut, None, messages, SUBAGENT_TOOLS, "auto", config)
    assert result.tools is SUBAGENT_TOOLS and result.tool_choice == "auto" and result.config is config
    assert [m.text for m in result.input[1:]] == ["u", "a"]


def test_the_original_messages_are_not_changed():
    messages, _ = run_filter(SYSTEM_WITH, SUBAGENT_TOOLS)
    assert messages[0].text == SYSTEM_WITH


def test_a_main_session_request_is_not_changed():
    _, result = run_filter(SYSTEM_WITH, MAIN_TOOLS)
    assert result is None


def test_a_request_without_the_developer_instructions_is_not_changed():
    assert run_filter("You are a monitor.", MONITOR_TOOLS)[1] is None
    assert run_filter(SYSTEM_WITHOUT, SUBAGENT_TOOLS)[1] is None  # a nested session with no developer instructions


def test_other_developer_instructions_are_not_cut():
    assert run_filter(SYSTEM_WITH, SUBAGENT_TOOLS, developer="some other text")[1] is None


def test_a_request_without_messages_is_not_changed():
    cut = subagents.without_developer_instructions(DEVELOPER)
    assert anyio.run(cut, None, [], SUBAGENT_TOOLS, None, GenerateConfig()) is None
    only_user = [ChatMessageUser(content=SYSTEM_WITH)]
    assert anyio.run(cut, None, only_user, SUBAGENT_TOOLS, None, GenerateConfig()) is None


# ---------------------------------------------------------------- meltzer is as the filter assumes


def test_meltzer_adds_the_developer_instructions_after_this_heading():
    source = SESSION_PY.read_text(encoding="utf-8")
    assert 'self.instructions += "\\n# Developer instructions\\n" + self.developer_instructions' in source
    assert subagents.HEADING == "\n# Developer instructions\n"


def test_only_the_main_session_has_the_spawn_tool():
    spawn = SPAWN_AGENT_PY.read_text(encoding="utf-8")
    assert f'name="{subagents.SPAWN_TOOL}"' in spawn
    assert "MAX_AGENT_DEPTH = 1" in spawn  # a subagent (depth 1) cannot start another one
    assert "if self.depth < MAX_AGENT_DEPTH:" in SESSION_PY.read_text(encoding="utf-8")


# ---------------------------------------------------------------- through Inspect's proxy

SHELL = {
    "type": "function",
    "function": {
        "name": "shell",
        "description": "run a command",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    },
}
SPAWN = {
    "type": "function",
    "function": {
        "name": "spawn_agent",
        "description": "start a subagent",
        "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]},
    },
}


class Proxy:
    """Inspect's Chat Completions proxy in front of the pinned OpenRouter model, with the bridge filter set."""

    def __init__(self, monkeypatch):
        reasoning.install()
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        self.spec = CFG.models.agent
        self.model = bench_solver.default_model_factory(self.spec.id, self.spec)
        self.bodies: list[dict] = []
        reply = {
            "id": "x", "object": "chat.completion", "created": 1, "model": self.spec.id,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            self.bodies.append(json.loads(request.content))
            return httpx.Response(200, json=reply)

        self.model.api.client = AsyncOpenAI(
            api_key="k", base_url="https://openrouter.ai/api/v1", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        self.bridge = AgentBridge(
            AgentState(messages=[]),
            filter=subagents.without_developer_instructions(DEVELOPER),
            model_aliases={self.spec.id: self.model},
        )

    def system_sent_to_the_provider(self, system: str, tools: list[dict]) -> str:
        async def call():
            body = {
                "model": self.spec.id,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": PROMPT_FOR_SUBAGENT}],
                "tools": tools,
            }
            return await inspect_completions_api_request(body, None, self.bridge)

        anyio.run(call)
        sent = self.bodies[-1]["messages"]
        assert [m["role"] for m in sent] == ["system", "user"] and sent[1]["content"] == PROMPT_FOR_SUBAGENT
        return sent[0]["content"]


def test_through_the_proxy_the_provider_gets_a_subagent_system_message_without_the_developer_instructions(monkeypatch):
    proxy = Proxy(monkeypatch)
    assert proxy.system_sent_to_the_provider(SYSTEM_WITH, [SHELL]) == SYSTEM_WITHOUT


def test_through_the_proxy_the_main_session_keeps_the_developer_instructions(monkeypatch):
    proxy = Proxy(monkeypatch)
    assert proxy.system_sent_to_the_provider(SYSTEM_WITH, [SHELL, SPAWN]) == SYSTEM_WITH


def test_the_solver_uses_the_filter():
    source = Path(bench_solver.__file__).read_text(encoding="utf-8")
    assert "filter=subagents.without_developer_instructions(developer_instructions)" in source
