from __future__ import annotations

import io
from pathlib import Path

import pytest

from meltzer.approval.orchestrator import (
    ApprovalOrchestrator,
    CommandRequest,
    McpCallRequest,
    NetworkRequest,
    PatchRequest,
    PermissionsRequest,
    UserReviewer,
    WebSearchRequest,
    make_reviewer,
)
from meltzer.approval.policy import (
    ApprovalChoice,
    ApprovalPolicy,
    ApprovalRequest,
    MAX_PATCH_CHARS,
    MAX_TOOL_DESCRIPTION_CHARS,
    ApprovalSettings,
    ReviewerKind,
    headless_conflict,
    patch_preview,
)
from meltzer.approval.rules import parse_rules
from meltzer.sandbox.policy import SandboxMode
from meltzer.ui import TerminalUI

from conftest import RecordingReviewer, make_orchestrator


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    (path / ".meltzer").mkdir(parents=True)
    return path


def orchestrator(work: Path, reviewer: RecordingReviewer, *, rules_text: str = "", **kwargs) -> ApprovalOrchestrator:
    rules, warnings = parse_rules(rules_text, work / "test.rules")
    assert warnings == []
    return make_orchestrator(work, reviewer, rules=rules, **kwargs)


def approve() -> RecordingReviewer:
    return RecordingReviewer()


def deny() -> RecordingReviewer:
    return RecordingReviewer(approved=False)


FORBIDDEN = 'prefix_rule(pattern=["rm", "-rf"], decision="forbidden", justification="no")\n'


@pytest.mark.parametrize(
    "command, escalate, options, approved, run, sandboxed, reason, reviewed",
    [
        ("pytest -q", False, {}, True, True, True, "runs in the sandbox", False),
        ("make install", True, {}, False, False, False, "denied by user", True),
        ("make install", True, {"policy": ApprovalPolicy.NEVER}, True, False, False, "approval_policy = never", False),
        ("make install", True, {"mode": SandboxMode.FULL_ACCESS}, False, True, False, None, False),
        ("cargo test --all", True, {"rules_text": 'prefix_rule(pattern=["cargo", "test"])\n'}, False, True, False,
         "allowed by rule cargo test", False),
        ("rm -rf build", False, {"rules_text": FORBIDDEN}, True, False, False, ("forbidden by rule rm -rf", ": no"), False),
        ("rm -rf build", False, {"rules_text": FORBIDDEN, "policy": ApprovalPolicy.NEVER}, True, False, False,
         ("forbidden by rule rm -rf", ": no"), False),
    ],
    ids=["plain", "escalated-denied", "policy-never", "full-access", "allow-rule", "forbidden", "forbidden-never"],
)
def test_command_decisions(work, command, escalate, options, approved, run, sandboxed, reason, reviewed):
    reviewer = approve() if approved else deny()
    extra = {"escalate": True, "justification": "needed"} if escalate else {}
    request = CommandRequest(command, work, **extra)
    decision = orchestrator(work, reviewer, **options).decide_command(request)
    assert decision.run is run
    if run:
        assert decision.sandboxed is sandboxed
    for part in (reason,) if isinstance(reason, str) else reason or ():
        assert part in decision.reason
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


PROMPT_RULE = 'prefix_rule(pattern=["git", "push"], decision="prompt", justification="pushes need review")\n'


@pytest.mark.parametrize(
    "escalate, options, approved, run, sandboxed",
    [
        (False, {}, True, True, True),
        (True, {}, True, True, False),
        (False, {}, False, False, False),
        (False, {"mode": SandboxMode.FULL_ACCESS}, True, True, False),
    ],
    ids=["approved", "approved-escalated", "denied", "full-access"],
)
def test_prompt_rule_sends_the_command_to_review(work, escalate, options, approved, run, sandboxed):
    reviewer = approve() if approved else deny()
    extra = {"escalate": True, "justification": "publish"} if escalate else {}
    decision = orchestrator(work, reviewer, rules_text=PROMPT_RULE, **options).decide_command(
        CommandRequest("git push origin main", work, **extra)
    )
    assert (decision.run, decision.sandboxed) == (run, sandboxed)
    assert "pushes need review" in decision.reason
    sent = reviewer.requests[0]
    assert sent.escalate is escalate and sent.suggested_prefix == ()
    never = orchestrator(work, approve(), rules_text=PROMPT_RULE, policy=ApprovalPolicy.NEVER)
    assert not never.decide_command(CommandRequest("git push origin main", work)).run


def test_session_approval_is_passed_on(work):
    request = PermissionsRequest("install deps", cwd=work, network_target="pypi.org")
    session = orchestrator(work, UserReviewer(lambda request: ApprovalChoice.APPROVE_FOR_SESSION))
    decision = session.decide_permissions(request)
    assert decision.run and decision.for_session
    once = orchestrator(work, approve()).decide_permissions(request)
    assert once.run and not once.for_session


