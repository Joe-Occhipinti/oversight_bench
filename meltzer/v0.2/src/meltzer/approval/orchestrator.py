"""Decides how each command or file change runs."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from meltzer.approval.policy import (
    ApprovalChoice,
    ApprovalPolicy,
    ApprovalRequest,
    ApprovalSettings,
    Reviewer,
    ReviewerKind,
    ReviewVerdict,
)
from meltzer.approval.rules import RuleDecision, RuleMatch, RuleSet
from meltzer.sandbox.policy import SandboxPolicy

from meltzer.telemetry.events import SOURCE_AUTOMATED_REVIEWER, SOURCE_CONFIG, SOURCE_USER

if TYPE_CHECKING:
    from meltzer.hooks import HookRunner
    from meltzer.telemetry.events import SessionTelemetry


@dataclass(frozen=True)
class Decision:
    """How a shell command runs."""

    run: bool
    # True: the command runs in the sandbox. False: it runs without it, or does
    # not run at all.
    sandboxed: bool
    reason: str
    # The reviewer approved for the rest of the session, not only this time.
    for_session: bool = False

    @classmethod
    def in_sandbox(cls, reason: str) -> Decision:
        return cls(run=True, sandboxed=True, reason=reason)

    @classmethod
    def unsandboxed(cls, reason: str) -> Decision:
        return cls(run=True, sandboxed=False, reason=reason)

    @classmethod
    def rejected(cls, reason: str) -> Decision:
        return cls(run=False, sandboxed=False, reason=reason)


@dataclass(frozen=True)
class PatchDecision:
    """Whether a patch is applied."""

    apply: bool
    reason: str


@dataclass(frozen=True)
class CommandRequest:
    command: str
    cwd: Path
    escalate: bool = False
    justification: str = ""
    background: bool = False
    # The tool that runs the command: "shell" or "exec_command".
    tool: str = "shell"
    tty: bool = False


@dataclass(frozen=True)
class PatchRequest:
    paths: tuple[Path, ...]
    patch: str = ""


@dataclass(frozen=True)
class McpCallRequest:
    server: str
    tool: str
    arguments: dict[str, Any]
    cwd: Path
    description: str = ""
    annotations: dict[str, Any] | None = None
    # False when the tool's approval mode lets the call run without review.
    needs_review: bool = True


@dataclass(frozen=True)
class PermissionsRequest:
    reason: str
    cwd: Path
    # A single host or IP address, with an optional port.
    network_target: str | None = None
    write_paths: tuple[Path, ...] = ()

    def permissions(self) -> dict[str, Any]:
        requested: dict[str, Any] = {}
        if self.network_target is not None:
            requested["network"] = {"target": self.network_target}
        if self.write_paths:
            requested["write"] = [str(p) for p in self.write_paths]
        return requested


@dataclass(frozen=True)
class NetworkRequest:
    host: str
    port: int
    protocol: str
    command: str
    cwd: Path


@dataclass(frozen=True)
class WebSearchRequest:
    query: str
    cwd: Path


class UserReviewer:
    """Asks the person at the terminal to approve once, always allow the command's prefix, or deny."""

    def __init__(self, ask: Callable[[ApprovalRequest], ApprovalChoice]):
        self._ask = ask

    def review(self, request: ApprovalRequest) -> ReviewVerdict:
        choice = self._ask(request)
        if choice == ApprovalChoice.APPROVE_FOR_SESSION:
            return ReviewVerdict(approved=True, rationale="approved by user for the session", for_session=True)
        if choice == ApprovalChoice.ALWAYS_ALLOW_PREFIX and request.suggested_prefix:
            return ReviewVerdict(
                approved=True,
                rationale="approved by user",
                remember_prefix=request.suggested_prefix,
            )
        if choice in (ApprovalChoice.APPROVE_ONCE, ApprovalChoice.ALWAYS_ALLOW_PREFIX):
            return ReviewVerdict(approved=True, rationale="approved by user")
        return ReviewVerdict(approved=False, rationale="denied by user")


def make_reviewer(
    kind: ReviewerKind,
    ask_user: Callable[[ApprovalRequest], ApprovalChoice],
    monitor: Callable[[], Reviewer] | None = None,
) -> Reviewer:
    if kind == ReviewerKind.USER:
        return UserReviewer(ask_user)
    if kind == ReviewerKind.MONITOR and monitor is not None:
        return monitor()
    raise ValueError(f"unsupported approvals_reviewer: {kind}")


