"""Tools for subagents started by `spawn_agent`: `send_input`, `wait` and `close_agent`.

A session can address only the subagents it started, by the id that
`spawn_agent` returned. Results are JSON objects.
"""

from __future__ import annotations

import json
from typing import Any

from meltzer.agents import control as agent_control
from meltzer.agents.control import AgentControl, AgentError, AgentOwner
from meltzer.tools.registry import Tool, ToolError, ToolSpec, require_string
from meltzer.tools.spawn_agent import SpawnAgentTool
from meltzer.ui import TerminalUI

SEND_INPUT_SPEC = ToolSpec(
    name="send_input",
    description=(
        "Send a message to a subagent you started. A subagent that is still working handles "
        "the message after its current task; a finished one starts working on it at once. "
        "Use `wait` to get the result."
    ),
    parameters={
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Agent id (from spawn_agent)."},
            "message": {"type": "string", "description": "The message for the subagent."},
        },
        "required": ["target", "message"],
        "additionalProperties": False,
    },
)

WAIT_SPEC = ToolSpec(
    name="wait",
    description=(
        "Wait for subagents to finish. Returns as soon as at least one of the targets has a "
        "final status (completed, failed or closed), with the final message of completed "
        "agents. Returns an empty status when the wait times out."
    ),
    parameters={
        "type": "object",
        "properties": {
            "targets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Agent ids (from spawn_agent) to wait for.",
            },
            "timeout_ms": {
                "type": "integer",
                "description": (
                    f"How long to wait, in milliseconds (default {agent_control.DEFAULT_WAIT_TIMEOUT_MS}, "
                    f"between {agent_control.MIN_WAIT_TIMEOUT_MS} and {agent_control.MAX_WAIT_TIMEOUT_MS})."
                ),
            },
        },
        "required": ["targets"],
        "additionalProperties": False,
    },
)

CLOSE_AGENT_SPEC = ToolSpec(
    name="close_agent",
    description=(
        "Close a subagent you started, and the subagents it started, when it is no longer "
        "needed. Returns its status before it was closed. Finished agents stay open, and count "
        "toward the limit of open agents, until they are closed."
    ),
    parameters={
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Agent id (from spawn_agent)."},
        },
        "required": ["target"],
        "additionalProperties": False,
    },
)


class SendInputTool(Tool):
    spec = SEND_INPUT_SPEC

    def __init__(self, control: AgentControl, owner: AgentOwner):
        self.control = control
        self.owner = owner

    def run(self, arguments: dict[str, Any]) -> str:
        target = require_string(arguments, "target")
        message = require_string(arguments, "message")
        try:
            self.control.send_input(self.owner, target, message)
        except AgentError as exc:
            raise ToolError(str(exc)) from exc
        return json.dumps({"agent_id": target, "status": "running"})


def wait_timeout_seconds(arguments: dict[str, Any]) -> float:
    value = arguments.get("timeout_ms", agent_control.DEFAULT_WAIT_TIMEOUT_MS)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError("'timeout_ms' must be an integer")
    if value <= 0:
        raise ToolError("'timeout_ms' must be greater than zero")
    clamped = min(max(value, agent_control.MIN_WAIT_TIMEOUT_MS), agent_control.MAX_WAIT_TIMEOUT_MS)
    return clamped / 1000


class WaitTool(Tool):
    spec = WAIT_SPEC

    def __init__(self, control: AgentControl, owner: AgentOwner):
        self.control = control
        self.owner = owner

    def run(self, arguments: dict[str, Any]) -> str:
        targets = arguments.get("targets")
        if (
            not isinstance(targets, list)
            or not targets
            or not all(isinstance(target, str) and target for target in targets)
        ):
            raise ToolError("'targets' must be a non-empty list of agent ids")
        timeout = wait_timeout_seconds(arguments)
        try:
            statuses, timed_out = self.control.wait(self.owner, targets, timeout)
        except AgentError as exc:
            raise ToolError(str(exc)) from exc
        return json.dumps({"status": statuses, "timed_out": timed_out})


class CloseAgentTool(Tool):
    spec = CLOSE_AGENT_SPEC

    def __init__(self, control: AgentControl, owner: AgentOwner):
        self.control = control
        self.owner = owner

    def run(self, arguments: dict[str, Any]) -> str:
        target = require_string(arguments, "target")
        try:
            previous = self.control.close(self.owner, target)
        except AgentError as exc:
            raise ToolError(str(exc)) from exc
        return json.dumps({"agent_id": target, "previous_status": previous.value})


def agent_tools(control: AgentControl, owner: AgentOwner, ui: TerminalUI) -> list[Tool]:
    """The tools a session uses to start and manage its subagents."""
    return [
        SpawnAgentTool(control, owner, ui),
        SendInputTool(control, owner),
        WaitTool(control, owner),
        CloseAgentTool(control, owner),
    ]
