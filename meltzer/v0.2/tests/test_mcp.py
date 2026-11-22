from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from meltzer.approval.orchestrator import ApprovalOrchestrator
from meltzer.approval.policy import ApprovalPolicy
from meltzer.config.loader import ConfigError
from meltzer.mcp import manager as manager_module
from meltzer.mcp.client import McpError
from meltzer.mcp.config import (
    DEFAULT_STARTUP_TIMEOUT_SECONDS,
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    McpServerConfig,
    ToolApprovalMode,
    parse_mcp_servers,
)
from meltzer.mcp.manager import MAX_TOOL_NAME_CHARS, McpManager, model_tool_names
from meltzer.sandbox.policy import SandboxMode
from meltzer.tools.mcp_tool import McpTool, format_result, mcp_tools, requires_approval
from meltzer.tools.registry import ToolError, ToolRegistry

from conftest import RecordingReviewer, make_orchestrator, process_stops

SERVER = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


def server(name: str = "notes", *args: str, **fields) -> McpServerConfig:
    return McpServerConfig(name=name, command=sys.executable, args=(str(SERVER), *args), **fields)


STARTED: list[McpManager] = []


def start(work: Path, *servers: McpServerConfig, notices: list[str] | None = None) -> McpManager:
    manager = McpManager.start(
        servers or (server(),),
        root=work,
        log_dir=work.parent / "logs",
        notice=(notices if notices is not None else []).append,
    )
    STARTED.append(manager)
    return manager


@pytest.fixture(autouse=True)
def shutdown_managers():
    yield
    while STARTED:
        STARTED.pop().shutdown()


@pytest.fixture
def manager(work):
    return start(work)


def tool(manager: McpManager, name: str, orchestrator: ApprovalOrchestrator, work: Path) -> McpTool:
    return next(t for t in mcp_tools(manager, orchestrator, work) if t.info.name == name)


# Configuration


def test_parse_server_settings():
    (config,) = parse_mcp_servers(
        {
            "db": {
                "command": "python3",
                "args": ["tools/db_mcp.py", "--readonly"],
                "env": {"DB_URL": "sqlite:///x.db"},
                "env_vars": ["GITHUB_TOKEN", {"name": "DB_PASSWORD", "source": "local"}],
                "cwd": "tools",
                "required": True,
                "startup_timeout_sec": 5,
                "tool_timeout_sec": 12.5,
                "enabled_tools": ["query", "drop"],
                "disabled_tools": ["drop"],
                "default_tools_approval_mode": "writes",
                "tools": {"query": {"approval_mode": "approve"}},
            }
        }
    )
    assert config == McpServerConfig(
        name="db",
        command="python3",
        args=("tools/db_mcp.py", "--readonly"),
        env={"DB_URL": "sqlite:///x.db"},
        env_vars=("GITHUB_TOKEN", "DB_PASSWORD"),
        cwd="tools",
        required=True,
        startup_timeout_sec=5.0,
        tool_timeout_sec=12.5,
        enabled_tools=("query", "drop"),
        disabled_tools=("drop",),
        default_tools_approval_mode=ToolApprovalMode.WRITES,
        tool_approval_modes={"query": ToolApprovalMode.APPROVE},
    )
    assert config.allows_tool("query") and not config.allows_tool("drop") and not config.allows_tool("other")
    assert config.approval_mode("query") == ToolApprovalMode.APPROVE
    assert config.approval_mode("insert") == ToolApprovalMode.WRITES


def test_server_defaults():
    (config,) = parse_mcp_servers({"docs": {"command": "docs-server"}})
    assert config.args == () and config.env == {} and config.env_vars == () and config.cwd is None
    assert config.enabled and not config.required
    assert config.startup_timeout_sec == DEFAULT_STARTUP_TIMEOUT_SECONDS
    assert config.tool_timeout_sec == DEFAULT_TOOL_TIMEOUT_SECONDS
    assert config.allows_tool("anything")
    assert config.approval_mode("anything") == ToolApprovalMode.AUTO
    assert parse_mcp_servers({}) == ()


