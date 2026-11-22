"""The `write_stdin` tool: send input to a process started by exec_command and read its output."""

from __future__ import annotations

import time
from typing import Any

from meltzer.exec_sessions import CTRL_C, InteractiveProcess, ProcessStore, write_yield_seconds
from meltzer.tools.exec_command import format_output
from meltzer.tools.registry import Tool, ToolError, ToolSpec, optional_int, optional_string, require_int

WRITE_STDIN_SPEC = ToolSpec(
    name="write_stdin",
    description=(
        "Writes characters to an existing exec_command session and returns recent output. "
        "Send empty `chars` to wait for more output without writing. Sending \"\\u0003\" "
        "(Ctrl-C) interrupts the process."
    ),
    parameters={
        "type": "object",
        "properties": {
            "session_id": {"type": "integer", "description": "Identifier of the running exec_command session."},
            "chars": {"type": "string", "description": "Characters to write to stdin (may be empty to poll)."},
            "yield_time_ms": {
                "type": "integer",
                "description": "How long to wait (in milliseconds) for output before yielding.",
            },
            "max_output_tokens": {
                "type": "integer",
                "description": "Maximum number of tokens to return. Excess output will be truncated.",
            },
        },
        "required": ["session_id"],
        "additionalProperties": False,
    },
)


class WriteStdinTool(Tool):
    spec = WRITE_STDIN_SPEC

    def __init__(self, store: ProcessStore, max_timeout_ms: int, session_id: str):
        self.store = store
        self.session_id = session_id
        # background_terminal_max_timeout: the longest a poll waits.
        self.max_timeout_ms = max_timeout_ms

    def hook_input(self, arguments: str) -> str | None:
        return None

    def run(self, arguments: dict[str, Any]) -> str:
        process_id = require_int(arguments, "session_id")
        chars = optional_string(arguments, "chars") or ""
        yield_time_ms = optional_int(arguments, "yield_time_ms")
        max_output_tokens = optional_int(arguments, "max_output_tokens", positive=True)
        try:
            process = self.store.get(process_id, self.session_id)
        except KeyError:
            raise ToolError(f"Unknown process id {process_id}") from None
        if not isinstance(process, InteractiveProcess):
            raise ToolError(f"Unknown process id {process_id}")

        with process.lock:
            started = time.monotonic()
            if chars == CTRL_C:
                process.interrupt()
            elif chars:
                if not process.tty:
                    raise ToolError(
                        "stdin is closed for this session; rerun exec_command with tty=true to keep stdin open"
                    )
                if not process.has_exited():
                    try:
                        process.write(chars.encode("utf-8"))
                    except OSError as exc:
                        raise ToolError(f"failed to write to session {process_id}: {exc}") from exc
            output = process.collect(started + write_yield_seconds(yield_time_ms, chars, self.max_timeout_ms))
            exit_code = process.poll()
        if exit_code is not None:
            self.store.remove(process_id)
        return format_output(
            wall_time=time.monotonic() - started,
            output=output,
            exit_code=exit_code,
            process_id=process_id,
            max_output_tokens=max_output_tokens,
        )
