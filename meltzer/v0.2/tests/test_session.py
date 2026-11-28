from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from meltzer import cli
from meltzer.approval.policy import ApprovalRequest
from conftest import ScriptedClient
from meltzer.client import ModelResponse, ToolCall
from meltzer.config import ConfigError, load_config
from meltzer.history import Item, Kind
from meltzer.sandbox.grants import Grant, GrantScope
from meltzer.sandbox.policy import resolve
from meltzer.monitor.review import MonitorReviewer
from meltzer.session import Session
from meltzer.ui import TerminalUI

TEXT = "Keep commits small.\nNever edit generated files.\nmarker-7f3a\n"
HEADING = "\n# Developer instructions\n"

pytestmark = pytest.mark.usefixtures("api_key")


@pytest.fixture
def instructions_file(tmp_path: Path) -> Path:
    path = tmp_path / "instructions.md"
    path.write_text(TEXT, encoding="utf-8")
    return path


@pytest.fixture
def make_session(session_factory):
    def make(path: Path | None = None) -> Session:
        return session_factory(*([] if path is None else [("developer_instructions_file", str(path))]))

    return make


def system_message(session: Session) -> str:
    first = session._messages()[0]
    assert first["role"] == "system"
    return first["content"]


def files_under(path: Path) -> list[Path]:
    return [p for p in path.rglob("*") if p.is_file()]


# --- developer_instructions_file ----------------------------------------------------------


def test_text_is_appended_to_the_system_message(make_session, instructions_file):
    session = make_session(instructions_file)
    content = system_message(session)
    assert content.endswith(HEADING + TEXT)


def test_no_section_without_text(make_session, tmp_path):
    assert "Developer instructions" not in system_message(make_session())
    empty = tmp_path / "empty.md"
    empty.write_text("", encoding="utf-8")
    assert "Developer instructions" not in system_message(make_session(empty))


def test_monitor_never_receives_the_text(make_session, instructions_file):
    session = make_session(instructions_file)
    session.history.append(Item.user("install the package"))
    verdict = '{"risk_level": "low", "user_authorization": "high", "outcome": "allow", "rationale": "ok"}'
    client = session.client = ScriptedClient(ModelResponse(content=verdict))
    request = ApprovalRequest(tool="shell", cwd=session.root, command="pip install -e .", escalate=True)
    assert MonitorReviewer(session).review(request).approved
    (messages,) = client.calls
    assert not any("marker-7f3a" in json.dumps(message) for message in messages)
    assert "install the package" in messages[-1]["content"]


def test_text_is_not_written_to_the_session_log(make_session, instructions_file, home):
    session = make_session(instructions_file)
    session.client = ScriptedClient()
    session.run_turn("go")
    assert "marker-7f3a" in json.dumps(session.client.calls)
    session.recorder.close()
    meta = json.loads(session.recorder.path.read_text(encoding="utf-8").splitlines()[0])
    assert meta["config"]["developer_instructions_file"] == str(instructions_file)
    for path in files_under(home):
        assert b"marker-7f3a" not in path.read_bytes(), path


def test_subagent_reuses_the_text_without_reading_the_file(make_session, instructions_file):
    session = make_session(instructions_file)
    instructions_file.unlink()
    subagent = session.start_subagent()
    try:
        assert system_message(subagent) == system_message(session)
        assert system_message(subagent).endswith(HEADING + TEXT)
    finally:
        subagent.close()


@pytest.mark.skipif(sys.platform != "linux", reason="needs /dev/stdin")
def test_file_can_be_dev_stdin_while_the_prompt_is_an_argument(tmp_path, home, work):
    script = (
        "import json, sys\n"
        "from meltzer import cli\n"
        "from meltzer.session import Session\n"
        "def run_turn(self, prompt):\n"
        "    print(json.dumps({'prompt': prompt, 'system': self._messages()[0]['content']}))\n"
        "    return 'done'\n"
        "Session.run_turn = run_turn\n"
        "sys.exit(cli.main(sys.argv[1:]))\n"
    )
    argv = ["exec", "-C", str(work), "-a", "never", "-c", "developer_instructions_file=/dev/stdin", "the prompt"]
    result = subprocess.run(
        [sys.executable, "-c", script, *argv],
        input=TEXT,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home), "MELTZER_API_KEY": "test-key"},
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    reported = json.loads(result.stdout.splitlines()[0])
    assert reported["prompt"] == "the prompt"
    assert reported["system"].endswith(HEADING + TEXT)


