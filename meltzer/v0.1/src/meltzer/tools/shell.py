"""The `shell` tool: run a bash command, sandboxed unless an escalation is approved."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from meltzer.approval.orchestrator import ApprovalOrchestrator, CommandRequest
from meltzer.exec import ProcessRunner
from meltzer.sandbox.bwrap import SandboxSetupError
from meltzer.tools.registry import ToolError, ToolSpec, optional_bool, optional_string, require_string

MAX_OUTPUT_CHARS = 30_000

SHELL_SPEC = ToolSpec(
    name="shell",
    description=(
        "Run a command with `bash -c`. Commands run in a sandbox: by default the filesystem is "
        "read-only except the workspace and /tmp (a directory private to this session), and there "
        "is no network access. "
        "If a command needs more (network, writing outside the workspace), set `escalate` to "
        "true and give a one-sentence `justification`; it then runs outside the sandbox if approved. "
        "Set `background` to true for long-running processes: the call returns at once with a "
        "process id and a log file that receives the output."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command to run."},
            "workdir": {
                "type": "string",
                "description": "Working directory, absolute or relative to the workspace root. Defaults to the root.",
            },
            "escalate": {
                "type": "boolean",
                "description": "Request to run outside the sandbox. Requires approval.",
            },
            "justification": {
                "type": "string",
                "description": "Why the command needs to run outside the sandbox. Required with escalate.",
            },
            "background": {
                "type": "boolean",
                "description": "Start the command and return immediately.",
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    },
)


def truncate_output(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    omitted = len(text) - 2 * half
    return f"{text[:half]}\n[... {omitted} characters omitted ...]\n{text[-half:]}"


class ShellTool:
    spec = SHELL_SPEC

    def __init__(self, root: Path, orchestrator: ApprovalOrchestrator, runner: ProcessRunner):
        self.root = root
        self.orchestrator = orchestrator
        self.runner = runner

    def _workdir(self, value: str | None) -> Path:
        if not value:
            return self.root
        path = Path(value)
        if not path.is_absolute():
            path = self.root / path
        if not path.is_dir():
            raise ToolError(f"workdir does not exist or is not a directory: {path}")
        return path

    def run(self, arguments: dict[str, Any]) -> str:
        command = require_string(arguments, "command")
        workdir = self._workdir(optional_string(arguments, "workdir"))
        escalate = optional_bool(arguments, "escalate")
        background = optional_bool(arguments, "background")
        justification = (optional_string(arguments, "justification") or "").strip()
        if escalate and not justification:
            raise ToolError("'justification' is required when 'escalate' is true")

        request = CommandRequest(
            command=command,
            cwd=workdir,
            escalate=escalate,
            justification=justification,
            background=background,
        )
        decision = self.orchestrator.decide_command(request)
        if not decision.run:
            raise ToolError(f"command not run: {decision.reason}")

        try:
            if background:
                process = self.runner.start_background(command, workdir, sandboxed=decision.sandboxed)
                return (
                    f"Started background process {process.pid}\n"
                    f"Log file: {process.log_path}"
                )
            result = self.runner.run(command, workdir, sandboxed=decision.sandboxed)
        except SandboxSetupError as exc:
            raise ToolError(f"command not run: {exc}") from exc
        except OSError as exc:
            raise ToolError(f"command not run: failed to start process: {exc}") from exc

        lines = []
        if result.timed_out:
            lines.append(f"Command timed out after {self.runner.timeout:.0f} seconds")
        lines.append(f"Exit code: {result.exit_code}")
        lines.append(f"Wall time: {result.duration:.1f} seconds")
        lines.append("Output:")
        lines.append(truncate_output(result.output))
        return "\n".join(lines)
