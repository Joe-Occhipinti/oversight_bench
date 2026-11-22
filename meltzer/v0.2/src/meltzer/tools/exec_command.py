"""The `exec_command` tool: start a command that can keep running, with a terminal or pipes."""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Any

from meltzer.approval.orchestrator import ApprovalOrchestrator, CommandRequest
from meltzer.exec import ProcessRunner, setup_failure
from meltzer.exec_sessions import InteractiveProcess, ProcessStore, exec_yield_seconds
from meltzer.network.policy import NetworkApprovals
from meltzer.sandbox.bwrap import SandboxSetupError
from meltzer.tools.registry import (
    Tool,
    ToolError,
    ToolSpec,
    optional_bool,
    optional_int,
    optional_string,
    require_string,
    resolve_workdir,
)
from meltzer.utils.truncate import truncate_output

DEFAULT_MAX_OUTPUT_TOKENS = 10_000
# Tokens are estimated as 4 bytes of output each.
BYTES_PER_TOKEN = 4

EXEC_COMMAND_SPEC = ToolSpec(
    name="exec_command",
    description=(
        "Runs a command with `bash -c`, returning its output, or a session ID for ongoing "
        "interaction if it is still running when the call returns. Commands run in the same "
        "sandbox as `shell`. If a command needs more (network, writing outside the workspace), "
        "set `escalate` to true and give a one-sentence `justification`; it then runs outside the "
        "sandbox if approved. Use `write_stdin` with the session ID to send input to the process "
        "or to read more of its output."
    ),
    parameters={
        "type": "object",
        "properties": {
            "cmd": {"type": "string", "description": "Shell command to execute."},
            "workdir": {
                "type": "string",
                "description": "Working directory, absolute or relative to the workspace root. Defaults to the root.",
            },
            "tty": {
                "type": "boolean",
                "description": (
                    "Whether to allocate a TTY for the command. Defaults to false (plain pipes, no "
                    "input); set to true to open a PTY and send input with write_stdin."
                ),
            },
            "yield_time_ms": {
                "type": "integer",
                "description": "How long to wait (in milliseconds) for output before yielding.",
            },
            "max_output_tokens": {
                "type": "integer",
                "description": "Maximum number of tokens to return. Excess output will be truncated.",
            },
            "escalate": {
                "type": "boolean",
                "description": "Request to run outside the sandbox. Requires approval.",
            },
            "justification": {
                "type": "string",
                "description": "Why the command needs to run outside the sandbox. Required with escalate.",
            },
        },
        "required": ["cmd"],
        "additionalProperties": False,
    },
)


def format_output(
    *, wall_time: float, output: str, exit_code: int | None, process_id: int, max_output_tokens: int | None
) -> str:
    """The result of exec_command and write_stdin."""
    max_tokens = max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS
    original_tokens = (len(output.encode("utf-8")) + BYTES_PER_TOKEN - 1) // BYTES_PER_TOKEN
    lines = [f"Chunk ID: {secrets.token_hex(3)}", f"Wall time: {wall_time:.1f} seconds"]
    if exit_code is not None:
        lines.append(f"Process exited with code {exit_code}")
    else:
        lines.append(f"Process running with session ID {process_id}")
    lines.append(f"Original token count: {original_tokens}")
    lines.append("Output:")
    lines.append(truncate_output(output, max_tokens * BYTES_PER_TOKEN))
    return "\n".join(lines)


class ExecCommandTool(Tool):
    spec = EXEC_COMMAND_SPEC

    def __init__(
        self,
        root: Path,
        orchestrator: ApprovalOrchestrator,
        runner: ProcessRunner,
        store: ProcessStore,
        session_id: str,
        network: NetworkApprovals | None = None,
    ):
        self.root = root
        self.orchestrator = orchestrator
        self.runner = runner
        self.store = store
        self.session_id = session_id
        # The session's network approvals, for commands that use the network proxy.
        self.network = network

    def run(self, arguments: dict[str, Any]) -> str:
        command = require_string(arguments, "cmd")
        workdir = resolve_workdir(self.root, optional_string(arguments, "workdir"))
        tty = optional_bool(arguments, "tty")
        escalate = optional_bool(arguments, "escalate")
        justification = (optional_string(arguments, "justification") or "").strip()
        yield_time_ms = optional_int(arguments, "yield_time_ms")
        max_output_tokens = optional_int(arguments, "max_output_tokens", positive=True)
        if escalate and not justification:
            raise ToolError("'justification' is required when 'escalate' is true")

        request = CommandRequest(
            command=command,
            cwd=workdir,
            escalate=escalate,
            justification=justification,
            tool="exec_command",
            tty=tty,
        )
        decision = self.orchestrator.decide_command(request)
        if not decision.run:
            raise ToolError(f"command not run: {decision.reason}")

        process_id = self.store.allocate_id()
        started = time.monotonic()
        try:
            process = InteractiveProcess.start(
                self.runner,
                process_id,
                self.session_id,
                command,
                workdir,
                sandboxed=decision.sandboxed,
                policy=self.orchestrator.sandbox,
                tty=tty,
                network=self.network,
            )
        except SandboxSetupError as exc:
            self.store.release(process_id)
            raise ToolError(f"command not run: {exc}") from exc
        except OSError as exc:
            self.store.release(process_id)
            raise ToolError(f"command not run: failed to start process: {exc}") from exc
        self.store.add(process)

        output = process.collect(started + exec_yield_seconds(yield_time_ms))
        exit_code = process.poll()
        if exit_code is not None:
            self.store.remove(process_id)
            if process.setup_failed:
                raise ToolError(f"command not run: {setup_failure(output, exit_code)}")
        return format_output(
            wall_time=time.monotonic() - started,
            output=output,
            exit_code=exit_code,
            process_id=process_id,
            max_output_tokens=max_output_tokens,
        )
