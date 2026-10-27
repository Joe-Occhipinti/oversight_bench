"""Line-based terminal output and prompts."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import TextIO

from meltzer.approval.policy import ApprovalChoice, ApprovalRequest, patch_preview


class TerminalUI:
    """In interactive mode replies go to stdout; in exec mode only the final reply does.

    Progress (tool calls, warnings) always goes to stderr.
    """

    def __init__(
        self,
        interactive: bool,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ):
        self.interactive = interactive
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr

    def _readline(self) -> str | None:
        line = self.stdin.readline()
        if not line:
            return None
        return line.rstrip("\n")

    def info(self, message: str) -> None:
        print(message, file=self.stderr, flush=True)

    def assistant_message(self, text: str) -> None:
        stream = self.stdout if self.interactive else self.stderr
        print(text, file=stream, flush=True)

    def final_message(self, text: str) -> None:
        if not self.interactive:
            print(text, file=self.stdout, flush=True)

    def tool_call(self, name: str, arguments: str) -> None:
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            parsed = None
        if name == "shell" and isinstance(parsed, dict) and isinstance(parsed.get("command"), str):
            flags = [flag for flag in ("escalate", "background") if parsed.get(flag)]
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            self.info(f"$ {parsed['command']}{suffix}")
        else:
            self.info(f"[{name}]")

    def read_prompt(self) -> str | None:
        """Next user prompt, or None at end of input."""
        if self.stdin.isatty():
            print("> ", end="", file=self.stdout, flush=True)
        while True:
            line = self._readline()
            if line is None:
                return None
            if line.strip():
                return line

    def _show_request(self, request: ApprovalRequest) -> None:
        print("", file=self.stdout)
        if request.patch is None:
            print("meltzer wants to run outside the sandbox:", file=self.stdout)
            print(json.dumps(request.planned_action(), indent=2), file=self.stdout)
            return
        print("meltzer wants to apply a patch that writes outside the writable paths:", file=self.stdout)
        for path in request.paths:
            print(f"  {path}", file=self.stdout)
        print("Patch:", file=self.stdout)
        print(patch_preview(request.patch).rstrip("\n"), file=self.stdout)

    def ask_approval(self, request: ApprovalRequest) -> ApprovalChoice:
        self._show_request(request)
        if request.suggested_prefix:
            question = (
                "Approve? [y]es once / [a]lways allow commands starting with "
                f"`{shlex.join(request.suggested_prefix)}` / [n]o: "
            )
        else:
            question = "Approve once? [y]es / [n]o: "
        while True:
            print(question, end="", file=self.stdout, flush=True)
            answer = self._readline()
            if answer is None:
                print("", file=self.stdout)
                return ApprovalChoice.DENY
            answer = answer.strip().lower()
            if answer in ("y", "yes"):
                return ApprovalChoice.APPROVE_ONCE
            if request.suggested_prefix and answer in ("a", "always"):
                return ApprovalChoice.ALWAYS_ALLOW_PREFIX
            if answer in ("n", "no"):
                return ApprovalChoice.DENY

    def confirm_hook(self, description: str) -> bool:
        """Ask whether to trust a hook that has not been approved before."""
        print("", file=self.stdout)
        print("A hook in your configuration has not been trusted yet:", file=self.stdout)
        print(description, file=self.stdout)
        while True:
            print("Trust and run this hook? [y]es / [n]o: ", end="", file=self.stdout, flush=True)
            answer = self._readline()
            if answer is None:
                print("", file=self.stdout)
                return False
            answer = answer.strip().lower()
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False

    def confirm_project_trust(self, project: Path, cwd: Path) -> bool | None:
        """Ask whether to trust the files in a project. None if input ended before an answer."""
        print("", file=self.stdout)
        print(f"Do you trust the files in {project}?", file=self.stdout)
        if project != cwd:
            print(
                f"{cwd} is inside a Git repository; the answer applies to the repository root.",
                file=self.stdout,
            )
        print(
            "Trusting it lets meltzer load the project config and rules from its .meltzer directories.",
            file=self.stdout,
        )
        while True:
            print("Trust this directory? [y]es / [n]o: ", end="", file=self.stdout, flush=True)
            answer = self._readline()
            if answer is None:
                print("", file=self.stdout)
                return None
            answer = answer.strip().lower()
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