@pytest.mark.parametrize("tool", ["request_permissions", "network_access"])
def test_user_is_asked_once_or_session_for_a_grant_and_a_network_request(tmp_path, tool):
    for answer, expected in (("s\n", ApprovalChoice.APPROVE_FOR_SESSION), ("y\n", ApprovalChoice.APPROVE_ONCE)):
        ui = TerminalUI(interactive=True, stdin=io.StringIO(answer), stdout=io.StringIO(), stderr=io.StringIO())
        request = ApprovalRequest(tool=tool, cwd=tmp_path, host="pypi.org", port=443, protocol="https", permissions={})
        assert ui.ask_approval(request) == expected


def test_always_allow_answer_saves_a_rule(work):
    reviewer = RecordingReviewer(remember_prefix=("cargo", "fetch"))
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
    ui.ask_approval(ApprovalRequest(tool="apply_patch", cwd=tmp_path, paths=(tmp_path / ("a" + chr(27) + "[8m"),), patch=text))
    assert chr(27) not in stdout.getvalue()


@pytest.mark.parametrize(
    "tool, escalate, heading",
    [
        ("shell", False, "a rule sends this command to review before it runs:"),
        ("exec_command", True, "meltzer wants to run outside the sandbox:"),
    ],
)
def test_user_prompt_heading_for_a_command_review(tmp_path, tool, escalate, heading):
    stdout = io.StringIO()
    ui = TerminalUI(interactive=True, stdin=io.StringIO("n\n"), stdout=stdout, stderr=io.StringIO())
    ui.ask_approval(ApprovalRequest(tool=tool, cwd=tmp_path, command="make deploy", escalate=escalate))
    assert stdout.getvalue().splitlines()[1] == heading


@pytest.mark.parametrize(
    "name, arguments, shown",
    [
        ("shell", '{"command": "ls", "escalate": true}', "$ ls  [escalate]"),
        ("exec_command", '{"cmd": "python3 -i", "tty": true}', "$ python3 -i  [tty]"),
        ("exec_command", '{"cmd": "make"}', "$ make"),
        ("write_stdin", '{"session_id": 1, "chars": "x"}', "[write_stdin]"),
    ],
)
def test_tool_call_shows_the_command(name, arguments, shown):
    stderr = io.StringIO()
    TerminalUI(interactive=True, stdout=io.StringIO(), stderr=stderr).tool_call(name, arguments)
    assert stderr.getvalue().strip() == shown


def test_planned_action_of_a_network_request(tmp_path):
    network_action = ApprovalRequest(
        tool="network_access", cwd=tmp_path, command="pip install requests", host="pypi.org", port=443, protocol="https",
    ).planned_action()
    assert (network_action["host"], network_action["port"], network_action["protocol"]) == ("pypi.org", 443, "https")
    assert network_action["command"] == "pip install requests"


@pytest.mark.parametrize(
    "decide, request_type",
    [
        ("decide_mcp_call", lambda work: McpCallRequest("ops", "restart_service", {"name": "api"}, cwd=work)),
        ("decide_permissions", lambda work: PermissionsRequest("install deps", cwd=work, network_target="pypi.org")),
        ("decide_network", lambda work: NetworkRequest("pypi.org", 443, "https", command="pip install x", cwd=work)),
        ("decide_web_search", lambda work: WebSearchRequest("bubblewrap seccomp", cwd=work)),
    ],
)
def test_other_requests_go_to_the_reviewer(work, decide, request_type):
    reviewer = approve()
    decision = getattr(orchestrator(work, reviewer), decide)(request_type(work))
    assert decision.run and len(reviewer.requests) == 1
    denied = getattr(orchestrator(work, deny()), decide)(request_type(work))
    assert not denied.run
    never = getattr(orchestrator(work, approve(), policy=ApprovalPolicy.NEVER), decide)(request_type(work))
    assert not never.run and "approval_policy = never" in never.reason


def test_mcp_call_without_review(work):
    reviewer = approve()
    request = McpCallRequest("ops", "service_status", {"name": "api"}, cwd=work, needs_review=False)
    decision = orchestrator(work, reviewer).decide_mcp_call(request)
    assert decision.run and reviewer.requests == []


@pytest.mark.parametrize(
    "tool, field, limit, what",
    [("apply_patch", "patch", MAX_PATCH_CHARS, "patch"), ("mcp_tool_call", "tool_description", MAX_TOOL_DESCRIPTION_CHARS, "description")],
)
def test_long_patches_and_tool_descriptions_are_shortened(tmp_path, tool, field, limit, what):
    text = "x" * (limit * 2)
    extra = {"patch": text, "paths": (tmp_path / "a",)} if field == "patch" else {
        "server": "ops", "mcp_tool": "restart_service", "tool_description": text, "annotations": {"destructiveHint": True}
    }
    action = ApprovalRequest(tool=tool, cwd=tmp_path, **extra).planned_action()
    assert action[field] == text[:limit] + f"\n[... {limit} more characters of the {what} not shown]"
    if field == "tool_description":
        assert action["annotations"] == {"destructiveHint": True}
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