def test_an_entry_with_only_approval_modes_starts_no_server():
    approvals = {"default_tools_approval_mode": "prompt", "tools": {"query": {"approval_mode": "approve"}}}
    assert parse_mcp_servers({"db": approvals, "docs": {"command": "docs-server"}})[0].name == "docs"
    (merged,) = parse_mcp_servers({"db": {"command": "db-server", **approvals}})
    assert merged.default_tools_approval_mode == ToolApprovalMode.PROMPT


@pytest.mark.parametrize(
    "entry, message",
    [
        ({"url": "https://mcp.example/mcp"}, "HTTP servers are not supported"),
        ({"command": "x", "bearer_token_env_var": "TOKEN"}, "HTTP servers are not supported"),
        ({}, "'command' is required"),
        ({"command": ""}, "'command'"),
        ({"command": "x", "args": "a b"}, "'args'"),
        ({"command": "x", "env": {"A": 1}}, "'env'"),
        ({"command": "x", "env_vars": [{"name": "A", "source": "remote"}]}, "source"),
        ({"command": "x", "env_vars": [{"source": "local"}]}, "'env_vars'"),
        ({"command": "x", "enabled": "yes"}, "'enabled'"),
        ({"command": "x", "startup_timeout_sec": 0}, "'startup_timeout_sec'"),
        ({"command": "x", "tool_timeout_sec": True}, "'tool_timeout_sec'"),
        ({"command": "x", "enabled_tools": "query"}, "'enabled_tools'"),
        ({"command": "x", "default_tools_approval_mode": "always"}, "auto, prompt, writes, approve"),
        ({"command": "x", "tools": {"query": {"approval_mode": "never"}}}, "auto, prompt, writes, approve"),
        ({"command": "x", "tools": {"query": {"timeout": 3}}}, "unknown field"),
        ({"command": "x", "timeout": 3}, "unknown field 'timeout'"),
        ("x", "expected a table"),
    ],
)
def test_invalid_server_settings(entry, message):
    with pytest.raises(ConfigError, match=message) as error:
        parse_mcp_servers({"db": entry})
    assert "mcp_servers.db" in str(error.value)


def test_invalid_server_name():
    with pytest.raises(ConfigError, match="server name"):
        parse_mcp_servers({"my server": {"command": "x"}})


# Starting servers, listing and calling tools


def test_start_lists_tools_across_pages(manager):
    names = [info.name for info in manager.tools]
    assert names[:4] == ["echo", "delete_note", "status", "fetch"]
    assert len(names) == 11
    echo = manager.tools[0]
    assert echo.server == "notes" and echo.model_name == "mcp__notes__echo"
    assert echo.description == "Return the text."
    assert echo.annotations == {"readOnlyHint": True}
    assert echo.input_schema == {"type": "object", "properties": {"text": {"type": "string"}}}
    fetch = manager.tools[3]
    assert fetch.annotations is None
    assert fetch.input_schema == {"type": "object", "properties": {}}


def test_call_tool_ping_and_server_log(manager, work):
    result = manager.call_tool("notes", "echo", {"text": "hello"})
    assert result == {"content": [{"type": "text", "text": "hello"}]}
    with pytest.raises(McpError, match="unknown MCP server"):
        manager.call_tool("other", "echo", {})
    manager.clients["notes"].ping(timeout=5)
    assert "fake server started" in (work.parent / "logs" / "mcp-notes.log").read_text()


def test_enabled_and_disabled_tools_and_disabled_server(work):
    manager = start(work, server(enabled_tools=("echo", "fetch", "status"), disabled_tools=("fetch",)))
    assert [info.name for info in manager.tools] == ["echo", "status"]
    notices: list[str] = []
    manager = start(work, server(enabled=False), notices=notices)
    assert manager.tools == [] and manager.clients == {} and notices == []


