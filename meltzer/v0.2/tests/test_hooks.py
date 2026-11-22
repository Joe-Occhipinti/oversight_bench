from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from meltzer.hooks import (
    POST_TOOL_USE,
    PRE_TOOL_USE,
    SESSION_START,
    STOP,
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


def test_definition_resolves_cwd_against_the_root(tmp_path):
    hook = HookDefinition(STOP, ("x",), "tools").resolve(tmp_path)
    assert hook.cwd == (tmp_path / "tools").resolve()
    assert HookDefinition(STOP, ("x",)).resolve(tmp_path).cwd == tmp_path.resolve()


def test_matcher():
    hook = Hook(PRE_TOOL_USE, ("x",), Path("/"), matcher="shell|apply_patch")
    assert hook.applies_to("shell") and hook.applies_to("apply_patch")
    assert not hook.applies_to("update_plan")
    assert Hook(PRE_TOOL_USE, ("x",), Path("/")).applies_to("anything")


@pytest.mark.parametrize(
    "code, blocked, reason, notice",
    [
        ("pass", False, None, None),
        ("import sys; sys.stderr.write('oops'); sys.exit(1)", False, None, "exited with status 1: oops"),
        ("import sys; sys.stderr.write('no network commands'); sys.exit(2)", True, "no network commands", None),
        ("import sys; sys.exit(2)", True, "blocked by ", None),
    ],
)
def test_pre_tool_use_exit_status(tmp_path, code, blocked, reason, notice):
    notices: list[str] = []
    result = runner([python_hook(PRE_TOOL_USE, code, tmp_path)], tmp_path, notices).pre_tool_use("shell", "{}")
    assert result.blocked is blocked
    if reason is not None:
        assert result.reason.startswith(reason)
    if notice is not None:
        assert len(notices) == 1 and notice in notices[0]


def test_first_blocking_hook_stops_the_others(tmp_path):
    marker = tmp_path / "second-ran"
    hooks = [
        python_hook(PRE_TOOL_USE, "import sys; sys.exit(2)", tmp_path),
        python_hook(PRE_TOOL_USE, f"open({str(marker)!r}, 'w').close()", tmp_path),
    ]
    assert runner(hooks, tmp_path).pre_tool_use("shell", "{}").blocked
    assert not marker.exists()


def test_hooks_only_run_for_matching_tools(tmp_path):
    hook = python_hook(PRE_TOOL_USE, "import sys; sys.exit(2)", tmp_path, matcher="shell")
    hooks = runner([hook], tmp_path)
    assert hooks.pre_tool_use("shell", "{}").blocked
    assert not hooks.pre_tool_use("apply_patch", "{}").blocked


def test_payloads(tmp_path):
    log = tmp_path / "events.jsonl"
    code = (
        "import json, sys\n"
        f"with open({str(log)!r}, 'a') as handle:\n"
        "    handle.write(json.dumps(json.load(sys.stdin)) + '\\n')\n"
    )
    hooks = runner([python_hook(event, code, tmp_path) for event in (SESSION_START, PRE_TOOL_USE, POST_TOOL_USE, STOP)], tmp_path)
    hooks.session_start()
    hooks.pre_tool_use("shell", '{"command": "ls"}')
    hooks.post_tool_use("shell", '{"command": "ls"}', "a.txt", True)
    hooks.stop("finished")
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert [e["hook_event_name"] for e in events] == [SESSION_START, PRE_TOOL_USE, POST_TOOL_USE, STOP]
    assert all(e["session_id"] == "session-1" and e["cwd"] == str(tmp_path) for e in events)
    assert events[0]["source"] == "startup"
    assert events[1]["tool_input"] == {"command": "ls"}
    assert events[2]["tool_response"] == {"output": "a.txt", "success": True}
    assert events[3]["last_assistant_message"] == "finished"


def test_hook_environment(tmp_path):
    out = tmp_path / "env.json"
    code = (
        "import json, os\n"
        f"json.dump({{k: os.environ[k] for k in ('MELTZER_PROJECT_DIR', 'MELTZER_SESSION_ID')}}, open({str(out)!r}, 'w'))\n"
    )
    runner([python_hook(STOP, code, tmp_path)], tmp_path).stop("done")
    assert json.loads(out.read_text()) == {"MELTZER_PROJECT_DIR": str(tmp_path), "MELTZER_SESSION_ID": "session-1"}


# --- trust ---------------------------------------------------------------------------


def test_trust_store_round_trip(tmp_path):
    path = tmp_path / ".meltzer" / "trusted_hooks.toml"
    hook = Hook(STOP, ("notify-send", "done"), tmp_path)
    store = TrustStore(path)
    assert not store.is_trusted(hook)
    store.trust(hook)
    assert store.is_trusted(hook)
    assert TrustStore(path).is_trusted(hook)


def test_trust_is_per_definition(tmp_path):
    path = tmp_path / "trusted_hooks.toml"
    hook = Hook(PRE_TOOL_USE, ("check",), tmp_path, matcher="shell")
    TrustStore(path).trust(hook)
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
    hooks = HookRunner.for_session(
        (definition,),
        root=tmp_path,
        trust=store,
        confirm=lambda hook: True,
        notice=lambda message: None,
        session_id="s",
        transcript_path=tmp_path / "s.jsonl",
    )
    assert [hook.command for hook in hooks.hooks] == [("new",)]
    assert TrustStore(store.path).is_trusted(definition.resolve(tmp_path))
