from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from meltzer.agents import control as control_module
from meltzer.agents.control import AgentStatus
from meltzer.agents.roles import BUILT_IN_ROLES, DEFAULT_ROLE, AgentRole, RoleSet, role_config
from meltzer.client import ModelError, ModelResponse
from meltzer.config import load_config
from meltzer.history import Kind, Role
from meltzer.monitor.review import MonitorReviewer
from meltzer.session import Session
from meltzer.tools.agents import WaitTool

AGENT_TOOLS = {"spawn_agent", "send_input", "wait", "close_agent"}

pytestmark = pytest.mark.usefixtures("api_key")


def last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message["role"] == "user":
            return message["content"]
    return ""


class ScriptedClient:
    """Stands in for the model client: answers "done: <last user message>".

    A message that contains "hold" waits until `release` is set; one that
    contains "fail" raises a ModelError.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[list[dict], str | None]] = []
        self.release = threading.Event()
        self.holding = threading.Event()
        self._lock = threading.Lock()

    def complete(self, messages, tools, *, model=None) -> ModelResponse:
        with self._lock:
            self.calls.append(([dict(m) for m in messages], model))
        text = last_user_text(messages)
        if "hold" in text:
            self.holding.set()
            assert self.release.wait(10)
        if "fail" in text:
            raise ModelError("provider unavailable")
        if "interrupt" in text:
            raise KeyboardInterrupt
        return ModelResponse(content=f"done: {text}")

    def close(self) -> None:
        pass

    def models_for(self, text: str) -> list[str | None]:
        with self._lock:
            return [model for messages, model in self.calls if last_user_text(messages) == text]


@pytest.fixture
def make_session(session_factory):
    sessions: list[Session] = []

    def make(*overrides: tuple[str, object], trusted: bool = False) -> Session:
        session = session_factory(*overrides, trusted=trusted)
        session.client = ScriptedClient()
        sessions.append(session)
        return session

    yield make
    for session in sessions:
        session.client.release.set()


def call(session: Session, tool: str, **arguments) -> tuple[str, bool]:
    output = session.tools.dispatch(tool, json.dumps(arguments))
    return output.text, output.success


def call_json(session: Session, tool: str, **arguments) -> dict:
    text, success = call(session, tool, **arguments)
    assert success, text
    return json.loads(text)


def tool_names(session: Session) -> set[str]:
    return {spec["function"]["name"] for spec in session.tools.specs()}


def spawn_background(session: Session, prompt: str, **arguments) -> str:
    result = call_json(session, "spawn_agent", prompt=prompt, background=True, **arguments)
    assert result["status"] == "running"
    return result["agent_id"]


def nested_agents(session: Session) -> tuple[Session, str, str]:
    """A finished child of `session` and a finished grandchild: the child's session and both ids."""
    child_id = spawn_background(session, "child")
    call_json(session, "wait", targets=[child_id])
    child = session.agent_control.get(child_id).session
    grandchild_id = spawn_background(child, "grandchild")
    call_json(child, "wait", targets=[grandchild_id])
    return child, child_id, grandchild_id


def user_items(session: Session) -> list[str]:
    return [item.content for item in session.history if item.role == Role.USER and item.kind == Kind.MESSAGE]


