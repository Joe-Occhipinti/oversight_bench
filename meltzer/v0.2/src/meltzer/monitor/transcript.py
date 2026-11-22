"""Renders the agent history as the transcript an approval review reads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from meltzer.history import Item, Kind, Role
from meltzer.realtime.bridge import VOICE_SOURCE
from meltzer.utils.truncate import truncate_output

NO_ENTRIES = "<no retained transcript entries>"
TOOL_ENTRY_CHARS = 4_000

USER_LABEL = "user"
VOICE_LABEL = "user (voice)"
USER_SHELL_LABEL = "user shell command"
# Entries with these labels are kept in every window.
USER_LABELS = (USER_LABEL, VOICE_LABEL)


@dataclass(frozen=True)
class TranscriptEntry:
    number: int  # 1-based position in the full history
    # "user", "user (voice)", "user shell command", "assistant", "tool <name> call",
    # "tool <name> result", "hook"
    label: str
    text: str

    @property
    def is_user(self) -> bool:
        return self.label in USER_LABELS

    def render(self) -> str:
        return f"[{self.number}] {self.label}: {self.text}"


# Labels of context items. None: the item describes the session and is not part
# of the transcript.
CONTEXT_LABELS: dict[Kind, str | None] = {
    Kind.ENVIRONMENT: None,
    Kind.SKILL: None,
    Kind.HOOK_CONTEXT: None,
    Kind.HOOK_PROMPT: "hook",
}


def _entry(number: int, item: Item) -> TranscriptEntry | None:
    if item.kind in (Kind.REASONING, Kind.AGENTS_MD):
        return None
    if item.kind in CONTEXT_LABELS:
        label = CONTEXT_LABELS[item.kind]
        if label is None:
            return None
        text = item.content
    elif item.kind == Kind.USER_SHELL_COMMAND:
        label, text = USER_SHELL_LABEL, item.content
    elif item.role == Role.USER:
        label, text = (VOICE_LABEL if item.source == VOICE_SOURCE else USER_LABEL), item.content
    elif item.kind == Kind.MESSAGE:
        label, text = "assistant", item.content
    elif item.kind == Kind.TOOL_CALL:
        label, text = f"tool {item.name} call", item.arguments or ""
    elif item.kind == Kind.TOOL_RESULT:
        label, text = f"tool {item.name} result", item.content
    else:
        return None
    if not text.strip():
        return None
    if item.kind in (Kind.TOOL_CALL, Kind.TOOL_RESULT):
        text = truncate_output(text, TOOL_ENTRY_CHARS)
    return TranscriptEntry(number, label, text)


def transcript_entries(items: Iterable[Item]) -> list[TranscriptEntry]:
    entries = []
    for number, item in enumerate(items, start=1):
        entry = _entry(number, item)
        if entry is not None:
            entries.append(entry)
    return entries


def select_window(entries: Sequence[TranscriptEntry], window: int) -> list[TranscriptEntry]:
    """The entries a review reads, in history order."""
    others = [entry for entry in entries if not entry.is_user]
    recent = {entry.number for entry in others[-window:]} if window > 0 else set()
    return [entry for entry in entries if entry.is_user or entry.number in recent]


def render_transcript(items: Iterable[Item], window: int) -> str:
    """Transcript lines, each ending with a newline."""
    entries = select_window(transcript_entries(items), window)
    if not entries:
        return NO_ENTRIES + "\n"
    return "".join(entry.render() + "\n" for entry in entries)
