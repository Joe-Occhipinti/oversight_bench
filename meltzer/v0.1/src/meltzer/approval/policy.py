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


def patch_preview(text: str) -> str:
    if len(text) <= MAX_PATCH_CHARS:
        return text
    omitted = len(text) - MAX_PATCH_CHARS
    return f"{text[:MAX_PATCH_CHARS]}\n[... {omitted} more characters of the patch not shown]"


@dataclass(frozen=True)
class ApprovalRequest:
    """An action that needs a reviewer's decision before it runs."""

    tool: str
    cwd: Path
    justification: str = ""
    command: str | None = None
    escalate: bool = False
    background: bool = False
    paths: tuple[Path, ...] = field(default=())
    # Full text of the patch, for apply_patch requests.
    patch: str | None = None
    # Command prefix the reviewer may allow for future commands; empty when none can be offered.
    suggested_prefix: tuple[str, ...] = field(default=())

    def planned_action(self) -> dict[str, Any]:
        action: dict[str, Any] = {"tool": self.tool}
        if self.command is not None:
            action["command"] = self.command
        if self.paths:
            action["paths"] = [str(p) for p in self.paths]
        if self.patch is not None:
            action["patch"] = patch_preview(self.patch)
        action["cwd"] = str(self.cwd)
        if self.justification:
            action["justification"] = self.justification
        if self.tool == "shell":
            action["escalate"] = self.escalate
            if self.background:
                action["background"] = True
        return action


class ApprovalChoice(StrEnum):
    APPROVE_ONCE = "approve-once"
    # Approve, and allow commands starting with the suggested prefix from now on.
    ALWAYS_ALLOW_PREFIX = "always-allow-prefix"
    DENY = "deny"


@dataclass(frozen=True)
class ReviewVerdict:
    approved: bool
    rationale: str = ""
    # Set when the reviewer allowed this prefix for future commands.
    remember_prefix: tuple[str, ...] = ()


class Reviewer(Protocol):
    def review(self, request: ApprovalRequest) -> ReviewVerdict: ...