class ApprovalOrchestrator:
    def __init__(
        self,
        settings: ApprovalSettings,
        sandbox: SandboxPolicy,
        reviewer: Reviewer,
        rules: RuleSet,
        hooks: HookRunner | None = None,
    ):
        self.settings = settings
        self.sandbox = sandbox
        self.reviewer = reviewer
        self.rules = rules
        # PermissionRequest hooks run before the reviewer.
        self.hooks = hooks
        # Records a tool decision event for each request that goes to review.
        self.telemetry: SessionTelemetry | None = None

    def decide_command(self, request: CommandRequest) -> Decision:
        match = self.rules.evaluate(request.command)
        if match is not None and match.decision == RuleDecision.FORBIDDEN:
            rule = match.rules[0]
            reason = f"forbidden by rule {rule.describe()}"
            if rule.justification:
                reason += f": {rule.justification}"
            return Decision.rejected(reason)
        if match is not None and match.decision == RuleDecision.PROMPT:
            return self._review_prompted(request, match)
        if match is not None and match.decision == RuleDecision.ALLOW:
            return Decision.unsandboxed(f"allowed by rule {match.describe()}")
        if not self.sandbox.is_sandboxed:
            return Decision.unsandboxed("sandbox_mode is full-access")
        if not request.escalate:
            return Decision.in_sandbox("runs in the sandbox")
        approval = ApprovalRequest(
            tool=request.tool,
            cwd=request.cwd,
            command=request.command,
            justification=request.justification,
            escalate=True,
            background=request.background,
            tty=request.tty,
            suggested_prefix=self.rules.suggest_prefix(request.command),
        )
        return self._review(approval)

    def _review_prompted(self, request: CommandRequest, match: RuleMatch) -> Decision:
        """Review a command that a prompt rule matched; it runs in the sandbox unless escalation was asked."""
        approval = ApprovalRequest(
            tool=request.tool,
            cwd=request.cwd,
            command=request.command,
            justification=request.justification,
            escalate=request.escalate,
            background=request.background,
            tty=request.tty,
        )
        decision = self._review(approval)
        rule = match.rules[0]
        note = f"review required by rule {rule.describe()}"
        if rule.justification:
            note += f": {rule.justification}"
        if not decision.run:
            return Decision.rejected(f"{decision.reason}; {note}")
        if request.escalate or not self.sandbox.is_sandboxed:
            return Decision.unsandboxed(f"{decision.reason}; {note}")
        return Decision.in_sandbox(f"{decision.reason}; {note}")

    def decide_mcp_call(self, request: McpCallRequest) -> Decision:
        """Whether an MCP tool call runs."""
        if not request.needs_review:
            return Decision.unsandboxed(f"{request.server}/{request.tool} runs without review")
        approval = ApprovalRequest(
            tool="mcp_tool_call",
            cwd=request.cwd,
            server=request.server,
            mcp_tool=request.tool,
            arguments=dict(request.arguments),
            tool_description=request.description,
            annotations=request.annotations,
        )
        return self._review(approval)

    def decide_permissions(self, request: PermissionsRequest) -> Decision:
        """Whether extra sandbox permissions are granted."""
        approval = ApprovalRequest(
            tool="request_permissions",
            cwd=request.cwd,
            justification=request.reason,
            permissions=request.permissions(),
        )
        return self._review(approval)

    def decide_network(self, request: NetworkRequest) -> Decision:
        """Whether a sandboxed command may reach a host that is not on the allow list."""
        approval = ApprovalRequest(
            tool="network_access",
            cwd=request.cwd,
            command=request.command,
            host=request.host,
            port=request.port,
            protocol=request.protocol,
        )
        return self._review(approval)

    def decide_web_search(self, request: WebSearchRequest) -> Decision:
        """Whether a web search is sent."""
        approval = ApprovalRequest(tool="web_search", cwd=request.cwd, query=request.query)
        return self._review(approval)

    def decide_patch(self, request: PatchRequest) -> PatchDecision:
        outside = tuple(p for p in request.paths if not self.sandbox.can_write(p))
        if not outside:
            return PatchDecision(apply=True, reason="all paths are writable")
        approval = ApprovalRequest(
            tool="apply_patch",
            cwd=self.sandbox.root,
            paths=outside,
            patch=request.patch,
        )
        decision = self._review(approval)
        return PatchDecision(apply=decision.run, reason=decision.reason)

    def _review(self, approval: ApprovalRequest) -> Decision:
        if self.settings.policy == ApprovalPolicy.NEVER:
            self._record_decision(approval.tool, approved=False, source=SOURCE_CONFIG)
            return Decision.rejected(
                "escalation is disabled (approval_policy = never); only sandboxed actions "
                "and commands allowed by prefix rules can run"
            )
        if self.hooks is not None:
            check = self.hooks.permission_request(approval)
            if check.decision == "deny":
                self._record_decision(approval.tool, approved=False, source=SOURCE_CONFIG)
                return Decision.rejected(f"denied by a PermissionRequest hook: {check.reason}")
        verdict = self.reviewer.review(approval)
        source = SOURCE_AUTOMATED_REVIEWER if self.settings.reviewer == ReviewerKind.MONITOR else SOURCE_USER
        self._record_decision(approval.tool, approved=verdict.approved, source=source, for_session=verdict.for_session)
        if verdict.approved:
            reason = verdict.rationale or "approved"
            if verdict.remember_prefix:
                reason += self._remember(verdict.remember_prefix)
            return Decision(run=True, sandboxed=False, reason=reason, for_session=verdict.for_session)
        return Decision.rejected(verdict.rationale or "denied")

    def _record_decision(self, tool: str, *, approved: bool, source: str, for_session: bool = False) -> None:
        if self.telemetry is not None:
            self.telemetry.tool_decision(tool, approved=approved, source=source, for_session=for_session)

    def _remember(self, prefix: tuple[str, ...]) -> str:
        try:
            path = self.rules.allow_always(prefix)
        except (OSError, ValueError) as exc:
            return f"; the prefix rule was not saved: {exc}"
        return f"; commands starting with {shlex.join(prefix)!r} are now allowed ({path})"
