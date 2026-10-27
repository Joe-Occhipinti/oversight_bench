"""The `spawn_agent` tool: hand a self-contained task to a subagent.

A subagent is a new session in the same process, created from the same
configuration and directory as the session that started it. The tool returns
the subagent's final message.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from meltzer.client import ModelError
from meltzer.config.loader import ConfigError
from meltzer.tools.registry import ToolError, ToolSpec, require_string
from meltzer.ui import TerminalUI

# The main session is depth 0. Subagents (depth 1) cannot start subagents.
MAX_AGENT_DEPTH = 1

SPAWN_AGENT_SPEC = ToolSpec(
    name="spawn_agent",
    description=(
        "Start a subagent for a self-contained part of the task, such as investigating a "
        "question or making a focused change. The subagent works in the same workspace with "
        "the same tools, but does not see this conversation: "
        "write a complete task description in `prompt`, including relevant file paths and "
        "constraints. Returns the subagent's final message."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "The task for the subagent."},
        },
        "required": ["prompt"],
        "additionalProperties": False,
    },
)


class Subagent(Protocol):
    id: str

    def run_turn(self, prompt: str) -> str: ...

    def close(self) -> None: ...


class SpawnAgentTool:
    spec = SPAWN_AGENT_SPEC

    def __init__(self, start: Callable[[], Subagent], ui: TerminalUI):
        self.start = start
        self.ui = ui

    def run(self, arguments: dict[str, Any]) -> str:
        prompt = require_string(arguments, "prompt")
        try:
            agent = self.start()
        except ConfigError as exc:
            raise ToolError(f"subagent not started: {exc}") from exc
        label = f"subagent {agent.id[:8]}"
        self.ui.info(f"[{label}] started")
        try:
            final = agent.run_turn(prompt)
        except ModelError as exc:
            raise ToolError(f"{label} failed: {exc}") from exc
        finally:
            agent.close()
        self.ui.info(f"[{label}] finished")
        return final or f"{label} finished without a final message."
