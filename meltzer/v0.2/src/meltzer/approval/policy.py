"""Approval settings and the request/verdict types shared by reviewers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol


class ApprovalPolicy(StrEnum):
    # The model may ask to run a command outside the sandbox; a reviewer decides.
    ON_REQUEST = "on-request"
    # Requests to leave the sandbox are rejected.
    NEVER = "never"


class ReviewerKind(StrEnum):
    # Ask the person at the terminal.
    USER = "user"
    # A separate model session reviews each request (see meltzer.monitor).
    MONITOR = "monitor"


@dataclass(frozen=True)
class ApprovalSettings:
    policy: ApprovalPolicy
    reviewer: ReviewerKind


def headless_conflict(settings: ApprovalSettings) -> str | None:
    """Explain why these settings cannot work without a terminal, or return None."""
    if settings.reviewer == ReviewerKind.USER and settings.policy != ApprovalPolicy.NEVER:
        return (
            "approvals_reviewer = \"user\" needs an interactive terminal; "
            "for `meltzer exec` set approvals_reviewer = \"monitor\" or approval_policy = \"never\""
        )
    return None


MAX_PATCH_CHARS = 16_000
MAX_COMMAND_CHARS = 2_000
MAX_TOOL_DESCRIPTION_CHARS = 1_000

# Tools whose requests carry a shell command.
COMMAND_TOOLS = ("shell", "exec_command")


def _preview(text: str, limit: int, what: str) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[... {len(text) - limit} more characters of the {what} not shown]"


def patch_preview(text: str) -> str:
    return _preview(text, MAX_PATCH_CHARS, "patch")


def command_preview(text: str) -> str:
    return _preview(text, MAX_COMMAND_CHARS, "command")


def tool_description_preview(text: str) -> str:
    return _preview(text, MAX_TOOL_DESCRIPTION_CHARS, "description")


@dataclass(frozen=True)
class ApprovalRequest:
    """An action that needs a reviewer's decision before it runs.

    `tool` names the request type: "shell", "exec_command", "apply_patch",
    "mcp_tool_call", "request_permissions", "network_access" or "web_search".
    """

    tool: str
    cwd: Path
    justification: str = ""
    command: str | None = None
    escalate: bool = False
    background: bool = False
    # exec_command: the process gets a terminal.
    tty: bool = False
    paths: tuple[Path, ...] = field(default=())
    # Full text of the patch, for apply_patch requests.
    patch: str | None = None
    # Command prefix the reviewer may allow for future commands; empty when none can be offered.
    suggested_prefix: tuple[str, ...] = field(default=())
    # mcp_tool_call: the server, the tool and its arguments, and what the server says about the tool.
    server: str | None = None
    mcp_tool: str | None = None
    arguments: dict[str, Any] | None = None
    tool_description: str | None = None
    annotations: dict[str, Any] | None = None
    # request_permissions: the permissions asked for.
    permissions: dict[str, Any] | None = None
    # network_access: the destination; `command` is the command that tried to reach it.
    host: str | None = None
    port: int | None = None
    protocol: str | None = None
    # web_search: the query sent to the search service.
    query: str | None = None

    def planned_action(self) -> dict[str, Any]:
        action: dict[str, Any] = {"tool": self.tool}
        if self.command is not None:
            action["command"] = command_preview(self.command)
        if self.paths:
            action["paths"] = [str(p) for p in self.paths]
        if self.patch is not None:
            action["patch"] = patch_preview(self.patch)
        if self.server is not None:
            action["server"] = self.server
            action["tool_name"] = self.mcp_tool
            action["arguments"] = self.arguments or {}
            if self.tool_description:
                action["tool_description"] = tool_description_preview(self.tool_description)
            if self.annotations:
                action["annotations"] = self.annotations
        if self.permissions is not None:
            action["permissions"] = self.permissions
        if self.host is not None:
            action["host"] = self.host
            if self.port is not None:
                action["port"] = self.port
            if self.protocol is not None:
                action["protocol"] = self.protocol
        if self.query is not None:
            action["query"] = self.query
        action["cwd"] = str(self.cwd)
        if self.justification:
            action["justification"] = self.justification
        if self.tool in COMMAND_TOOLS:
            action["escalate"] = self.escalate
            if self.background:
                action["background"] = True
            if self.tty:
                action["tty"] = True
        return action


class ApprovalChoice(StrEnum):
    APPROVE_ONCE = "approve-once"
    # Approve for the rest of the session (offered for permission requests).
    APPROVE_FOR_SESSION = "approve-for-session"
    # Approve, and allow commands starting with the suggested prefix from now on.
    ALWAYS_ALLOW_PREFIX = "always-allow-prefix"
    DENY = "deny"


@dataclass(frozen=True)
class ReviewVerdict:
    approved: bool
    rationale: str = ""
    # Set when the reviewer allowed this prefix for future commands.
    remember_prefix: tuple[str, ...] = ()
    # The approval holds for the rest of the session.
    for_session: bool = False


class Reviewer(Protocol):
    def review(self, request: ApprovalRequest) -> ReviewVerdict: ...
