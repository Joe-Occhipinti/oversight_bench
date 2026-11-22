"""Slash commands of the full-screen interface.

`/help`, `/status` and `/clear` run in the interface, also while a turn runs.
`/plan`, `/rollback`, `/feedback`, `/quit`, `/exit` and `!cmd` go to the session like
any other input, and run between turns, as in the line-based interface.
"""

from __future__ import annotations

from typing import Any, Iterable

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document

from meltzer.sessions import ROLLBACK_COMMAND
from meltzer.telemetry.feedback import FEEDBACK_COMMAND
from meltzer.tools.plan import PLAN_COMMAND, PLAN_COMMAND_HELP

SLASH_COMMANDS = {
    PLAN_COMMAND: "show or edit the task plan",
    ROLLBACK_COMMAND: "remove the last N user turns from the conversation (/rollback N)",
    FEEDBACK_COMMAND: "send a feedback report, with feedback.enabled (/feedback [note])",
    "/status": "show the model, sandbox mode, approval policy and working directory",
    "/clear": "clear the transcript pane",
    "/help": "list the commands and keys",
    "/quit": "end the session (also /exit)",
}

# Commands the interface runs itself.
LOCAL_COMMANDS = ("/help", "/status", "/clear")

KEYS_HELP = """\
Enter          send the prompt
Ctrl-C         clear the input; stop the running turn; at an empty prompt, end the session
Ctrl-D         end the session at an empty prompt
PageUp/Down    scroll the transcript
@              complete a file path
!cmd           run cmd in the workspace root, as you, outside the sandbox
In a dialog: Up/Down choose, Enter confirms, Tab moves between the request and the choices,
Esc or Ctrl-C refuses."""


def local_command(text: str) -> str | None:
    """The interface command that text starts with, or None if the session handles text."""
    command = text.strip().split(" ", 1)[0]
    return command if command in LOCAL_COMMANDS else None


def help_text() -> str:
    width = max(len(name) for name in SLASH_COMMANDS)
    lines = ["Commands:"]
    lines += [f"  {name.ljust(width)}  {description}" for name, description in SLASH_COMMANDS.items()]
    lines += ["", PLAN_COMMAND_HELP, "", "Keys:", KEYS_HELP]
    return "\n".join(lines)


def status_report(session: Any | None) -> str:
    """What `/status` shows for a session (None before the session has started)."""
    if session is None:
        return "the session has not started yet"
    return "\n".join(
        [
            f"model: {session.config.model}",
            f"sandbox: {session.policy.mode.value}",
            f"approval policy: {session.settings.policy.value}",
            f"cwd: {session.root}",
            f"session: {session.id}",
        ]
    )


class SlashCompleter(Completer):
    """Completes a command name typed at the start of the input."""

    def get_completions(self, document: Document, complete_event: CompleteEvent) -> Iterable[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        for name, description in SLASH_COMMANDS.items():
            if name.startswith(text):
                yield Completion(name, start_position=-len(text), display_meta=description)
