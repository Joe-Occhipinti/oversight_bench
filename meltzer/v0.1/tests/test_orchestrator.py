from __future__ import annotations

import io
from pathlib import Path

import pytest

from meltzer.approval.orchestrator import (
    ApprovalOrchestrator,
    CommandRequest,
    PatchRequest,
    UserReviewer,
    make_reviewer,
)
from meltzer.approval.policy import (
    ApprovalChoice,
    ApprovalPolicy,
    ApprovalRequest,
    MAX_PATCH_CHARS,
    ApprovalSettings,
    ReviewerKind,
    ReviewVerdict,
    headless_conflict,
    patch_preview,
)
from meltzer.approval.rules import RuleSet, parse_rules
from meltzer.sandbox.policy import SandboxMode, SandboxPolicy
from meltzer.ui import TerminalUI


class RecordingReviewer:
    def __init__(self, verdict: ReviewVerdict):
        self.verdict = verdict
        self.requests: list[ApprovalRequest] = []

    def review(self, request: ApprovalRequest) -> ReviewVerdict:
        self.requests.append(request)
        return self.verdict


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    (path / ".meltzer").mkdir(parents=True)
    return path


def orchestrator(
    work: Path,
    reviewer: RecordingReviewer,
    *,
    policy: ApprovalPolicy = ApprovalPolicy.ON_REQUEST,
    mode: SandboxMode = SandboxMode.WORKSPACE_WRITE,
    rules_text: str = "",
) -> ApprovalOrchestrator:
    rules, warnings = parse_rules(rules_text, work / "test.rules")
    assert warnings == []
    return ApprovalOrchestrator(
        ApprovalSettings(policy=policy, reviewer=ReviewerKind.USER),
        SandboxPolicy.for_session(mode, work),
        reviewer,
        RuleSet(rules, work.parent / "rules"),
    )


def approve() -> RecordingReviewer:
    return RecordingReviewer(ReviewVerdict(approved=True, rationale="approved by user"))


def deny() -> RecordingReviewer:
    return RecordingReviewer(ReviewVerdict(approved=False, rationale="denied by user"))


@pytest.mark.parametrize(
    "command, escalate, options, approved, run, sandboxed, reason, reviewed",
    [
        ("pytest -q", False, {}, True, True, True, "runs in the sandbox", False),
        ("make install", True, {}, False, False, False, "denied by user", True),
        ("make install", True, {"policy": ApprovalPolicy.NEVER}, True, False, False, "approval_policy = never", False),
        ("make install", True, {"mode": SandboxMode.FULL_ACCESS}, False, True, False, None, False),
        ("cargo test --all", True, {"rules_text": 'prefix_rule(pattern=["cargo", "test"])\n'}, False, True, False,
         "allowed by rule cargo test", False),
    ],
    ids=["plain", "escalated-denied", "policy-never", "full-access", "allow-rule"],
)
def test_command_decisions(work, command, escalate, options, approved, run, sandboxed, reason, reviewed):
    reviewer = approve() if approved else deny()
    extra = {"escalate": True, "justification": "needed"} if escalate else {}
    request = CommandRequest(command, work, **extra)
    decision = orchestrator(work, reviewer, **options).decide_command(request)
    assert decision.run is run
    if run:
        assert decision.sandboxed is sandboxed
    if reason is not None:
        assert reason in decision.reason
    assert bool(reviewer.requests) is reviewed


def test_escalation_goes_to_the_reviewer(work):
    reviewer = approve()
    request = CommandRequest("curl -sO https://example.com/x.tar.gz", work, escalate=True, justification="download")
    decision = orchestrator(work, reviewer).decide_command(request)
    assert decision.run and not decision.sandboxed
    assert decision.reason == "approved by user"
    (sent,) = reviewer.requests
    assert sent.tool == "shell" and sent.command == request.command
    assert sent.justification == "download" and sent.escalate
    assert sent.suggested_prefix == ("curl", "-sO", "https://example.com/x.tar.gz")


def test_forbidden_rule_rejects_even_without_escalation(work):
    reviewer = approve()
    decision = orchestrator(
        work, reviewer, rules_text='prefix_rule(pattern=["rm", "-rf"], decision="forbidden", justification="no")\n'
    ).decide_command(CommandRequest("rm -rf build", work))
    assert not decision.run
    assert decision.reason.startswith("forbidden by rule rm -rf") and decision.reason.endswith(": no")
    assert reviewer.requests == []