def write_role(directory: Path, filename: str, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(text, encoding="utf-8")
    return path


# --- roles ---------------------------------------------------------------------------------


def test_built_in_roles(tmp_path):
    roles = RoleSet.load(tmp_path / "missing", tmp_path)
    assert roles.names() == ["default", "explorer", "worker"]
    write_role(tmp_path / "user", "notes.txt", 'developer_instructions = "x"\n')
    assert RoleSet.load(tmp_path / "user", tmp_path).names() == roles.names()
    assert all(role.developer_instructions == "" and role.model is None for role in BUILT_IN_ROLES)
    assert roles.describe().startswith("Available roles:\n- default: Default agent.")


def test_user_role_is_loaded(tmp_path):
    user_dir = tmp_path / "user"
    write_role(
        user_dir,
        "reviewer.toml",
        'description = "Reviews diffs."\ndeveloper_instructions = "List the bugs."\nmodel = "small-model"\n',
    )
    roles = RoleSet.load(user_dir, tmp_path)
    role = roles.get("reviewer")
    assert role == AgentRole(
        "reviewer",
        description="Reviews diffs.",
        developer_instructions="List the bugs.",
        model="small-model",
        source=str(user_dir / "reviewer.toml"),
    )
    assert roles.warnings == []
    assert "- reviewer: Reviews diffs." in roles.describe()


def test_name_key_overrides_the_file_name(tmp_path):
    write_role(tmp_path / "user", "a.toml", 'name = "tester"\ndeveloper_instructions = "Run the tests."\n')
    roles = RoleSet.load(tmp_path / "user", tmp_path)
    assert roles.get("tester") is not None
    assert roles.get("a") is None


def test_other_keys_are_ignored_with_a_warning(tmp_path):
    path = write_role(
        tmp_path / "user",
        "fast.toml",
        'developer_instructions = "Be quick."\n'
        'sandbox_mode = "danger-full-access"\n'
        'approval_policy = "never"\n'
        "[monitor]\nmodel = \"other\"\n",
    )
    roles = RoleSet.load(tmp_path / "user", tmp_path)
    assert roles.get("fast") == AgentRole("fast", developer_instructions="Be quick.", source=str(path))
    assert roles.warnings == [
        f"{path}: unknown key 'sandbox_mode' in agent role; ignored",
        f"{path}: unknown key 'approval_policy' in agent role; ignored",
        f"{path}: unknown key 'monitor' in agent role; ignored",
    ]


@pytest.mark.parametrize(
    "text, message",
    [
        ('description = "no instructions"\n', "'developer_instructions' must be a non-empty string"),
        ('developer_instructions = 3\n', "'developer_instructions' must be a string"),
        ('developer_instructions = "x"\nmodel = ""\n', "'model' must be a non-empty string"),
        ('name = "two words"\ndeveloper_instructions = "x"\n', "invalid role name 'two words'"),
        ("developer_instructions = \n", "invalid TOML"),
    ],
)
def test_invalid_role_files_are_rejected(tmp_path, text, message):
    path = write_role(tmp_path / "user", "bad.toml", text)
    roles = RoleSet.load(tmp_path / "user", tmp_path)
    assert roles.get("bad") is None
    assert len(roles.warnings) == 1
    assert roles.warnings[0].startswith(f"{path}: agent role rejected: {message}")


def test_project_roles_only_when_included(tmp_path):
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    write_role(tmp_path / "user", "worker.toml", 'developer_instructions = "user worker"\n')
    write_role(work / ".meltzer" / "agents", "worker.toml", 'developer_instructions = "project worker"\n')
    write_role(work / ".meltzer" / "agents", "builder.toml", 'developer_instructions = "Build it."\n')
    user_only = RoleSet.load(tmp_path / "user", work, home=tmp_path)
    assert user_only.get("builder") is None
    assert user_only.get("worker").developer_instructions == "user worker"
    both = RoleSet.load(tmp_path / "user", work, include_project=True, home=tmp_path)
    assert both.get("builder").developer_instructions == "Build it."
    assert both.get("worker").developer_instructions == "project worker"
    assert both.names().count("worker") == 1


def test_role_config_changes_only_the_model(work, home):
    overrides = [("model", "big-model"), ("sandbox_mode", "read-only"), ("approval_policy", "never")]
    config = load_config(work, home=home, overrides=overrides)
    child = role_config(config, AgentRole("small", developer_instructions="x", model="small-model"))
    assert child.model == "small-model"
    assert child.monitor_model == "big-model"
    assert child.sources["model"] == "agent role small"
    assert child.sandbox_mode == config.sandbox_mode
    assert child.approval_policy == config.approval_policy
    assert child.approvals_reviewer == config.approvals_reviewer
    assert child.monitor_transcript_items == config.monitor_transcript_items
    assert role_config(config, BUILT_IN_ROLES[0]) is config


def test_role_config_uses_agents_default_model(work, home):
    overrides = [("model", "big-model"), ("agents.default_model", "agent-model"), ("monitor.model", "review-model")]
    config = load_config(work, home=home, overrides=overrides)
    child = role_config(config, BUILT_IN_ROLES[0])
    assert child.model == "agent-model"
    assert child.monitor_model == "review-model"
    with_model = role_config(config, AgentRole("r", developer_instructions="x", model="role-model"))
    assert with_model.model == "role-model"


def test_role_warnings_are_shown_at_session_start(make_session, home):
    write_role(home / ".meltzer" / "agents", "r.toml", 'developer_instructions = "x"\nextra = 1\n')
    session = make_session()
    assert "unknown key 'extra' in agent role" in session.ui.stderr.getvalue()
    assert session.agent_control.roles.get("r") is not None


def test_project_roles_need_a_trusted_project(make_session, work):
    write_role(work / ".meltzer" / "agents", "builder.toml", 'developer_instructions = "Build it."\n')
    assert make_session().agent_control.roles.get("builder") is None
    assert make_session(trusted=True).agent_control.roles.get("builder") is not None


# --- tool registration ---------------------------------------------------------------------


def test_agent_tools_are_registered_below_the_depth_limit(make_session):
    session = make_session()
    assert AGENT_TOOLS <= tool_names(session)
    child = session.start_subagent()
    try:
        assert child.depth == 1
        assert not AGENT_TOOLS & tool_names(child)
    finally:
        child.close()


def test_deeper_trees_with_a_higher_depth_limit(make_session):
    session = make_session(("agents.max_depth", 2))
    child = session.start_subagent()
    grandchild = child.start_subagent()
    try:
        assert AGENT_TOOLS <= tool_names(child)
        assert grandchild.depth == 2
        assert not AGENT_TOOLS & tool_names(grandchild)
        assert grandchild.agent_control is session.agent_control
        with pytest.raises(control_module.AgentError, match=r"agent depth limit reached \(agents.max_depth = 2\)"):
            session.agent_control.start(grandchild, None)
    finally:
        grandchild.close()
        child.close()


def test_spawn_agent_spec_lists_the_roles(make_session, home):
    write_role(home / ".meltzer" / "agents", "reviewer.toml", 'description = "Reviews diffs."\ndeveloper_instructions = "x"\n')
    session = make_session()
    (spec,) = [s["function"] for s in session.tools.specs() if s["function"]["name"] == "spawn_agent"]
    properties = spec["parameters"]["properties"]
    assert set(properties) == {"prompt", "agent_type", "background"}
    assert spec["parameters"]["required"] == ["prompt"]
    assert "- reviewer: Reviews diffs." in properties["agent_type"]["description"]
    assert "- explorer:" in properties["agent_type"]["description"]


# --- spawn_agent ---------------------------------------------------------------------------


def test_spawn_returns_the_final_message_and_closes_the_child(make_session):
    session = make_session()
    text, success = call(session, "spawn_agent", prompt="count the files")
    assert success
    assert text == "done: count the files"
    (handle,) = session.agent_control.children(session.id)
    assert handle.status == AgentStatus.CLOSED
    assert handle.final == "done: count the files"
    assert handle.session.recorder._file.closed
    first = next(item for item in handle.session.history if item.kind == Kind.MESSAGE)
    assert (first.role, first.content) == (Role.USER, "count the files")


def test_child_shares_the_parent_rules_runner_and_policy(make_session):
    session = make_session()
    child = session.start_subagent()
    try:
        assert child.parent is session
        assert child.rules is session.rules
        assert child.runner is session.runner
        assert child.policy == session.policy
        assert child.settings == session.settings
        assert child.orchestrator is not session.orchestrator
    finally:
        child.close()


def test_spawn_with_a_role(make_session, home):
    write_role(
        home / ".meltzer" / "agents",
        "reviewer.toml",
        'developer_instructions = "List the bugs you find."\nmodel = "small-model"\n',
    )
    session = make_session(("model", "big-model"))
    text, success = call(session, "spawn_agent", prompt="review the diff", agent_type="reviewer")
    assert success, text
    (handle,) = session.agent_control.children(session.id)
    assert handle.role.name == "reviewer"
    assert handle.session.config.model == "small-model"
    system = handle.session._messages()[0]["content"]
    assert system.endswith("\n# Agent role: reviewer\nList the bugs you find.")
    assert session.client.models_for("review the diff") == ["small-model"]
    meta = json.loads(handle.session.recorder.path.read_text(encoding="utf-8").splitlines()[0])
    assert (meta["parent_session_id"], meta["agent_role"]) == (session.id, "reviewer")


def test_unknown_agent_type_is_an_error(make_session):
    session = make_session()
    text, success = call(session, "spawn_agent", prompt="x", agent_type="nope")
    assert not success
    assert "unknown agent_type 'nope'" in text
    assert "default, explorer, worker" in text
    assert session.agent_control.children(session.id) == []


def test_failed_child_turn_is_a_tool_error(make_session):
    session = make_session()
    text, success = call(session, "spawn_agent", prompt="fail please")
    assert not success
    assert "failed: provider unavailable" in text
    (handle,) = session.agent_control.children(session.id)
    assert handle.status == AgentStatus.CLOSED


# --- background agents, wait, send_input ---------------------------------------------------


def test_background_spawn_and_wait(make_session):
    session = make_session()
    agent_id = spawn_background(session, "hold on")
    assert session.client.holding.wait(5)
    assert session.agent_control.get(agent_id).status == AgentStatus.RUNNING
    session.client.release.set()
    result = call_json(session, "wait", targets=[agent_id])
    assert result == {
        "status": {agent_id: {"status": "completed", "final_message": "done: hold on"}},
        "timed_out": False,
    }
    handle = session.agent_control.get(agent_id)
    assert handle.status == AgentStatus.COMPLETED
    assert handle.thread is None


def test_wait_returns_when_any_target_finishes(make_session):
    session = make_session()
    slow = spawn_background(session, "hold this one")
    assert session.client.holding.wait(5)
    fast = spawn_background(session, "quick one")
    result = call_json(session, "wait", targets=[slow, fast])
    assert list(result["status"]) == [fast]
    session.client.release.set()


def test_wait_times_out(make_session, monkeypatch):
    monkeypatch.setattr(control_module, "MIN_WAIT_TIMEOUT_MS", 50)
    session = make_session()
    agent_id = spawn_background(session, "hold on")
    result = call_json(session, "wait", targets=[agent_id], timeout_ms=50)
    assert result == {"status": {}, "timed_out": True}
    session.client.release.set()


@pytest.mark.parametrize(
    "arguments, message",
    [
        ({"targets": []}, "'targets' must be a non-empty list of agent ids"),
        ({"targets": "abc"}, "'targets' must be a non-empty list of agent ids"),
        ({"targets": ["x"], "timeout_ms": 0}, "'timeout_ms' must be greater than zero"),
        ({"targets": ["x"], "timeout_ms": "5"}, "'timeout_ms' must be an integer"),
        ({"targets": ["x"]}, "unknown agent: x"),
    ],
)
def test_wait_argument_errors(make_session, arguments, message):
    session = make_session()
    text, success = call(session, "wait", **arguments)
    assert not success
    assert text == message


def test_wait_clamps_the_timeout():
    seen = []

    class Control:
        def wait(self, owner, targets, timeout):
            seen.append(timeout)
            return {}, True

    tool = WaitTool(Control(), owner=None)
    for arguments in ({"timeout_ms": 1}, {"timeout_ms": 10**12}, {}):
        tool.run({"targets": ["a"], **arguments})
    limits = (control_module.MIN_WAIT_TIMEOUT_MS, control_module.MAX_WAIT_TIMEOUT_MS)
    assert seen == [limit / 1000 for limit in (*limits, control_module.DEFAULT_WAIT_TIMEOUT_MS)]


def test_background_failure_is_reported_by_wait(make_session):
    session = make_session()
    agent_id = spawn_background(session, "fail now")
    result = call_json(session, "wait", targets=[agent_id])
    assert result["status"][agent_id] == {"status": "failed", "error": "provider unavailable"}


def test_interrupted_background_turn_marks_the_agent_failed(make_session, monkeypatch):
    raised = []
    ended = threading.Event()

    def excepthook(args):
        raised.append(args.exc_type)
        ended.set()

    monkeypatch.setattr(threading, "excepthook", excepthook)
    session = make_session()
    agent_id = spawn_background(session, "interrupt now")
    result = call_json(session, "wait", targets=[agent_id])
    assert result["status"][agent_id] == {"status": "failed", "error": "interrupted"}
    assert ended.wait(5)
    assert raised == [KeyboardInterrupt]
    assert session.agent_control.get(agent_id).thread is None


def test_send_input_to_a_finished_agent_starts_a_new_turn(make_session):
    session = make_session()
    agent_id = spawn_background(session, "first task")
    call_json(session, "wait", targets=[agent_id])
    result = call_json(session, "send_input", target=agent_id, message="second task")
    assert result == {"agent_id": agent_id, "status": "running"}
    waited = call_json(session, "wait", targets=[agent_id])
    assert waited["status"][agent_id]["final_message"] == "done: second task"
    child = session.agent_control.get(agent_id).session
    assert user_items(child) == ["first task", "second task"]


def test_send_input_to_a_running_agent_is_queued(make_session):
    session = make_session()
    agent_id = spawn_background(session, "hold first")
    assert session.client.holding.wait(5)
    call_json(session, "send_input", target=agent_id, message="then this")
    session.client.release.set()
    waited = call_json(session, "wait", targets=[agent_id])
    assert waited["status"][agent_id]["final_message"] == "done: then this"
    child = session.agent_control.get(agent_id).session
    assert user_items(child) == ["hold first", "then this"]


def test_send_input_errors(make_session):
    session = make_session()
    text, success = call(session, "send_input", target="nope", message="hi")
    assert (text, success) == ("unknown agent: nope", False)
    text, success = call(session, "send_input", target="nope")
    assert (text, success) == ("'message' must be a non-empty string", False)


# --- close_agent ---------------------------------------------------------------------------


def test_close_a_finished_agent(make_session):
    session = make_session()
    agent_id = spawn_background(session, "small task")
    call_json(session, "wait", targets=[agent_id])
    result = call_json(session, "close_agent", target=agent_id)
    assert result == {"agent_id": agent_id, "previous_status": "completed"}
    handle = session.agent_control.get(agent_id)
    assert handle.status == AgentStatus.CLOSED
    assert handle.session.recorder._file.closed
    text, success = call(session, "send_input", target=agent_id, message="more")
    assert (text, success) == (f"agent {agent_id} is closed", False)
    assert call_json(session, "close_agent", target=agent_id)["previous_status"] == "closed"
    waited = call_json(session, "wait", targets=[agent_id])
    assert waited["status"][agent_id] == {"status": "closed", "final_message": "done: small task"}


def test_close_a_running_agent_stops_it(make_session):
    session = make_session()
    agent_id = spawn_background(session, "hold for a while")
    assert session.client.holding.wait(5)
    handle = session.agent_control.get(agent_id)
    thread = handle.thread
    call_json(session, "send_input", target=agent_id, message="queued")
    assert call_json(session, "close_agent", target=agent_id)["previous_status"] == "running"
    session.client.release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert handle.status == AgentStatus.CLOSED
    assert handle.session.recorder._file.closed
    assert user_items(handle.session) == ["hold for a while"]
    # The closed agent made no model request after its first one.
    assert session.client.models_for("hold for a while") == [session.config.model]


def test_close_agent_closes_descendants(make_session):
    session = make_session(("agents.max_depth", 2))
    _, child_id, grandchild_id = nested_agents(session)
    call_json(session, "close_agent", target=child_id)
    assert session.agent_control.get(grandchild_id).status == AgentStatus.CLOSED


def test_session_close_closes_background_children(make_session):
    session = make_session()
    agent_id = spawn_background(session, "hold until the end")
    assert session.client.holding.wait(5)
    handle = session.agent_control.get(agent_id)
    thread = handle.thread
    session.client.release.set()
    session.close()
    thread.join(5)
    assert handle.status == AgentStatus.CLOSED
    assert handle.session.recorder._file.closed


# --- limits and targets --------------------------------------------------------------------


def test_thread_limit(make_session):
    session = make_session(("agents.max_threads", 2))
    first = spawn_background(session, "one")
    spawn_background(session, "two")
    text, success = call(session, "spawn_agent", prompt="three", background=True)
    assert not success
    assert text == "subagent not started: agent thread limit reached (agents.max_threads = 2); close an agent before starting another"
    call_json(session, "wait", targets=[first])
    call_json(session, "close_agent", target=first)
    spawn_background(session, "three")


def test_finished_foreground_agents_do_not_count_toward_the_limit(make_session):
    session = make_session(("agents.max_threads", 1))
    for prompt in ("one", "two", "three"):
        assert call(session, "spawn_agent", prompt=prompt) == (f"done: {prompt}", True)


def test_thread_limit_counts_the_whole_tree(make_session):
    session = make_session(("agents.max_depth", 2), ("agents.max_threads", 2))
    nested_agents(session)
    text, success = call(session, "spawn_agent", prompt="another", background=True)
    assert not success
    assert "agent thread limit reached" in text


def test_agents_of_another_session_cannot_be_targeted(make_session):
    session = make_session(("agents.max_depth", 2))
    child, child_id, grandchild_id = nested_agents(session)
    for tool, arguments in (
        ("send_input", {"target": grandchild_id, "message": "hi"}),
        ("wait", {"targets": [grandchild_id]}),
        ("close_agent", {"target": grandchild_id}),
    ):
        assert call(session, tool, **arguments) == (f"unknown agent: {grandchild_id}", False)
    assert call(child, "close_agent", target=child_id) == (f"unknown agent: {child_id}", False)


# --- approval review -----------------------------------------------------------------------


def test_each_agent_has_its_own_monitor_reviewer_with_the_parent_review_model(make_session, home):
    write_role(home / ".meltzer" / "agents", "r.toml", 'developer_instructions = "x"\nmodel = "small-model"\n')
    session = make_session(("model", "big-model"), ("approval_policy", "on-request"), ("approvals_reviewer", "monitor"))
    child = session.agent_control.start(session, "r").session
    assert isinstance(child.reviewer, MonitorReviewer) and child.reviewer is not session.reviewer
    assert child.orchestrator.reviewer is child.reviewer
    assert child.config.model == "small-model"
    assert child.reviewer.model == session.reviewer.model == "big-model"


def test_agent_client_stops_after_close(make_session):
    session = make_session()
    handle = session.agent_control.start(session, None)
    assert handle.role.name == handle.session.role.name == DEFAULT_ROLE
    session.agent_control.close(session, handle.id)
    with pytest.raises(ModelError, match="the agent was closed"):
        handle.session.client.complete([], [])