def test_failed_server_gives_a_notice(work):
    notices: list[str] = []
    manager = start(work, server("broken", "--exit-at-start"), server("notes"), notices=notices)
    assert {info.server for info in manager.tools} == {"notes"}
    assert len(notices) == 1 and "broken" in notices[0] and "failed to start" in notices[0]
    assert "cannot start: missing database" in (work.parent / "logs" / "mcp-broken.log").read_text()


def test_failed_required_server_stops_the_session(work):
    with pytest.raises(ConfigError, match="required MCP server 'broken' failed to start"):
        start(work, server("broken", "--exit-at-start", required=True), server("notes"))


def test_unexpected_failure_of_a_server_is_a_failed_start(work, monkeypatch):
    started = []
    real = manager_module._start_server

    def start_server(config, root, log_dir):
        if config.name == "odd":
            raise RuntimeError("unexpected")
        result = real(config, root, log_dir)
        started.append(result[0])
        return result

    monkeypatch.setattr(manager_module, "_start_server", start_server)
    notices: list[str] = []
    manager = start(work, server("odd"), server("notes"), notices=notices)
    assert {info.server for info in manager.tools} == {"notes"}
    assert len(notices) == 1 and "odd" in notices[0] and "unexpected" in notices[0]
    with pytest.raises(ConfigError, match="required MCP server 'odd' failed to start"):
        start(work, server("odd", required=True), server("notes"))
    assert not started[-1].running


def test_missing_program_and_startup_timeout_fail_to_start(work):
    notices: list[str] = []
    manager = start(work, McpServerConfig(name="missing", command=str(work / "no-such-program")), notices=notices)
    assert manager.tools == [] and "missing" in notices[0]
    started = time.monotonic()
    manager = start(work, server("slow", "--no-reply", startup_timeout_sec=0.5), notices=notices)
    assert time.monotonic() - started < 5
    assert manager.tools == [] and "timed out" in notices[1]


def test_tool_timeout(work):
    manager = start(work, server(tool_timeout_sec=0.5))
    with pytest.raises(McpError, match="timed out after 0.5 seconds"):
        manager.call_tool("notes", "sleep", {"seconds": 10})
    assert manager.clients["notes"].running
    with pytest.raises(ToolError, match="timed out"):
        tool(manager, "sleep", make_orchestrator(work, RecordingReviewer()), work).run({"seconds": 10})


def test_server_requests_are_answered_and_server_exit_fails_later_calls(manager):
    answers = manager.call_tool("notes", "ask_client", {})["structuredContent"]
    assert answers["s1"]["result"] == {} and answers["s2"]["error"]["code"] == -32601
    with pytest.raises(McpError, match="exited"):
        manager.call_tool("notes", "exit", {})
    with pytest.raises(McpError, match="not running"):
        manager.call_tool("notes", "echo", {"text": "x"})


# Environment, working directory, shutdown


def test_server_runs_with_its_environment_and_the_root_or_configured_directory_as_cwd(work, monkeypatch):
    monkeypatch.setenv("FORWARDED_VAR", "forwarded")
    monkeypatch.setenv("OTHER_VAR", "other")
    monkeypatch.delenv("MISSING_VAR", raising=False)
    (work / "tools").mkdir()
    env_vars = ("FORWARDED_VAR", "MISSING_VAR")
    manager = start(work, server(env={"LITERAL_VAR": "literal"}, env_vars=env_vars), server("tools", cwd="tools"))
    seen = manager.call_tool("notes", "environment", {})["structuredContent"]
    assert seen["env"]["FORWARDED_VAR"] == "forwarded" and seen["env"]["LITERAL_VAR"] == "literal"
    assert "OTHER_VAR" not in seen["env"] and "MISSING_VAR" not in seen["env"]
    assert Path(seen["cwd"]).resolve() == work.resolve()
    seen = manager.call_tool("tools", "environment", {})["structuredContent"]
    assert Path(seen["cwd"]).resolve() == (work / "tools").resolve()


