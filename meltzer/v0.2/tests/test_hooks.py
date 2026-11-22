from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path

import pytest

from meltzer.approval.policy import ApprovalRequest
from meltzer.hook_output import HOOK_OUTPUT_LIMIT, HookOutcome, parse_output, preview
from meltzer.hooks import (
    HOOK_EVENTS,
    PERMISSION_REQUEST,
    POST_COMPACT,
    POST_TOOL_USE,
    PRE_COMPACT,
    PRE_TOOL_USE,
    SESSION_END,
    SESSION_START,
    STOP,
    SUBAGENT_START,
    SUBAGENT_STOP,
    USER_PROMPT_SUBMIT,
    Hook,
    HookConfigError,
    HookDefinition,
    HookRunner,
    TrustStore,
    parse_hook_entries,
)


def python_hook(event: str, code: str, root: Path, matcher: str = "*") -> Hook:
    return Hook(event=event, command=(sys.executable, "-c", code), cwd=root, matcher=matcher)


def runner(hooks: list[Hook], root: Path, notices: list[str] | None = None) -> HookRunner:
    return HookRunner(
        hooks=hooks,
        session_id="session-1",
        root=root,
        transcript_path=root / "session.jsonl",
        notice=(notices if notices is not None else []).append,
    )


def test_parse_hook_entries():
    hooks = parse_hook_entries(
        PRE_TOOL_USE,
        [
            {"command": ["check", "--strict"], "matcher": "shell|apply_patch"},
            {"command": ["log"], "cwd": "tools"},
        ],
    )
    assert hooks == (
        HookDefinition(PRE_TOOL_USE, ("check", "--strict"), None, "shell|apply_patch"),
        HookDefinition(PRE_TOOL_USE, ("log",), "tools", "*"),
    )
    assert parse_hook_entries(STOP, []) == ()


@pytest.mark.parametrize(
    "value, message",
    [
        ({"command": ["x"]}, "expected a list"),
        (["x"], "expected a table"),
        ([{"command": []}], "non-empty list"),
        ([{"command": "check"}], "non-empty list"),
        ([{"command": ["x"], "timeout": 5}], "unknown field"),
        ([{"command": ["x"], "cwd": ""}], "'cwd'"),
        ([{"command": ["x"], "matcher": ""}], "'matcher'"),
    ],
)
def test_invalid_hook_entries(value, message):
    with pytest.raises(HookConfigError, match=message):
        parse_hook_entries(PRE_TOOL_USE, value)


def test_equivalent_definitions_compare_equal():
    plain, dot, slash = (
        parse_hook_entries(STOP, [entry])[0]
        for entry in ({"command": ["x"]}, {"command": ["x"], "cwd": "."}, {"command": ["x"], "cwd": "./"})
    )
    assert plain == dot == slash
    tools = parse_hook_entries(STOP, [{"command": ["x"], "cwd": "./tools/"}, {"command": ["x"], "cwd": "tools"}])
    assert tools[0] == tools[1] and tools[0].cwd == "tools"
    assert HookDefinition(STOP, ("x",), "tools").resolve(Path("/root")).cwd == Path("/root/tools").resolve()
    assert HookDefinition(STOP, ("x",)).resolve(Path("/root")).cwd == Path("/root").resolve()


def test_matcher():
    hook = Hook(PRE_TOOL_USE, ("x",), Path("/"), matcher="shell|apply_patch")
    assert hook.applies_to("shell") and hook.applies_to("apply_patch")
    assert not hook.applies_to("update_plan")
    assert Hook(PRE_TOOL_USE, ("x",), Path("/")).applies_to("anything")
    assert "matcher: worker" in Hook(SUBAGENT_STOP, ("x",), Path("/"), matcher="worker").describe()
    assert "matcher" not in Hook(STOP, ("x",), Path("/")).describe()


def echo_hook(event: str, stdout: str, root: Path, matcher: str = "*", exit_code: int = 0) -> Hook:
    code = f"import sys; sys.stdin.read(); sys.stdout.write({stdout!r}); sys.exit({exit_code})"
    return python_hook(event, code, root, matcher)