def test_unreadable_file_is_a_config_error(tmp_path, home, work, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(home))
    missing = tmp_path / "missing.md"
    argv = ["exec", "-C", str(work), "-a", "never", "-c", f"developer_instructions_file={missing}", "go"]
    assert cli.main(argv) == cli.EXIT_CONFIG
    assert "developer_instructions_file" in capsys.readouterr().err


def test_invalid_utf8_error_does_not_show_the_text(tmp_path, make_session):
    path = tmp_path / "binary.md"
    path.write_bytes(b"marker-7f3a \xff\xfe")
    with pytest.raises(ConfigError, match="not valid UTF-8") as exc:
        make_session(path)
    assert "marker-7f3a" not in str(exc.value)


# --- tools -------------------------------------------------------------------------------


def tool_names(session: Session) -> list[str]:
    return [spec["function"]["name"] for spec in session.tools.specs()]


@pytest.mark.parametrize(
    "unified_exec, offered, absent",
    [(True, {"exec_command", "write_stdin"}, {"shell"}), (False, {"shell"}, {"exec_command", "write_stdin"})],
)
def test_one_set_of_command_tools(session_factory, unified_exec, offered, absent):
    session = session_factory(("features.unified_exec", unified_exec))
    names = set(tool_names(session))
    assert offered <= names and not absent & names
    tools_section = session.instructions.split("# Tools\n")[1].split("\n# ")[0]
    assert all(f"`{name}`" in tools_section for name in offered)
    assert not any(f"`{name}`" in tools_section for name in absent)
    assert "<command tools>" not in session.instructions


def test_subagent_has_its_own_grants_and_no_agent_tools(make_session):
    session = make_session()
    child = session.start_subagent()
    try:
        assert child.grants is not session.grants
        assert child.process_store is session.process_store and child.mcp is session.mcp
        assert "spawn_agent" not in tool_names(child) and "exec_command" in tool_names(child)
    finally:
        child.close()


class StoredStub:
    def __init__(self, process_id: int, owner: str):
        self.id = process_id
        self.owner = owner
        self.last_used = 0.0
        self.terminated = False

    def has_exited(self) -> bool:
        return False

    def terminate(self) -> None:
        self.terminated = True

    def remaining_group(self) -> int | None:
        return None


def test_closing_a_subagent_stops_only_its_own_processes(make_session):
    session = make_session()
    child = session.start_subagent()
    mine, theirs = StoredStub(1, session.id), StoredStub(2, child.id)
    session.process_store.add(mine)
    session.process_store.add(theirs)
    child.close()
    assert theirs.terminated and not mine.terminated
    session.close()
    assert mine.terminated


class StubMcpManager:
    tools: list = []

    def __init__(self):
        self.stopped = False

    def shutdown(self) -> None:
        self.stopped = True


class StubClient:
    closed = False

    def close(self) -> None:
        self.closed = True