def test_always_allow_answer_saves_a_rule(work):
    reviewer = RecordingReviewer(ReviewVerdict(approved=True, rationale="approved by user", remember_prefix=("cargo", "fetch")))
    orch = orchestrator(work, reviewer)
    decision = orch.decide_command(CommandRequest("cargo fetch", work, escalate=True, justification="network"))
    assert decision.run and "are now allowed" in decision.reason
    assert (work.parent / "rules" / "default.rules").is_file()
    again = orch.decide_command(CommandRequest("cargo fetch", work, escalate=True, justification="network"))
    assert again.reason.startswith("allowed by rule")
    assert len(reviewer.requests) == 1


@pytest.mark.parametrize(
    "relative, options, approved, apply, reviewed",
    [
        ("src/a.py", {}, False, True, False),
        ("../outside.txt", {}, False, False, True),
        ("../outside.txt", {"policy": ApprovalPolicy.NEVER}, True, False, False),
    ],
    ids=["inside", "outside-denied", "outside-policy-never"],
)
def test_patch_decisions(work, relative, options, approved, apply, reviewed):
    reviewer = approve() if approved else deny()
    decision = orchestrator(work, reviewer, **options).decide_patch(PatchRequest(paths=((work / relative).resolve(),)))
    assert decision.apply is apply
    assert bool(reviewer.requests) is reviewed


@pytest.mark.parametrize("relative", ["../outside.txt", ".meltzer/config.toml"])
def test_patch_outside_writable_paths_goes_to_the_reviewer(work, relative):
    reviewer = approve()
    target = (work / relative).resolve()
    text = f"*** Begin Patch\n*** Add File: {target}\n+x\n*** End Patch\n"
    decision = orchestrator(work, reviewer).decide_patch(PatchRequest(paths=(work / "a.py", target), patch=text))
    assert decision.apply
    (sent,) = reviewer.requests
    assert sent.tool == "apply_patch" and sent.paths == (target,)
    action = sent.planned_action()
    assert action["patch"] == text and action["paths"] == [str(target)]
    assert "justification" not in action


def test_user_prompt_for_a_patch_shows_paths_and_patch(tmp_path):
    stdout = io.StringIO()
    ui = TerminalUI(interactive=True, stdin=io.StringIO("y\n"), stdout=stdout, stderr=io.StringIO())
    target = tmp_path / "outside.txt"
    text = f"*** Begin Patch\n*** Add File: {target}\n+hello\n*** End Patch\n"
    choice = ui.ask_approval(ApprovalRequest(tool="apply_patch", cwd=tmp_path, paths=(target,), patch=text))
    assert choice == ApprovalChoice.APPROVE_ONCE
    shown = stdout.getvalue()
    assert "meltzer wants to apply a patch that writes outside the writable paths:" in shown
    assert f"  {target}\n" in shown and "+hello\n*** End Patch\n" in shown
    assert "outside the sandbox" not in shown


def test_patch_preview(tmp_path):
    text = "*** Begin Patch\n" + "+x\n" * MAX_PATCH_CHARS
    action = ApprovalRequest(tool="apply_patch", cwd=tmp_path, paths=(tmp_path / "a",), patch=text).planned_action()
    shown = action["patch"]
    assert shown.startswith(text[:MAX_PATCH_CHARS])
    assert shown.endswith(f"[... {len(text) - MAX_PATCH_CHARS} more characters of the patch not shown]")
    assert patch_preview("short") == "short"


@pytest.mark.parametrize(
    "choice, approved, remembered",
    [
        (ApprovalChoice.APPROVE_ONCE, True, ()),
        (ApprovalChoice.ALWAYS_ALLOW_PREFIX, True, ("cargo", "fetch")),
        (ApprovalChoice.DENY, False, ()),
    ],
)
def test_user_reviewer_answers(choice, approved, remembered, tmp_path):
    reviewer = UserReviewer(lambda request: choice)
    request = ApprovalRequest(tool="shell", cwd=tmp_path, command="cargo fetch", suggested_prefix=("cargo", "fetch"))
    verdict = reviewer.review(request)
    assert verdict.approved is approved
    assert verdict.remember_prefix == remembered


def test_configured_reviewer_is_used():
    sentinel = approve()
    assert isinstance(make_reviewer(ReviewerKind.USER, lambda request: ApprovalChoice.DENY), UserReviewer)
    assert make_reviewer(ReviewerKind.MONITOR, lambda request: ApprovalChoice.DENY, monitor=lambda: sentinel) is sentinel


def test_headless_sessions_need_a_monitor_or_policy_never():
    message = headless_conflict(ApprovalSettings(ApprovalPolicy.ON_REQUEST, ReviewerKind.USER))
    assert message is not None
    assert 'approvals_reviewer = "monitor"' in message and 'approval_policy = "never"' in message
    assert headless_conflict(ApprovalSettings(ApprovalPolicy.ON_REQUEST, ReviewerKind.MONITOR)) is None
    assert headless_conflict(ApprovalSettings(ApprovalPolicy.NEVER, ReviewerKind.USER)) is None
