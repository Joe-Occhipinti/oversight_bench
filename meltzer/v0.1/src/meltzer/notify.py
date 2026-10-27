"""Runs the configured `notify` program when a turn of the main session completes.

    notify = ["python3", "/home/me/.meltzer/notify.py"]

The program gets one extra argument, a JSON object:

    {"type": "agent-turn-complete", "thread-id": "...", "turn-id": "...",
     "cwd": "...", "client": "meltzer", "input-messages": ["..."],
     "last-assistant-message": "..."}

It is started in the background, with no stdin or stdout, and is not waited
for. Turns of subagents do not run it.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Callable

CLIENT_NAME = "meltzer"


def turn_complete_payload(
    *,
    thread_id: str,
    turn_id: str,
    cwd: Path,
    input_messages: list[str],
    last_assistant_message: str | None,
) -> str:
    return json.dumps(
        {
            "type": "agent-turn-complete",
            "thread-id": thread_id,
            "turn-id": turn_id,
            "cwd": str(cwd),
            "client": CLIENT_NAME,
            "input-messages": input_messages,
            "last-assistant-message": last_assistant_message,
        },
        ensure_ascii=False,
    )


class Notifier:
    def __init__(self, argv: tuple[str, ...] | None, notice: Callable[[str], None]):
        self.argv = tuple(argv or ())
        self.notice = notice

    def turn_complete(
        self,
        *,
        thread_id: str,
        turn_id: str,
        cwd: Path,
        input_messages: list[str],
        last_assistant_message: str | None,
    ) -> None:
        if not self.argv:
            return
        payload = turn_complete_payload(
            thread_id=thread_id,
            turn_id=turn_id,
            cwd=cwd,
            input_messages=input_messages,
            last_assistant_message=last_assistant_message,
        )
        try:
            subprocess.Popen(
                [*self.argv, payload],
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Detached, so it is not interrupted with the terminal's process group.
                start_new_session=True,
            )
        except OSError as exc:
            self.notice(f"notify command {shlex.join(self.argv)} failed to start: {exc}")