def test_failed_session_start_releases_everything_it_created(tmp_path, home, work, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    mcp, client = StubMcpManager(), StubClient()
    monkeypatch.setattr("meltzer.session.McpManager.start", lambda *args, **kwargs: mcp)
    monkeypatch.setattr("meltzer.session.ChatClient", lambda *args, **kwargs: client)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("meltzer.session.HookRunner.for_session", fail)
    config = load_config(work, home=home, overrides=[("approval_policy", "never")])
    with pytest.raises(OSError, match="disk full"):
        Session(config, work, TerminalUI(interactive=False, stderr=io.StringIO()), home=home)
    assert mcp.stopped and client.closed
    assert list(tmp_path.glob("meltzer-*")) == []


def test_close_runs_every_step_when_one_fails(make_session, tmp_path, monkeypatch):
    session = make_session()
    process = StoredStub(1, session.id)
    session.process_store.add(process)

    def fail(reason):
        raise OSError("hook failed")

    monkeypatch.setattr(session.hooks, "session_end", fail)
    session.close()
    assert process.terminated
    assert "closing the session failed: hook failed" in session.ui.stderr.getvalue()
    assert list(tmp_path.glob("meltzer-*")) == []


# --- environment item ----------------------------------------------------------------------


def test_environment_item_is_added_again_when_grants_change(make_session, tmp_path):
    session = make_session()
    extra = tmp_path / "out"
    extra.mkdir()
    session.grants.add(Grant(GrantScope.TURN, write=(extra,)))
    session.refresh_environment()
    assert f"<root>{resolve(extra)}</root>" in session.history.last_of_kind(Kind.ENVIRONMENT).content
    session.grants.end_turn()
    session.refresh_environment()
    environment = [item for item in session.history if item.kind == Kind.ENVIRONMENT]
    assert len(environment) == 3 and environment[2].content == environment[0].content


def test_tool_calls_reach_hooks_with_their_arguments(make_session):
    arguments = '{"plan": []}'
    assert make_session().tools.hook_input("update_plan", arguments) == arguments


# --- denials of automatic review ---------------------------------------------------------------

DENY_VERDICT = ModelResponse(
    content='{"risk_level": "high", "user_authorization": "unknown", "outcome": "deny", "rationale": "not needed"}'
)
STOPPED = "Stopped: automatic review rejected too many approval requests for this turn (3 consecutive, 3 in the last 50 reviews)."


def escalated(*call_ids: str) -> ModelResponse:
    arguments = json.dumps({"cmd": "touch /etc/motd", "escalate": True, "justification": "update the banner"})
    return ModelResponse(content=None, tool_calls=[ToolCall(call_id, "exec_command", arguments) for call_id in call_ids])


@pytest.fixture
def monitored(session_factory):
    def make(**kwargs) -> Session:
        return session_factory(("approval_policy", "on-request"), ("approvals_reviewer", "monitor"), **kwargs)

    return make


def test_three_denials_in_a_row_end_the_turn(monitored):
    session = monitored()
    session.client = ScriptedClient(
        escalated("t1"), DENY_VERDICT, escalated("t2"), DENY_VERDICT, escalated("t3", "t4"), DENY_VERDICT
    )
    assert session.run_turn("update the login banner") == STOPPED
    assert len(session.client.calls) == 6  # no model request after the third denial
    results = {item.call_id: item.content for item in session.history if item.kind == Kind.TOOL_RESULT}
    assert results["t4"] == "aborted" and "not needed" in results["t3"]
    assert session.run_turn("leave it as it is") == "done"  # the next turn runs


def test_denials_count_per_turn(monitored):
    session = monitored()
    session.client = ScriptedClient(
        escalated("t1"), DENY_VERDICT, escalated("t2"), DENY_VERDICT, ModelResponse(content="cannot do it"),
        escalated("t3"), DENY_VERDICT, ModelResponse(content="still cannot"),
    )  # fmt: skip
    assert session.run_turn("update the login banner") == "cannot do it"
    assert session.run_turn("try once more") == "still cannot"


def test_each_session_counts_its_own_denials(monitored):
    session = monitored()
    subagent = session.start_subagent()
    try:
        session.rejection_breaker.record_denial()
        session.rejection_breaker.record_denial()
        subagent.client = ScriptedClient(DENY_VERDICT)
        request = ApprovalRequest(tool="exec_command", cwd=subagent.root, command="touch /etc/motd", escalate=True)
        assert not subagent.reviewer.review(request).approved
        assert (subagent.rejection_breaker.consecutive_denials, session.rejection_breaker.consecutive_denials) == (1, 2)
    finally:
        subagent.close()


def test_exec_prints_a_stop_line_and_exec_resume_continues(home, work, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    client = ScriptedClient(escalated("t1"), DENY_VERDICT, escalated("t2"), DENY_VERDICT, escalated("t3"), DENY_VERDICT)
    monkeypatch.setattr("meltzer.session.ChatClient", lambda *args, **kwargs: client)
    options = ["-C", str(work), "-a", "on-request", "-c", 'approvals_reviewer="monitor"']
    assert cli.main(["exec", *options, "update the login banner"]) == cli.EXIT_OK
    out = capsys.readouterr()
    assert out.out.strip() == STOPPED
    assert "interrupting the turn" in out.err
    (path,) = (home / ".meltzer" / "sessions").rglob("*.jsonl")
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [r["consecutive_denials"] for r in lines if r["type"] == "turn_interrupted"] == [3]

    assert cli.main(["exec", "resume", *options, lines[0]["id"], "leave it as it is"]) == cli.EXIT_OK
    assert capsys.readouterr().out.strip() == "done"
