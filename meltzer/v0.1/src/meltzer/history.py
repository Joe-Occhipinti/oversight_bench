"""Conversation history and session recording.

Every item has a role (who produced it) and a kind (what it is). Items that
come from one model response share a response_id, so they can be sent back
to the provider as one assistant message.

Sessions are recorded as JSON lines under ~/.meltzer/sessions/YYYY/MM/DD/:
a `session_meta` line first, then one `item` line per new history item, and
event lines for changes to existing items (`item_updated`, `item_removed`,
`compacted`) and for approval reviews (`review`).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class Kind(StrEnum):
    MESSAGE = "message"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    # Context items.
    AGENTS_MD = "agents_md"
    PLAN = "plan"
    COMPACTION_SUMMARY = "compaction_summary"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class Item:
    role: Role
    kind: Kind
    content: str = ""
    name: str | None = None  # tool name, for tool calls and results
    call_id: str | None = None
    arguments: str | None = None  # raw JSON arguments of a tool call
    reasoning: dict[str, Any] | None = None  # provider reasoning fields, kept verbatim
    response_id: str | None = None
    created_at: str = field(default_factory=_now)

    @classmethod
    def user(cls, text: str) -> Item:
        return cls(Role.USER, Kind.MESSAGE, content=text)

    @classmethod
    def assistant(cls, text: str, response_id: str) -> Item:
        return cls(Role.ASSISTANT, Kind.MESSAGE, content=text, response_id=response_id)

    @classmethod
    def reasoning_fields(cls, fields: dict[str, Any], response_id: str) -> Item:
        return cls(Role.ASSISTANT, Kind.REASONING, reasoning=dict(fields), response_id=response_id)

    @classmethod
    def tool_call(cls, call_id: str, name: str, arguments: str, response_id: str) -> Item:
        return cls(
            Role.ASSISTANT,
            Kind.TOOL_CALL,
            name=name,
            call_id=call_id,
            arguments=arguments,
            response_id=response_id,
        )

    @classmethod
    def tool_result(cls, call_id: str, name: str, output: str) -> Item:
        return cls(Role.TOOL, Kind.TOOL_RESULT, content=output, name=name, call_id=call_id)

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["role"] = self.role.value
        record["kind"] = self.kind.value
        return {key: value for key, value in record.items() if value is not None}


class History:
    def __init__(self) -> None:
        self.items: list[Item] = []

    def append(self, item: Item) -> None:
        self.items.append(item)

    def __iter__(self) -> Iterator[Item]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def last_of_kind(self, kind: Kind) -> Item | None:
        for item in reversed(self.items):
            if item.kind == kind:
                return item
        return None

    def index_of(self, item: Item) -> int:
        for index, candidate in enumerate(self.items):
            if candidate is item:
                return index
        raise ValueError("item is not in the history")

    def remove(self, item: Item) -> int:
        index = self.index_of(item)
        del self.items[index]
        return index

    def replace(self, items: list[Item]) -> None:
        self.items = list(items)

    def to_messages(self) -> list[dict[str, Any]]:
        """Chat-completions messages for the history (without the system message)."""
        messages: list[dict[str, Any]] = []
        pending: dict[str, Any] | None = None
        pending_id: str | None = None

        def flush() -> None:
            nonlocal pending, pending_id
            if pending is not None:
                messages.append(pending)
            pending, pending_id = None, None

        for item in self.items:
            if item.role == Role.ASSISTANT:
                if pending is None or item.response_id != pending_id:
                    flush()
                    pending = {"role": "assistant", "content": None}
                    pending_id = item.response_id
                if item.kind == Kind.REASONING and item.reasoning:
                    pending.update(item.reasoning)
                elif item.kind == Kind.MESSAGE:
                    pending["content"] = item.content
                elif item.kind == Kind.TOOL_CALL:
                    pending.setdefault("tool_calls", []).append(
                        {
                            "id": item.call_id,
                            "type": "function",
                            "function": {"name": item.name, "arguments": item.arguments or "{}"},
                        }
                    )
                continue
            flush()
            if item.kind == Kind.TOOL_RESULT:
                messages.append({"role": "tool", "tool_call_id": item.call_id, "content": item.content})
            elif item.role == Role.USER:
                messages.append({"role": "user", "content": item.content})
        flush()
        return messages


def session_file(sessions_dir: Path, session_id: str, started: datetime) -> Path:
    day_dir = sessions_dir / started.strftime("%Y") / started.strftime("%m") / started.strftime("%d")
    return day_dir / f"session-{started.strftime('%Y-%m-%dT%H-%M-%S')}-{session_id}.jsonl"


class SessionRecorder:
    """Appends session records to a JSONL file, flushing after every line."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a", encoding="utf-8")

    def _write(self, record: dict[str, Any]) -> None:
        self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._file.flush()

    def write_meta(self, meta: dict[str, Any]) -> None:
        self._write({"type": "session_meta", "timestamp": _now(), **meta})

    def record(self, item: Item) -> None:
        self._write({"type": "item", **item.to_record()})

    def event(self, kind: str, payload: dict[str, Any]) -> None:
        """A session event that is not a new history item (reviews, compaction, item updates)."""
        self._write({"type": kind, "timestamp": _now(), **payload})

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()

