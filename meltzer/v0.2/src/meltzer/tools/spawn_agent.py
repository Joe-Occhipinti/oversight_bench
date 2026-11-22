"""The `spawn_agent` tool: hand a self-contained task to a subagent.

A subagent is a new session in the same process, created from the same
configuration and directory as the session that started it. An `agent_type`
selects a role (see meltzer.agents.roles). The tool returns the subagent's
final message; with `background`, it returns the subagent's id at once, and
the result comes from `wait` (see meltzer.tools.agents).
"""

from __future__ import annotations

import json
from typing import Any

from meltzer.agents.control import AgentControl, AgentError, AgentOwner
from meltzer.agents.roles import RoleSet
from meltzer.client import ModelError
from meltzer.config.loader import ConfigError
from meltzer.tools.registry import Tool, ToolError, ToolSpec, optional_bool, optional_string, require_string
from meltzer.ui import TerminalUI


def spawn_agent_spec(roles: RoleSet) -> ToolSpec:
    return ToolSpec(
        name="spawn_agent",
        description=(
            "Start a subagent for a self-contained part of the task, such as investigating a "
            "question or making a focused change. The subagent works in the same workspace with "
            "the same tools, but does not see this conversation: "
            "write a complete task description in `prompt`, including relevant file paths and "
            "constraints. Returns the subagent's final message. With `background`, returns the "
            "subagent's id at once; use `wait` to get its result, `send_input` to give it more "
            "work and `close_agent` when it is no longer needed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The task for the subagent."},
                "agent_type": {
                    "type": "string",
                    "description": "Role for the subagent (default: `default`). " + roles.describe(),
                },
                "background": {
                    "type": "boolean",
                    "description": "Return the subagent's id at once instead of waiting for its result.",
                },
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    )


class SpawnAgentTool(Tool):
    def __init__(self, control: AgentControl, owner: AgentOwner, ui: TerminalUI):
        self.control = control
        self.owner = owner
        self.ui = ui
        self.spec = spawn_agent_spec(control.roles)

    def run(self, arguments: dict[str, Any]) -> str:
        prompt = require_string(arguments, "prompt")
        agent_type = optional_string(arguments, "agent_type")
        background = optional_bool(arguments, "background")
        try:
            agent = self.control.start(self.owner, agent_type)
        except AgentError as exc:
            raise ToolError(f"subagent not started: {exc}") from exc
        except ConfigError as exc:
            raise ToolError(f"subagent not started: {exc}") from exc
        label = agent.label
        if background:
            self.control.run_in_background(agent, prompt)
            self.ui.info(f"[{label}] started in the background")
            return json.dumps({"agent_id": agent.id, "agent_type": agent.role.name, "status": "running"})
        self.ui.info(f"[{label}] started")
        try:
            final = self.control.run(agent, prompt)
        except ModelError as exc:
            raise ToolError(f"{label} failed: {exc}") from exc
        self.ui.info(f"[{label}] finished")
        return final or f"{label} finished without a final message."
