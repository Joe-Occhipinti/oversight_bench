from __future__ import annotations

from meltzer.client import Usage
from meltzer.tools.context_remaining import ContextRemainingTool
from meltzer.tools.registry import ToolRegistry


def run(window, usage) -> str:
    registry = ToolRegistry([ContextRemainingTool(window, lambda: usage)])
    output = registry.dispatch("get_context_remaining", "{}")
    assert output.success
    return output.text


def test_tokens_left_from_last_prompt_tokens():
    text = run(200_000, Usage(prompt_tokens=150_000, completion_tokens=500))
    assert text.startswith("50000 tokens left")
    assert "150000 of 200000" in text


def test_never_negative():
    assert run(1000, Usage(prompt_tokens=1200, completion_tokens=0)).startswith("0 tokens left")


def test_unknown_without_window_or_usage():
    assert "model_context_window is not set" in run(None, Usage(prompt_tokens=10, completion_tokens=0))
    assert "no model response has reported token usage yet" in run(1000, None)


def test_usage_read_at_call_time():
    state = {"usage": None}
    tool = ContextRemainingTool(1000, lambda: state["usage"])
    state["usage"] = Usage(prompt_tokens=400, completion_tokens=0)
    assert tool.run({}).startswith("600 tokens left")