def test_shutdown_stops_the_server(work):
    manager = start(work)
    process = manager.clients["notes"].process
    manager.shutdown()
    assert process.poll() is not None
    with pytest.raises(McpError, match="not running"):
        manager.call_tool("notes", "echo", {})


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs Linux /proc")
def test_shutdown_stops_the_server_process_group(work):
    manager = start(work)
    child = int(manager.call_tool("notes", "start_child", {})["content"][0]["text"])
    manager.shutdown()
    assert process_stops(child)


# Tool names


def test_model_tool_names():
    names = model_tool_names([("notes", "echo"), ("notes", "list.items"), ("my-db", "run query")])
    assert names == {
        ("notes", "echo"): "mcp__notes__echo",
        ("notes", "list.items"): "mcp__notes__list_items",
        ("my-db", "run query"): "mcp__my-db__run_query",
    }


def test_colliding_tool_names_get_a_hash_suffix():
    names = model_tool_names([("notes", "a.b"), ("notes", "a_b"), ("notes", "c")])
    first, second = names[("notes", "a.b")], names[("notes", "a_b")]
    assert first != second
    assert first.startswith("mcp__notes__a_b_") and second.startswith("mcp__notes__a_b_")
    assert names[("notes", "c")] == "mcp__notes__c"
    assert model_tool_names([("notes", "c"), ("notes", "a_b"), ("notes", "a.b")]) == names


def test_long_tool_names_are_shortened():
    long_name = "x" * 100
    names = model_tool_names([("notes", long_name), ("notes", long_name + "y")])
    assert all(len(name) == MAX_TOOL_NAME_CHARS for name in names.values())
    assert len(set(names.values())) == 2


def test_tools_from_two_servers(work):
    manager = start(work, server("alpha", "--tool-names", "run,check"), server("beta", "--tool-names", "run"))
    assert sorted(info.model_name for info in manager.tools) == ["mcp__alpha__check", "mcp__alpha__run", "mcp__beta__run"]


# Approval modes


NO_HINTS = None
READ_ONLY = {"readOnlyHint": True}
DESTRUCTIVE = {"destructiveHint": True}
CLOSED_WORLD = {"destructiveHint": False, "openWorldHint": False}
OPEN_WORLD = {"destructiveHint": False, "openWorldHint": True}


@pytest.mark.parametrize(
    "annotations, auto, writes",
    [
        (NO_HINTS, True, True),
        (READ_ONLY, False, False),
        (DESTRUCTIVE, True, True),
        ({"destructiveHint": True, "readOnlyHint": True}, True, False),
        (CLOSED_WORLD, False, True),
        (OPEN_WORLD, True, True),
        ({"destructiveHint": False}, True, True),
        ({"openWorldHint": False}, True, True),
    ],
)
def test_requires_approval(annotations, auto, writes):
    assert requires_approval(annotations, ToolApprovalMode.AUTO) is auto
    assert requires_approval(annotations, ToolApprovalMode.WRITES) is writes
    assert requires_approval(annotations, ToolApprovalMode.PROMPT) is True
    assert requires_approval(annotations, ToolApprovalMode.APPROVE) is False


@pytest.mark.parametrize(
    "fields, reviewed",
    [
        ({}, {"delete_note", "fetch"}),
        ({"tool_approval_modes": {"echo": ToolApprovalMode.PROMPT}}, {"echo", "delete_note", "fetch"}),
        ({"default_tools_approval_mode": ToolApprovalMode.WRITES}, {"delete_note", "status", "fetch"}),
        ({"default_tools_approval_mode": ToolApprovalMode.PROMPT}, {"echo", "delete_note", "status", "fetch"}),
        (
            {"default_tools_approval_mode": ToolApprovalMode.PROMPT,
             "tool_approval_modes": {"delete_note": ToolApprovalMode.APPROVE}},
            {"echo", "status", "fetch"},
        ),
    ],
)
def test_approval_modes_follow_the_server_hints_and_the_configured_modes(work, fields, reviewed):
    manager = start(work, server(**fields))
    reviewer = RecordingReviewer()
    approvals = make_orchestrator(work, reviewer)
    for name in ("echo", "delete_note", "status", "fetch"):
        tool(manager, name, approvals, work).run({})
    assert {request.mcp_tool for request in reviewer.requests} == reviewed


