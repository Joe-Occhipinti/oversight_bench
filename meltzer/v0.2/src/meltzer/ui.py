"""Line-based terminal output and prompts."""

from __future__ import annotations

import json
import re
import shlex
import sys
import unicodedata
from pathlib import Path
from typing import TextIO

from meltzer.approval.policy import ApprovalChoice, ApprovalRequest, patch_preview
from meltzer.tools.request_user_input import Question

# First line of an approval prompt, by request type.
REQUEST_HEADINGS = {
    "mcp_tool_call": "meltzer wants to call an MCP tool:",
    "request_permissions": "meltzer asks for more sandbox permissions:",
    "network_access": "a sandboxed command wants to reach a host that is not on the allow list:",
    "web_search": "meltzer wants to search the web:",
}
PROMPT_RULE_HEADING = "a rule sends this command to review before it runs:"

# Terminal escape sequences: CSI, OSC (ended by BEL or ST), DCS/SOS/PM/APC (ended by ST),
# and two-character escapes.
ESCAPE_SEQUENCE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b[PX^_][^\x1b]*(?:\x1b\\)?"
    r"|\x1b[ -~]?"
)
# Characters that change the direction of the text around them.
BIDI_CONTROLS = frozenset("؜‎‏‪‫‬‭‮⁦⁧⁨⁩")
KEPT_CONTROLS = frozenset("\n\t")

# Command tools: the argument that holds the command, and the arguments shown as flags.
COMMAND_ARGUMENTS = {
    "shell": ("command", ("escalate", "background")),
    "exec_command": ("cmd", ("escalate", "tty")),
}


def plain_text(text: str) -> str:
    """text without terminal escape sequences and control characters (newlines and tabs are kept)."""
    text = ESCAPE_SEQUENCE.sub("", text.replace("\r\n", "\n"))
    return "".join(
        char
        for char in text
        if char in KEPT_CONTROLS or (unicodedata.category(char) != "Cc" and char not in BIDI_CONTROLS)
    )


def tool_call_text(name: str, arguments: str) -> str:
    """The line shown for a tool call: the command for command tools, else the tool name."""
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        parsed = None
    command_key, flag_names = COMMAND_ARGUMENTS.get(name, (None, ()))
    if command_key is not None and isinstance(parsed, dict) and isinstance(parsed.get(command_key), str):
        flags = [flag for flag in flag_names if parsed.get(flag)]
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        return f"$ {parsed[command_key]}{suffix}"
    return f"[{name}]"


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
        print(plain_text(message), file=self.stderr, flush=True)

    def assistant_message(self, text: str) -> None:
        stream = self.stdout if self.interactive else self.stderr
        print(plain_text(text), file=stream, flush=True)

    def final_message(self, text: str) -> None:
        if not self.interactive:
            print(plain_text(text), file=self.stdout, flush=True)

    def tool_call(self, name: str, arguments: str) -> None:
        self.info(tool_call_text(name, arguments))

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
            if request.tool in COMMAND_ARGUMENTS and not request.escalate:
                heading = PROMPT_RULE_HEADING
            else:
                heading = REQUEST_HEADINGS.get(request.tool, "meltzer wants to run outside the sandbox:")
            print(heading, file=self.stdout)
            print(json.dumps(request.planned_action(), indent=2), file=self.stdout)
            return
        print("meltzer wants to apply a patch that writes outside the writable paths:", file=self.stdout)
        for path in request.paths:
            print(plain_text(f"  {path}"), file=self.stdout)
        print("Patch:", file=self.stdout)
        print(plain_text(patch_preview(request.patch).rstrip("\n")), file=self.stdout)

    def ask_approval(self, request: ApprovalRequest) -> ApprovalChoice:
        self._show_request(request)
        if request.tool == "request_permissions":
            return self._ask_session("Grant? [y]es for this turn / [s]ession / [n]o: ")
        if request.tool == "network_access":
            return self._ask_session("Approve? [y]es once / [s]ession / [n]o: ")
        if request.suggested_prefix:
            question = (
                "Approve? [y]es once / [a]lways allow commands starting with "
                f"`{plain_text(shlex.join(request.suggested_prefix))}` / [n]o: "
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

    def _ask_session(self, question: str) -> ApprovalChoice:
        while True:
            print(question, end="", file=self.stdout, flush=True)
            answer = self._readline()
            if answer is None:
                print("", file=self.stdout)
                return ApprovalChoice.DENY
            answer = answer.strip().lower()
            if answer in ("y", "yes"):
                return ApprovalChoice.APPROVE_ONCE
            if answer in ("s", "session"):
                return ApprovalChoice.APPROVE_FOR_SESSION
            if answer in ("n", "no"):
                return ApprovalChoice.DENY

    def ask_questions(self, questions: list[Question]) -> dict[str, list[str]] | None:
        """Ask request_user_input questions: a number picks an option, other text is the user's own answer.

        None if input ended before every question was answered.
        """
        answers: dict[str, list[str]] = {}
        for question in questions:
            print("", file=self.stdout)
            print(plain_text(f"[{question.header}] {question.question}"), file=self.stdout)
            for number, option in enumerate(question.options, 1):
                print(plain_text(f"  {number}. {option.label}: {option.description}"), file=self.stdout)
            while True:
                print("Type a number, or your own answer: ", end="", file=self.stdout, flush=True)
                answer = self._readline()
                if answer is None:
                    print("", file=self.stdout)
                    return None
                answer = answer.strip()
                if answer:
                    break
            if answer.isascii() and answer.isdigit() and 1 <= int(answer) <= len(question.options):
                answer = question.options[int(answer) - 1].label
            answers[question.id] = [answer]
        return answers

    def confirm_feedback(self, question: str) -> bool:
        """Ask whether to send a feedback report."""
        while True:
            print(f"{question} [y]es / [n]o: ", end="", file=self.stdout, flush=True)
            answer = self._readline()
            if answer is None:
                print("", file=self.stdout)
                return False
            answer = answer.strip().lower()
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False

    def confirm_hook(self, description: str) -> bool:
        """Ask whether to trust a hook that has not been approved before."""
        print("", file=self.stdout)
        print("A hook in your configuration has not been trusted yet:", file=self.stdout)
        print(plain_text(description), file=self.stdout)
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
