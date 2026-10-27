"""The `compact` tool: replace a long conversation with a summary written by the model."""

from __future__ import annotations

from typing import Any, Callable

from meltzer.history import History, Item, Kind, Role, SessionRecorder
from meltzer.tools.registry import ToolSpec, require_string

SUMMARY_PREFIX = (
    "The conversation so far was compacted. The summary below was written before compaction; "
    "use it to continue the task without repeating finished work:\n"
)

# Items that describe the session rather than the conversation; compaction keeps them.
KEPT_KINDS = (Kind.AGENTS_MD,)

COMPACT_SPEC = ToolSpec(
    name="compact",
    description=(
        "Replace the conversation history with a summary, to free up context when the "
        "conversation has grown long. The summary is all you will see of the earlier "
        "conversation, so include: the user's request and preferences, progress and key "
        "decisions, what remains to be done, and any data or file paths needed to continue. "
        "Call it on its own, not together with other tools."
    ),
    parameters={
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Handoff summary of the conversation so far."},
        },
        "required": ["summary"],
        "additionalProperties": False,
    },
)


def compact_history(history: History, recorder: SessionRecorder, summary: str) -> Item:
    """Replace the history with the kept items, the summary and the plan."""
    kept = [item for item in history if item.kind in KEPT_KINDS]
    summary_item = Item(Role.USER, Kind.COMPACTION_SUMMARY, content=SUMMARY_PREFIX + summary)
    plan = history.last_of_kind(Kind.PLAN)
    replacement = [*kept, summary_item, *([plan] if plan is not None else [])]
    dropped = len(history) - len(kept) - (1 if plan is not None else 0)
    history.replace(replacement)
    recorder.event(
        "compacted",
        {"dropped_items": dropped, "replacement_history": [item.to_record() for item in replacement]},
    )
    return summary_item


class CompactTool:
    spec = COMPACT_SPEC

    def __init__(
        self,
        history: History,
        recorder: SessionRecorder,
        after_tool_calls: Callable[[Callable[[], None]], None],
    ):
        self.history = history
        self.recorder = recorder
        self.after_tool_calls = after_tool_calls

    def run(self, arguments: dict[str, Any]) -> str:
        summary = require_string(arguments, "summary").strip()
        self.after_tool_calls(lambda: compact_history(self.history, self.recorder, summary))
        return "The history will be replaced by the summary."
