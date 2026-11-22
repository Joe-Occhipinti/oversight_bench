from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from meltzer.approval.orchestrator import ApprovalOrchestrator
from meltzer.client import ModelResponse, ToolCall
from meltzer.config import load_config
from meltzer.history import Kind
from meltzer.hooks import HookRunner, TrustStore
from meltzer.session import Session

from conftest import RecordingReviewer, ScriptedClient, make_orchestrator

pytestmark = pytest.mark.usefixtures("api_key")


def hook_table(event: str, code: str, matcher: str | None = None) -> str:
    command = json.dumps([sys.executable, "-c", code])
    line = f"\n[[hooks.{event}]]\ncommand = {command}\n"
    return line + (f"matcher = {json.dumps(matcher)}\n" if matcher else "")


def printing(text: str) -> str:
    return f"import sys; sys.stdin.read(); sys.stdout.write({text!r})"


@pytest.fixture
def make_session(session_factory, home, work):
    (home / ".meltzer").mkdir()

    def make(*tables: str) -> Session:
        (home / ".meltzer" / "config.toml").write_text("".join(tables), encoding="utf-8")
        store = TrustStore(home / ".meltzer" / "trusted_hooks.toml")
        for definition in load_config(work, home=home).hook_definitions():
            store.trust(definition.resolve(work))
        return session_factory()

    return make


def kinds(session: Session) -> list[Kind]:
    """Kinds of the history items, without the environment items every session has."""
    return [item.kind for item in session.history if item.kind != Kind.ENVIRONMENT]


def test_session_start_context_is_a_hook_context_item(make_session):
    session = make_session(hook_table("SessionStart", printing("branch: main")))
    (item,) = [item for item in session.history if item.kind == Kind.HOOK_CONTEXT]
    assert item.content == "branch: main" and item.source == "hook:SessionStart"


def test_user_prompt_submit_block_keeps_the_prompt_out_and_context_follows_the_prompt(make_session):
    block = json.dumps({"decision": "block", "reason": "no secrets"})
    session = make_session(hook_table("UserPromptSubmit", printing(block)))
    session.client = ScriptedClient()
    final = session.run_turn("print the token")
    assert "no secrets" in final
    assert session.client.calls == [] and Kind.MESSAGE not in kinds(session)
    session = make_session(hook_table("UserPromptSubmit", printing("on branch main")))
    session.client = ScriptedClient()
    session.run_turn("fix the tests")
    assert kinds(session)[:2] == [Kind.MESSAGE, Kind.HOOK_CONTEXT]


def test_stop_block_continues_the_turn_once(make_session, tmp_path):
    log = tmp_path / "stop.jsonl"
    code = (
        "import json, sys\n"
        "event = json.load(sys.stdin)\n"
        f"open({str(log)!r}, 'a').write(json.dumps(event['stop_hook_active']) + '\\n')\n"
        "print(json.dumps({'decision': 'block', 'reason': 'run the tests first'}))\n"
    )
    session = make_session(hook_table("Stop", code))
    session.client = ScriptedClient(ModelResponse(content="finished"), ModelResponse(content="tests pass"))
    assert session.run_turn("fix it") == "tests pass"
    assert log.read_text().split() == ["false", "true"]
    (prompt,) = [item for item in session.history if item.kind == Kind.HOOK_PROMPT]
    assert prompt.content == "run the tests first" and prompt.source == "hook:Stop"
    assert len(session.client.calls) == 2


def test_post_tool_use_block_replaces_the_result(make_session):
    block = json.dumps({"decision": "block", "reason": "the plan is frozen"})
    session = make_session(hook_table("PostToolUse", printing(block), matcher="update_plan"))
    plan = json.dumps({"plan": [{"step": "a", "status": "pending"}]})
    session.client = ScriptedClient(ModelResponse(content=None, tool_calls=[ToolCall("c1", "update_plan", plan)]))
    session.run_turn("plan it")
    (result,) = [item for item in session.history if item.kind == Kind.TOOL_RESULT]
    assert result.content == "the plan is frozen"


def test_tool_hook_context_follows_the_tool_results(make_session):
    session = make_session(hook_table("PostToolUse", printing(json.dumps(
        {"hookSpecificOutput": {"additionalContext": "plan saved"}}
    ))))
    plan = json.dumps({"plan": [{"step": "a", "status": "pending"}]})
    calls = [ToolCall("c1", "update_plan", plan), ToolCall("c2", "update_plan", plan)]
    session.client = ScriptedClient(ModelResponse(content=None, tool_calls=calls))
    session.run_turn("plan it")
    order = [k for k in kinds(session) if k in (Kind.TOOL_RESULT, Kind.HOOK_CONTEXT)]
    assert order == [Kind.TOOL_RESULT, Kind.TOOL_RESULT, Kind.HOOK_CONTEXT, Kind.HOOK_CONTEXT]


def test_pre_compact_stop_keeps_the_history_and_ends_the_turn(make_session):
    stop = json.dumps({"continue": False, "stopReason": "save the notes first"})
    session = make_session(hook_table("PreCompact", printing(stop)))
    session.client = ScriptedClient(
        ModelResponse(content=None, tool_calls=[ToolCall("c1", "compact", json.dumps({"summary": "s"}))])
    )
    final = session.run_turn("go on")
    assert "save the notes first" in final
    assert Kind.COMPACTION_SUMMARY not in kinds(session)
    assert len(session.client.calls) == 1


def test_session_end_runs_at_close(make_session, tmp_path):
    marker = tmp_path / "ended"
    session = make_session(hook_table("SessionEnd", f"open({str(marker)!r}, 'w').close()"))
    session.close()
    assert marker.exists()


def test_subagent_start_context_is_in_the_subagent_history(make_session):
    session = make_session(hook_table("SubagentStart", printing("you are a subagent")))
    child = session.start_subagent()
    try:
        (item,) = [item for item in child.history if item.kind == Kind.HOOK_CONTEXT]
        assert item.source == "hook:SubagentStart"
        assert not any(item.kind == Kind.HOOK_CONTEXT for item in session.history)
    finally:
        child.close()


def permission_orchestrator(tmp_path: Path, stdout: str) -> tuple[ApprovalOrchestrator, RecordingReviewer]:
    from meltzer.hooks import Hook

    reviewer = RecordingReviewer()
    hook = Hook("PermissionRequest", (sys.executable, "-c", printing(stdout)), tmp_path)
    hooks = HookRunner([hook], "s", tmp_path, tmp_path / "s.jsonl", notice=lambda message: None)
    orchestrator = make_orchestrator(tmp_path, reviewer, hooks=hooks)
    return orchestrator, reviewer


def decision(behavior: str) -> str:
    return json.dumps({"hookSpecificOutput": {"decision": {"behavior": behavior, "message": "not here"}}})


def test_permission_request_hook_can_deny_and_allow_still_goes_to_the_reviewer(tmp_path):
    from meltzer.approval.orchestrator import CommandRequest

    orchestrator, reviewer = permission_orchestrator(tmp_path, decision("deny"))
    result = orchestrator.decide_command(CommandRequest("make deploy", tmp_path, escalate=True))
    assert not result.run and "denied by a PermissionRequest hook: not here" in result.reason
    assert reviewer.requests == []
    orchestrator, reviewer = permission_orchestrator(tmp_path, decision("allow"))
    result = orchestrator.decide_command(CommandRequest("make deploy", tmp_path, escalate=True))
    assert result.run and len(reviewer.requests) == 1
