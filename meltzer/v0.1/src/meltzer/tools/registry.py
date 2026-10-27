"""Tool specs sent to the model, and dispatch of the model's tool calls."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Protocol


class ToolError(Exception):
    """A tool call failed; the message is returned to the model."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]

    def to_wire(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class Tool(Protocol):
    spec: ToolSpec

    def run(self, arguments: dict[str, Any]) -> str: ...


@dataclass(frozen=True)
class ToolOutput:
    text: str
    success: bool


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()):
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.spec.name in self._tools:
            raise ValueError(f"tool {tool.spec.name!r} is already registered")
        self._tools[tool.spec.name] = tool

    def specs(self) -> list[dict[str, Any]]:
        return [tool.spec.to_wire() for tool in self._tools.values()]

    def dispatch(self, name: str, raw_arguments: str) -> ToolOutput:
        tool = self._tools.get(name)
        if tool is None:
            return ToolOutput(f"unknown tool: {name}", success=False)
        try:
            arguments = json.loads(raw_arguments) if raw_arguments.strip() else {}
        except json.JSONDecodeError as exc:
            return ToolOutput(f"invalid arguments for {name}: {exc}", success=False)
        if not isinstance(arguments, dict):
            return ToolOutput(f"invalid arguments for {name}: expected a JSON object", success=False)
        try:
            return ToolOutput(tool.run(arguments), success=True)
        except ToolError as exc:
            return ToolOutput(str(exc), success=False)
        except Exception as exc:
            # An unexpected failure is reported to the model like any tool error,
            # so one bad call does not end the session.
            return ToolOutput(f"{name} failed: {type(exc).__name__}: {exc}", success=False)


def require_string(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"'{key}' must be a non-empty string")
    return value


def optional_string(arguments: dict[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError(f"'{key}' must be a string")
    return value


def optional_bool(arguments: dict[str, Any], key: str) -> bool:
    value = arguments.get(key, False)
    if not isinstance(value, bool):
        raise ToolError(f"'{key}' must be true or false")
    return value
