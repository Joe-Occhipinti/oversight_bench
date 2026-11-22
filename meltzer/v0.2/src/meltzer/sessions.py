"""Resume, fork and roll back recorded sessions.

`meltzer resume` continues a recorded session: it loads the conversation and
appends to the same session file. `meltzer fork` starts a new session, with a
new id and file, from a copy of a recorded session's conversation. Both find
the session by id, or take the newest one, in ~/.meltzer/sessions.

The new session takes its settings (sandbox, approvals, reviewer, profile,
rules, hooks, MCP servers, model) from the current configuration, as any new
session does, and adds context items (AGENTS.md, environment) from the
current files and settings. The settings recorded in the session file are not
read. A resumed session writes a `resumed` event with its settings.

`/rollback N` (interactive sessions, between turns) removes the last N user
turns from the conversation and writes a `rolled_back` event; files changed in
those turns stay as they are.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meltzer.config.loader import ConfigError, meltzer_home
from meltzer.history import (
    Item,
    SessionFileError,
    SessionRecorder,
    find_session,
    load_session,
    session_file,
)

if TYPE_CHECKING:
    from meltzer.session import Session

SESSIONS_DIR_NAME = "sessions"

# The result given to a tool call that has no result in the session file.
UNFINISHED_TOOL_CALL = "the session ended before this tool call finished"

ROLLBACK_COMMAND = "/rollback"
ROLLBACK_USAGE = "usage: /rollback N  (remove the last N user turns from the conversation)"


class StartMode(StrEnum):
    RESUMED = "resumed"
    FORKED = "forked"


@dataclass(frozen=True)
class InitialHistory:
    """A recorded session that a new top-level session continues (resumed) or copies (forked)."""

    mode: StartMode
    path: Path
    meta: dict[str, Any]
    items: list[Item]

    @property
    def session_id(self) -> str:
        return self.meta["id"]


def open_initial_history(
    mode: StartMode, session_id: str | None, *, home: Path | None = None, cwd: Path | None = None
) -> InitialHistory:
    """Load session `session_id` from ~/.meltzer/sessions; for None, the newest top-level session recorded in `cwd`."""
    sessions_dir = meltzer_home(home) / SESSIONS_DIR_NAME
    try:
        path = find_session(sessions_dir, session_id, cwd)
        meta, items = load_session(path)
    except SessionFileError as exc:
        raise ConfigError(str(exc)) from None
    if not _is_session_id(meta.get("id")) or not path.stem.endswith(f"-{meta['id']}"):
        raise ConfigError(f"{path} has no valid session id")
    parent = meta.get("parent_session_id")
    if parent is not None:
        verb = "resume" if mode == StartMode.RESUMED else "fork"
        raise ConfigError(f"session {meta['id']} is a subagent session; {verb} its parent session {parent} instead")
    return InitialHistory(mode, path, meta, items)


def _is_session_id(value: Any) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def open_recorder(
    initial: InitialHistory, sessions_dir: Path, session_id: str, started: datetime, meta: dict[str, Any]
) -> SessionRecorder:
    """The recorder of a session started from `initial`, with its start record written.

    `meta` is the new session's `session_meta` record. A resumed session appends
    to the recorded file, starting with a `resumed` event that holds `meta`
    without the id and start time; a forked session gets a new file whose
    `session_meta` has `forked_from_id`.
    """
    if initial.mode == StartMode.RESUMED:
        recorder = SessionRecorder(initial.path)
        recorder.event("resumed", {key: value for key, value in meta.items() if key not in ("id", "started_at")})
        return recorder
    recorder = SessionRecorder(session_file(sessions_dir, session_id, started))
    recorder.write_meta({**meta, "forked_from_id": initial.session_id})
    return recorder


def rollback(session: Session, num_turns: int) -> int:
    """Remove the last `num_turns` user turns from the session's history; returns the number of items removed.

    Writes a `rolled_back` event. The task plan keeps its steps and stays in the history.
    """
    dropped = session.history.drop_last_user_turns(num_turns)
    session.recorder.event("rolled_back", {"num_turns": num_turns})
    session.plan.sync()
    return dropped


def run_rollback_command(session: Session, arguments: str) -> str:
    """Apply a `/rollback` command typed by the user and return what to show them."""
    try:
        num_turns = int(arguments.strip())
    except ValueError:
        return ROLLBACK_USAGE
    try:
        dropped = rollback(session, num_turns)
    except ValueError as exc:
        return f"cannot roll back {num_turns} turns: {exc}"
    turns = "turn" if num_turns == 1 else "turns"
    return f"rolled back {num_turns} {turns} ({dropped} items); files changed in those turns are not restored"
