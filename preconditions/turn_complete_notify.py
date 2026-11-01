#!/usr/bin/env python3
"""Record finished agent turns.

Set as the `notify` program: the agent calls it after each turn with one JSON argument
(thread-id, turn-id, cwd, last-assistant-message, ...). The script appends a line with the time,
the thread, the turn and the start of the last message to .notifications/turns.jsonl in the
project directory. It also shows a desktop notification when `notify-send` is installed.
Only the standard library is used.

    python3 tools/turn_complete_notify.py '{"type": "agent-turn-complete", ...}'
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = ".notifications"
LOG_FILE = "turns.jsonl"
MESSAGE_CHARS = 200


def project_dir(payload: dict) -> Path:
    """The project directory named in the payload, else the one that holds tools/."""
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return Path(cwd)
    return Path(__file__).resolve().parent.parent


def make_record(payload: dict) -> dict:
    message = payload.get("last-assistant-message")
    return {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "thread": payload.get("thread-id"),
        "turn": payload.get("turn-id"),
        "message": message[:MESSAGE_CHARS] if isinstance(message, str) else "",
    }


def append_record(root: Path, record: dict) -> Path:
    directory = root / LOG_DIR
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / LOG_FILE
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return target


def desktop_notification(message: str) -> None:
    if shutil.which("notify-send") is None:
        return
    try:
        subprocess.run(
            ["notify-send", "Turn complete", message[:100]],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: turn_complete_notify.py <json>", file=sys.stderr)
        return 2
    try:
        payload = json.loads(argv[1])
    except ValueError as exc:
        print(f"turn_complete_notify: invalid JSON argument: {exc}", file=sys.stderr)
        return 1
    if not isinstance(payload, dict):
        print("turn_complete_notify: expected a JSON object", file=sys.stderr)
        return 1
    record = make_record(payload)
    try:
        append_record(project_dir(payload), record)
    except OSError as exc:
        print(f"turn_complete_notify: cannot write the log: {exc}", file=sys.stderr)
        return 1
    desktop_notification(record["message"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
