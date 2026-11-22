"""The `get_context_remaining` tool: tokens left in the model's context window.

The count is `model_context_window` minus the prompt tokens of the last model
response, as reported by the provider.
"""

from __future__ import annotations

from typing import Any, Callable

from meltzer.client import Usage
from meltzer.tools.registry import Tool, ToolSpec

CONTEXT_REMAINING_SPEC = ToolSpec(
    name="get_context_remaining",
    description="Report how many tokens are left in the context window, measured at the last model request.",
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
)


class ContextRemainingTool(Tool):
    spec = CONTEXT_REMAINING_SPEC

    def __init__(self, context_window: int | None, last_usage: Callable[[], Usage | None]):
        self.context_window = context_window
        self.last_usage = last_usage

    def run(self, arguments: dict[str, Any]) -> str:
        if self.context_window is None:
            return "Tokens left unknown: model_context_window is not set in the config."
        usage = self.last_usage()
        if usage is None:
            return "Tokens left unknown: no model response has reported token usage yet."
        left = max(self.context_window - usage.prompt_tokens, 0)
        return (
            f"{left} tokens left in the context window "
            f"({usage.prompt_tokens} of {self.context_window} used by the last request)."
        )