def recording_hook(event: str, log: Path, root: Path, matcher: str = "*") -> Hook:
    code = (
        "import json, sys\n"
        f"with open({str(log)!r}, 'a') as handle:\n"
        "    handle.write(json.dumps(json.load(sys.stdin)) + '\\n')\n"
    )
    return python_hook(event, code, root, matcher)


def read_log(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_first_blocking_hook_stops_the_others(tmp_path):
    marker = tmp_path / "second-ran"
    hooks = [
        python_hook(PRE_TOOL_USE, "import sys; sys.exit(2)", tmp_path),
        python_hook(PRE_TOOL_USE, f"open({str(marker)!r}, 'w').close()", tmp_path),
    ]
    assert runner(hooks, tmp_path).pre_tool_use("shell", "{}").blocked
    assert not marker.exists()


def test_hook_environment(tmp_path):
    out = tmp_path / "env.json"
    code = (
        "import json, os\n"
        f"json.dump({{k: os.environ[k] for k in ('MELTZER_PROJECT_DIR', 'MELTZER_SESSION_ID')}}, open({str(out)!r}, 'w'))\n"
    )
    runner([python_hook(STOP, code, tmp_path)], tmp_path).stop("done")
    assert json.loads(out.read_text()) == {"MELTZER_PROJECT_DIR": str(tmp_path), "MELTZER_SESSION_ID": "session-1"}


# --- trust ---------------------------------------------------------------------------


def test_trust_is_per_definition(tmp_path):
    path = tmp_path / ".meltzer" / "trusted_hooks.toml"
    hook = Hook(PRE_TOOL_USE, ("check",), tmp_path, matcher="shell")
    store = TrustStore(path)
    assert not store.is_trusted(hook)
    store.trust(hook)
    assert store.is_trusted(hook) and TrustStore(path).is_trusted(hook)
    store = TrustStore(path)
    assert not store.is_trusted(Hook(PRE_TOOL_USE, ("check", "--fix"), tmp_path, matcher="shell"))
    assert not store.is_trusted(Hook(PRE_TOOL_USE, ("check",), tmp_path, matcher="*"))
    assert not store.is_trusted(Hook(POST_TOOL_USE, ("check",), tmp_path, matcher="shell"))
    assert not store.is_trusted(Hook(PRE_TOOL_USE, ("check",), tmp_path / "sub", matcher="shell"))


def test_unreadable_trust_file_trusts_nothing(tmp_path):
    path = tmp_path / "trusted_hooks.toml"
    path.write_text("not = [valid\n")
    store = TrustStore(path)
    assert not store.is_trusted(Hook(STOP, ("x",), tmp_path))
    assert len(store.warnings) == 1


def test_for_session_keeps_trusted_hooks_and_skips_the_others(tmp_path):
    store = TrustStore(tmp_path / "trusted_hooks.toml")
    trusted = HookDefinition(STOP, ("trusted",))
    store.trust(trusted.resolve(tmp_path))
    notices: list[str] = []
    hooks = HookRunner.for_session(
        (trusted, HookDefinition(STOP, ("unknown",))),
        root=tmp_path,
        trust=store,
        confirm=None,
        notice=notices.append,
        session_id="s",
        transcript_path=tmp_path / "s.jsonl",
    )
    assert [hook.command for hook in hooks.hooks] == [("trusted",)]
    assert len(notices) == 1 and "skipping untrusted Stop hook: unknown" in notices[0]


def test_for_session_records_confirmed_hooks(tmp_path):
    store = TrustStore(tmp_path / "trusted_hooks.toml")
    definition = HookDefinition(STOP, ("new",))
    records: list[str] = []
    hooks = HookRunner.for_session(
        (definition,),
        root=tmp_path,
        trust=store,
        confirm=lambda hook: True,
        notice=lambda message: None,
        session_id="s",
        transcript_path=tmp_path / "s.jsonl",
        record=lambda kind, payload: records.append(kind),
    )
    assert [hook.command for hook in hooks.hooks] == [("new",)]
    assert TrustStore(store.path).is_trusted(definition.resolve(tmp_path))
    hooks.record("x", {})
    assert records == ["x"]


# --- output of the hooks of each event -----------------------------------------------


def outcome(event: str, stdout: str = "", returncode: int = 0, stderr: str = "") -> HookOutcome:
    return parse_output(event, returncode, stdout, stderr)


def specific(event: str, **fields) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": event, **fields}})


def test_empty_output_has_no_effect():
    for event in HOOK_EVENTS:
        assert outcome(event) == HookOutcome()


@pytest.mark.parametrize("event", HOOK_EVENTS)
def test_other_exit_statuses_have_no_effect(event):
    result = outcome(event, stdout=specific(event, additionalContext="x"), returncode=1, stderr="oops\nmore")
    assert not result.blocked and not result.stop and result.additional_context == ()
    assert result.error == "exited with status 1: oops"


@pytest.mark.parametrize("event", [SESSION_START, SESSION_END, PRE_COMPACT, POST_COMPACT, SUBAGENT_START])
def test_exit_status_2_blocks_nothing_for_events_that_cannot_block(event):
    result = outcome(event, returncode=2, stderr="no")
    assert not result.blocked and result.error == "exited with status 2: no"


@pytest.mark.parametrize("event", [PERMISSION_REQUEST, POST_TOOL_USE, USER_PROMPT_SUBMIT, STOP, SUBAGENT_STOP])
def test_exit_status_2_blocks_with_stderr_as_the_reason(event):
    result = outcome(event, returncode=2, stderr="  not now \n")
    assert result.blocked and result.reason == "not now"
    assert result.decision == ("deny" if event == PERMISSION_REQUEST else None)
    assert outcome(event, returncode=2).error == "exited with status 2 without a reason on stderr"
    assert outcome(PRE_TOOL_USE, returncode=2) == HookOutcome(blocked=True)


@pytest.mark.parametrize(
    "event", [SESSION_START, SUBAGENT_START, USER_PROMPT_SUBMIT, PRE_TOOL_USE, PERMISSION_REQUEST, POST_TOOL_USE, PRE_COMPACT]
)
def test_plain_text_becomes_context_only_for_some_events(event):
    text = outcome(event, stdout="  branch: main\n").additional_context
    json_context = outcome(event, stdout=specific(event, additionalContext="ctx")).additional_context
    adds = event in (SESSION_START, SUBAGENT_START, USER_PROMPT_SUBMIT)
    assert (text, json_context) == ((("branch: main",), ("ctx",)) if adds else ((), json_context))


@pytest.mark.parametrize("event", [SESSION_START, PRE_TOOL_USE, STOP])
def test_invalid_json_is_ignored(event):
    result = outcome(event, stdout='{"continue": ')
    assert result.error == "output ignored: invalid JSON" and result.additional_context == ()
    assert outcome(STOP, stdout="done").error == "output ignored: invalid JSON"


@pytest.mark.parametrize(
    "stdout, message",
    [
        ('{"continue": "no"}', "'continue' must be true or false"),
        ('{"systemMessage": 3}', "'systemMessage' must be a string"),
        ('{"hookSpecificOutput": []}', "'hookSpecificOutput' must be an object"),
        (specific(STOP), "hookSpecificOutput is for Stop, not SessionStart"),
        (specific(SESSION_START, additionalContext=["a"]), "'additionalContext' must be a string"),
    ],
)
def test_fields_of_the_wrong_type_are_ignored(stdout, message):
    result = outcome(SESSION_START, stdout=stdout)
    assert result.error == f"output ignored: {message}" and result.additional_context == ()


def test_system_message_is_reported():
    result = outcome(SESSION_START, stdout=json.dumps({"systemMessage": "lint config changed"}))
    assert result.system_message == "lint config changed" and result.error == ""
    result = outcome(PRE_TOOL_USE, stdout=json.dumps({"systemMessage": "note", "continue": False}))
    assert result.system_message == "note" and result.error


def blocked(reason: str, **fields) -> HookOutcome:
    return HookOutcome(blocked=True, reason=reason, **fields)


def decision(behavior: str, **fields) -> str:
    return specific(PERMISSION_REQUEST, decision={"behavior": behavior, **fields})


@pytest.mark.parametrize(
    "event, stdout, expected",
    [
        (PRE_TOOL_USE, specific(PRE_TOOL_USE, permissionDecision="deny", permissionDecisionReason=" no rm "), blocked("no rm")),
        (PRE_TOOL_USE, json.dumps({"decision": "block", "reason": "no rm"}), blocked("no rm")),
        (
            PRE_TOOL_USE,
            specific(PRE_TOOL_USE, permissionDecision="allow", updatedInput={"command": "ls -la"}),
            HookOutcome(updated_input={"command": "ls -la"}),
        ),
        (
            PRE_TOOL_USE,
            specific(PRE_TOOL_USE, permissionDecision="deny", permissionDecisionReason="no", additionalContext="c"),
            blocked("no", additional_context=("c",)),
        ),
        (PERMISSION_REQUEST, decision("allow"), HookOutcome(decision="allow")),
        (PERMISSION_REQUEST, decision("deny", message=" not on Fridays "), blocked("not on Fridays", decision="deny")),
        (PERMISSION_REQUEST, decision("deny"), blocked("", decision="deny")),
        (POST_TOOL_USE, json.dumps({"decision": "block", "reason": "tests fail"}), blocked("tests fail")),
        (
            POST_TOOL_USE,
            json.dumps({"continue": False, "reason": "use the formatter output"}),
            HookOutcome(stop=True, reason="use the formatter output"),
        ),
        (POST_TOOL_USE, json.dumps({"continue": False, "stopReason": "lint failed"}), HookOutcome(stop=True, reason="lint failed")),
        (POST_TOOL_USE, specific(POST_TOOL_USE, additionalContext="3 warnings"), HookOutcome(additional_context=("3 warnings",))),
        (USER_PROMPT_SUBMIT, json.dumps({"decision": "block", "reason": "no secrets"}), blocked("no secrets")),
        (USER_PROMPT_SUBMIT, json.dumps({"continue": False, "stopReason": "paused"}), HookOutcome(stop=True, reason="paused")),
        (STOP, json.dumps({"decision": "block", "reason": "run the tests"}), blocked("run the tests")),
        (SUBAGENT_STOP, json.dumps({"continue": False, "decision": "block", "reason": "x"}), HookOutcome(stop=True)),
        (STOP, "{}", HookOutcome()),
        (PRE_COMPACT, json.dumps({"continue": False, "stopReason": "save first"}), HookOutcome(stop=True, reason="save first")),
        (POST_COMPACT, json.dumps({"continue": True}), HookOutcome()),
    ],
)
def test_hook_output(event, stdout, expected):
    assert outcome(event, stdout=stdout) == expected


@pytest.mark.parametrize(
    "event, stdout, message",
    [
        (PRE_TOOL_USE, specific(PRE_TOOL_USE, permissionDecision="allow"), "permissionDecision:allow without an updatedInput object"),
        (PRE_TOOL_USE, specific(PRE_TOOL_USE, permissionDecision="ask"), "unsupported permissionDecision:ask"),
        (PRE_TOOL_USE, specific(PRE_TOOL_USE, permissionDecision="deny"), "permissionDecision:deny without a non-empty"),
        (PRE_TOOL_USE, specific(PRE_TOOL_USE, updatedInput={"command": "ls"}), "updatedInput without permissionDecision:allow"),
        (PRE_TOOL_USE, specific(PRE_TOOL_USE, permissionDecisionReason="x"), "permissionDecisionReason without permissionDecision"),
        (PRE_TOOL_USE, json.dumps({"decision": "approve"}), "unsupported decision:approve"),
        (PRE_TOOL_USE, json.dumps({"stopReason": "x"}), "unsupported stopReason"),
        (PERMISSION_REQUEST, decision("allow", updatedInput={}), "unsupported updatedInput"),
        (PERMISSION_REQUEST, decision("allow", updatedPermissions=[]), "unsupported updatedPermissions"),
        (PERMISSION_REQUEST, decision("deny", interrupt=True), "unsupported interrupt:true"),
        (PERMISSION_REQUEST, decision("ask"), "unsupported decision.behavior: 'ask'"),
        (POST_TOOL_USE, specific(POST_TOOL_USE, updatedMCPToolOutput={}), "unsupported updatedMCPToolOutput"),
        *[(event, json.dumps({"continue": False}), "unsupported continue:false") for event in (PRE_TOOL_USE, PERMISSION_REQUEST)],
        *[(event, json.dumps({"decision": "block"}), "decision:block without a non-empty reason") for event in (PRE_TOOL_USE, POST_TOOL_USE)],
        *[(event, json.dumps({"reason": "x"}), "reason without decision") for event in (PRE_TOOL_USE, POST_TOOL_USE)],
        *[(event, json.dumps({"suppressOutput": True}), "unsupported suppressOutput") for event in (PRE_TOOL_USE, POST_TOOL_USE)],
    ],
)
def test_unsupported_output_has_no_effect(event, stdout, message):
    result = outcome(event, stdout=stdout)
    assert not result.blocked and not result.stop and result.decision is None and result.updated_input is None
    assert result.error.startswith(f"output ignored: {message}")


@pytest.mark.parametrize("event", [USER_PROMPT_SUBMIT, STOP, SUBAGENT_STOP])
def test_block_without_a_reason_is_ignored(event):
    for stdout in ({"decision": "block"}, {"decision": "block", "reason": " "}):
        assert outcome(event, stdout=json.dumps(stdout)).error.endswith("decision:block without a non-empty reason")


def test_preview_keeps_the_start_and_the_end():
    text = "a" * 60 + "b" * 60
    short = preview(text, 40)
    assert short.startswith("a" * 20) and short.endswith("b" * 20)
    assert "[... 80 characters not shown; the full hook output is in the session log]" in short
    assert preview("short", 40) == "short"


# --- running the hooks of each event --------------------------------------------------


def test_payloads(tmp_path):
    log = tmp_path / "events.jsonl"
    events = (SESSION_START, SESSION_END, USER_PROMPT_SUBMIT, PRE_TOOL_USE, PERMISSION_REQUEST, POST_TOOL_USE)
    events += (PRE_COMPACT, POST_COMPACT, STOP)
    hooks = runner([recording_hook(event, log, tmp_path) for event in events], tmp_path)
    hooks.session_start()
    hooks.session_start("resume")
    hooks.session_end("exit")
    hooks.user_prompt_submit("fix the tests")
    hooks.pre_tool_use("shell", '{"command": "ls"}')
    hooks.permission_request(
        ApprovalRequest(tool="shell", cwd=tmp_path, command="make build", justification="build", escalate=True)
    )
    hooks.post_tool_use("shell", '{"command": "ls"}', "a.txt", True)
    hooks.pre_compact("manual")
    hooks.post_compact("manual")
    hooks.stop("done", stop_hook_active=True)
    payloads = read_log(log)
    start, resumed, end, prompt, pre_tool, permission, post_tool, pre, post, stop = payloads
    assert [p["hook_event_name"] for p in payloads] == [SESSION_START, *events]
    assert all(p["session_id"] == "session-1" and p["cwd"] == str(tmp_path) for p in payloads)
    assert all("agent_id" not in p for p in payloads)
    assert (start["source"], resumed["source"], end["reason"]) == ("startup", "resume", "exit")
    assert prompt["prompt"] == "fix the tests"
    assert pre_tool["tool_input"] == {"command": "ls"}
    assert permission["tool_name"] == "shell"
    assert permission["tool_input"] == {
        "cwd": str(tmp_path),
        "command": "make build",
        "justification": "build",
        "escalate": True,
    }
    assert post_tool["tool_response"] == {"output": "a.txt", "success": True}
    assert pre["trigger"] == post["trigger"] == "manual"
    assert stop["stop_hook_active"] is True and stop["last_assistant_message"] == "done"


def test_permission_request_input_for_other_request_types(tmp_path):
    log = tmp_path / "events.jsonl"
    hooks = runner([recording_hook(PERMISSION_REQUEST, log, tmp_path)], tmp_path)
    patch = ApprovalRequest(tool="apply_patch", cwd=tmp_path, paths=(tmp_path / "a.txt",), patch="*** Begin Patch")
    mcp = ApprovalRequest(tool="mcp_tool_call", cwd=tmp_path, server="db", mcp_tool="query", arguments={"sql": "select 1"})
    hooks.permission_request(patch)
    hooks.permission_request(mcp)
    patch_input, mcp_input = (payload["tool_input"] for payload in read_log(log))
    assert patch_input == {"cwd": str(tmp_path), "paths": [str(tmp_path / "a.txt")], "patch": "*** Begin Patch"}
    assert mcp_input == {"cwd": str(tmp_path), "server": "db", "mcp_tool": "query", "arguments": {"sql": "select 1"}}


def test_subagent_payloads(tmp_path):
    log = tmp_path / "events.jsonl"
    parent = runner(
        [recording_hook(event, log, tmp_path) for event in (SUBAGENT_START, SUBAGENT_STOP, PRE_TOOL_USE)], tmp_path
    )
    child = parent.for_subagent("child-1", tmp_path / "child.jsonl", agent_type="explorer")
    child.subagent_start()
    child.subagent_stop("found it", stop_hook_active=False)
    child.pre_tool_use("shell", '{"command": "ls"}')
    payloads = read_log(log)
    start, stop, tool = payloads
    assert [p["hook_event_name"] for p in payloads] == [SUBAGENT_START, SUBAGENT_STOP, PRE_TOOL_USE]
    assert all(p["agent_id"] == "child-1" and p["agent_type"] == "explorer" for p in payloads)
    assert all(p["session_id"] == "child-1" for p in payloads)
    assert stop["last_assistant_message"] == "found it" and stop["stop_hook_active"] is False
    assert stop["agent_transcript_path"] == str(tmp_path / "child.jsonl")


def test_subagent_hooks_match_the_agent_type(tmp_path):
    hooks = [
        echo_hook(SUBAGENT_START, "explorer context", tmp_path, matcher="explorer"),
        echo_hook(SUBAGENT_START, "worker context", tmp_path, matcher="worker"),
    ]
    child = runner(hooks, tmp_path).for_subagent("child-1", tmp_path / "c.jsonl", agent_type="explorer")
    assert child.subagent_start().additional_context == ("explorer context",)


def test_permission_request_hooks_match_the_request_type(tmp_path):
    deny = "import sys; sys.stderr.write('no'); sys.exit(2)"
    hooks = runner([python_hook(PERMISSION_REQUEST, deny, tmp_path, matcher="apply_patch")], tmp_path)
    assert hooks.permission_request(ApprovalRequest(tool="apply_patch", cwd=tmp_path)).decision == "deny"
    assert hooks.permission_request(ApprovalRequest(tool="shell", cwd=tmp_path, command="ls")).decision is None


def test_any_deny_wins_over_allow(tmp_path):
    allow = specific(PERMISSION_REQUEST, decision={"behavior": "allow"})
    deny = specific(PERMISSION_REQUEST, decision={"behavior": "deny", "message": "outside the project"})
    request = ApprovalRequest(tool="shell", cwd=tmp_path, command="ls")
    hooks = [echo_hook(PERMISSION_REQUEST, allow, tmp_path), echo_hook(PERMISSION_REQUEST, deny, tmp_path)]
    result = runner(hooks, tmp_path).permission_request(request)
    assert result.decision == "deny" and result.blocked and result.reason == "outside the project"
    only_allow = runner([echo_hook(PERMISSION_REQUEST, allow, tmp_path)], tmp_path).permission_request(request)
    assert only_allow.decision == "allow" and not only_allow.blocked


def test_reason_defaults_to_the_hook_command(tmp_path):
    hook = echo_hook(PERMISSION_REQUEST, specific(PERMISSION_REQUEST, decision={"behavior": "deny"}), tmp_path)
    result = runner([hook], tmp_path).permission_request(ApprovalRequest(tool="shell", cwd=tmp_path, command="ls"))
    assert result.reason == f"denied by {shlex.join(hook.command)}"
    hook = echo_hook(USER_PROMPT_SUBMIT, json.dumps({"continue": False}), tmp_path)
    result = runner([hook], tmp_path).user_prompt_submit("hi")
    assert result.stop and result.reason == f"stopped by {shlex.join(hook.command)}"


def test_contexts_of_all_hooks_are_kept_in_order(tmp_path):
    hooks = runner(
        [
            echo_hook(SESSION_START, "first", tmp_path),
            echo_hook(SESSION_START, "", tmp_path, exit_code=1),
            echo_hook(SESSION_START, specific(SESSION_START, additionalContext="second"), tmp_path),
        ],
        tmp_path,
    )
    assert hooks.session_start().additional_context == ("first", "second")


def test_stop_reasons_of_all_blocking_hooks_are_joined(tmp_path):
    def block(reason: str) -> Hook:
        return echo_hook(STOP, json.dumps({"decision": "block", "reason": reason}), tmp_path)

    result = runner([block("run the tests"), block("update the docs")], tmp_path).stop("done")
    assert result.blocked and result.reason == "run the tests\n\nupdate the docs"


def test_system_message_and_errors_are_notices(tmp_path):
    notices: list[str] = []
    hooks = runner(
        [
            echo_hook(SESSION_START, json.dumps({"systemMessage": "using staging"}), tmp_path),
            echo_hook(SESSION_START, "{not json", tmp_path),
            echo_hook(SESSION_START, "", tmp_path, exit_code=1),
        ],
        tmp_path,
        notices,
    )
    assert hooks.session_start() == HookOutcome()
    assert len(notices) == 3
    assert notices[0].startswith("SessionStart hook ") and notices[0].endswith(": using staging")
    assert notices[1].endswith("output ignored: invalid JSON")
    assert notices[2].endswith("exited with status 1")


def test_long_output_is_shortened_and_recorded(tmp_path):
    records: list[tuple[str, dict]] = []
    text = "line\n" * 5000
    hooks = runner([echo_hook(SESSION_START, text, tmp_path)], tmp_path)
    hooks.record = lambda kind, payload: records.append((kind, payload))
    (context,) = hooks.session_start().additional_context
    assert len(context) < HOOK_OUTPUT_LIMIT + 200
    assert "the full hook output is in the session log" in context
    command = list(hooks.hooks[0].command)
    assert records == [("hook_output", {"event": SESSION_START, "command": command, "text": text.strip()})]


@pytest.mark.parametrize(
    "event, call",
    [
        (PRE_TOOL_USE, lambda hooks: hooks.pre_tool_use("shell", "{}")),
        (PERMISSION_REQUEST, lambda hooks: hooks.permission_request(ApprovalRequest(tool="shell", cwd=hooks.root))),
        (POST_TOOL_USE, lambda hooks: hooks.post_tool_use("shell", "{}", "ok", True)),
        (USER_PROMPT_SUBMIT, lambda hooks: hooks.user_prompt_submit("hi")),
        (STOP, lambda hooks: hooks.stop("done")),
    ],
)
def test_long_block_reasons_are_shortened(tmp_path, event, call):
    code = f"import sys; sys.stderr.write('x' * {HOOK_OUTPUT_LIMIT * 2}); sys.exit(2)"
    result = call(runner([python_hook(event, code, tmp_path)], tmp_path))
    assert result.blocked and len(result.reason) < HOOK_OUTPUT_LIMIT + 200


@pytest.mark.skipif(sys.platform == "win32", reason="needs process groups")
def test_a_hook_that_times_out_is_stopped_with_its_child_processes(tmp_path):
    beat = tmp_path / "beat"
    child = f"import time\nwhile True:\n    open({str(beat)!r}, 'a').write('x')\n    time.sleep(0.05)"
    code = f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', {child!r}])\ntime.sleep(60)"
    notices: list[str] = []
    hooks = runner([python_hook(STOP, code, tmp_path)], tmp_path, notices)
    hooks.timeout = 1.0
    assert hooks.stop("done") == HookOutcome()
    assert len(notices) == 1 and "timed out after 1 seconds" in notices[0]
    size = beat.stat().st_size
    time.sleep(0.5)
    assert beat.stat().st_size == size


def test_subagent_records_in_its_own_log(tmp_path):
    parent_records: list[str] = []
    child_records: list[str] = []
    hooks = runner([echo_hook(SUBAGENT_START, "y" * (HOOK_OUTPUT_LIMIT + 1), tmp_path)], tmp_path)
    hooks.record = lambda kind, payload: parent_records.append(kind)
    child = hooks.for_subagent(
        "c", tmp_path / "c.jsonl", agent_type="worker", record=lambda kind, payload: child_records.append(kind)
    )
    child.subagent_start()
    assert child_records == ["hook_output"] and parent_records == []