def test_review_request_shows_the_call_and_each_call_is_reviewed(manager, work):
    reviewer = RecordingReviewer()
    delete = tool(manager, "delete_note", make_orchestrator(work, reviewer), work)
    delete.run({"id": 7})
    delete.run({"id": 7})
    assert len(reviewer.requests) == 2
    action = reviewer.requests[0].planned_action()
    assert action["tool"] == "mcp_tool_call"
    assert (action["server"], action["tool_name"], action["arguments"]) == ("notes", "delete_note", {"id": 7})
    assert action["tool_description"] == "Delete a note."
    assert action["annotations"] == {"destructiveHint": True}
    assert action["cwd"] == str(work)
    with pytest.raises(ToolError, match="denied by user"):
        tool(manager, "delete_note", make_orchestrator(work, RecordingReviewer(approved=False)), work).run({})


def test_policy_never_rejects_calls_that_need_review_unless_there_is_no_sandbox(manager, work):
    reviewer = RecordingReviewer()
    approvals = make_orchestrator(work, reviewer, policy=ApprovalPolicy.NEVER)
    with pytest.raises(ToolError, match="approval_policy = never"):
        tool(manager, "delete_note", approvals, work).run({})
    assert tool(manager, "echo", approvals, work).run({"text": "ok"}) == "ok"
    approvals = make_orchestrator(work, reviewer, policy=ApprovalPolicy.NEVER, mode=SandboxMode.FULL_ACCESS)
    assert tool(manager, "delete_note", approvals, work).run({}) == "delete_note called\n[image content: image/png]"
    assert reviewer.requests == []


# The tool exposed to the model


def test_tools_dispatch_through_the_registry(manager, work):
    registry = ToolRegistry(mcp_tools(manager, make_orchestrator(work, RecordingReviewer()), work))
    specs = {spec["function"]["name"]: spec["function"] for spec in registry.specs()}
    assert len(specs) == 11 and specs["mcp__notes__echo"]["description"] == "Return the text."
    assert specs["mcp__notes__echo"]["parameters"] == {"type": "object", "properties": {"text": {"type": "string"}}}
    output = registry.dispatch("mcp__notes__echo", json.dumps({"text": "hi"}))
    assert output.success and output.text == "hi"
    error = registry.dispatch("mcp__notes__fail", "{}")
    assert not error.success and error.text == "the note does not exist"
    structured = registry.dispatch("mcp__notes__structured", "{}")
    assert structured.success and json.loads(structured.text) == {"count": 3}


def test_format_result():
    assert format_result({"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}) == "a\nb"
    assert format_result({"content": [{"type": "text", "text": "a"}], "structuredContent": {"n": 1}}) == '{"n": 1}'
    assert format_result({"content": [{"type": "audio", "data": "", "mimeType": "audio/wav"}]}) == (
        "[audio content: audio/wav]"
    )
    assert format_result({"content": [{"type": "resource", "resource": {"uri": "file:///a", "text": "body"}}]}) == "body"
    assert format_result({"content": [{"type": "resource_link", "uri": "file:///b", "name": "b"}]}) == (
        "[resource: file:///b]"
    )
    assert format_result({"content": []}) == ""
    with pytest.raises(ToolError, match="bad input"):
        format_result({"content": [{"type": "text", "text": "bad input"}], "isError": True})
