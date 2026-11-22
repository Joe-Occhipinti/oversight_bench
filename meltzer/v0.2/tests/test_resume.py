from __future__ import annotations

import io
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from meltzer import cli
from meltzer.client import ModelResponse, ToolCall
from meltzer.config import ConfigError
from meltzer.history import (
    REBUILT_KINDS,
    History,
    Item,
    Kind,
    Role,
    SessionFileError,
    find_session,
    load_session,
    session_file,
)
from meltzer.hook_output import HookOutcome
from meltzer.hooks import HookRunner
from meltzer.session import Session
from meltzer.tools.plan import StepStatus
from meltzer.sessions import (
    ROLLBACK_COMMAND,
    StartMode,
    open_initial_history,
    rollback,
    run_rollback_command,
)
from meltzer.ui import TerminalUI

from conftest import ScriptedClient

pytestmark = pytest.mark.usefixtures("api_key")


@pytest.fixture
def sessions_dir(home: Path) -> Path:
    return home / ".meltzer" / "sessions"


@pytest.fixture
def make_session(session_factory, home):
    def make(*overrides: tuple[str, object], mode: StartMode | None = None, session_id: str | None = None) -> Session:
        initial = None if mode is None else open_initial_history(mode, session_id, home=home)
        return session_factory(*overrides, initial=initial)

    return make


def run(session: Session, prompt: str, *responses: ModelResponse) -> ScriptedClient:
    client = ScriptedClient(*responses)
    session.client = client
    session.run_turn(prompt)
    return client


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def conversation(items) -> list[tuple[str, str]]:
    return [(item.kind.value, item.content) for item in items if item.kind not in REBUILT_KINDS]


def write_session(path: Path, *lines: dict, meta: dict | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"type": "session_meta", "id": "s1", **(meta or {})}
    path.write_text("".join(json.dumps(line) + "\n" for line in (meta, *lines)), encoding="utf-8")
    return path


def item_line(item: Item) -> dict:
    return {"type": "item", **item.to_record()}


# --- history: records, turns, replay --------------------------------------------------------


def test_item_round_trips_through_its_record():
    items = [
        Item.user("fix it"),
        Item.reasoning_fields({"reasoning_content": "hm"}, "r1"),
        Item.tool_call("c1", "shell", '{"command": "ls"}', "r1"),
        Item.tool_result("c1", "shell", "a.py"),
        Item(Role.USER, Kind.MESSAGE, content="spoken", source="voice"),
    ]
    for item in items:
        assert Item.from_record({"type": "item", **item.to_record()}) == item


def test_drop_last_user_turns_keeps_everything_before_the_cut():
    history = History()
    agents_md = Item(Role.USER, Kind.AGENTS_MD, content="rules")
    first, second, third = Item.user("one"), Item.user("two"), Item.user("three")
    for item in (agents_md, first, Item.assistant("a1", "r1"), second, Item.assistant("a2", "r2"), third):
        history.append(item)
    history.append(Item.tool_call("c1", "shell", "{}", "r3"))
    history.append(Item.tool_result("c1", "shell", "out"))

    assert history.drop_last_user_turns(2) == 5
    assert [item.content for item in history] == ["rules", "one", "a1"]
    assert history.drop_last_user_turns(1) == 2
    assert list(history) == [agents_md]


@pytest.mark.parametrize("num_turns", [0, -1, 3])
def test_drop_last_user_turns_rejects_counts_outside_the_history(num_turns):
    history = History()
    history.append(Item.user("one"))
    history.append(Item.user("two"))
    with pytest.raises(ValueError):
        history.drop_last_user_turns(num_turns)
    assert len(history) == 2
    # Only user messages start a turn.
    history.append(Item(Role.USER, Kind.ENVIRONMENT, content="env"))
    history.append(Item(Role.USER, Kind.HOOK_PROMPT, content="continue"))
    with pytest.raises(ValueError, match="2 user turns$"):
        history.drop_last_user_turns(3)


def context(kind: Kind, text: str) -> dict:
    return item_line(Item(Role.USER, kind, content=text))


def reply(text: str) -> dict:
    return item_line(Item.assistant(text, "r1"))


SUMMARY = Item(Role.USER, Kind.COMPACTION_SUMMARY, content="summary")


@pytest.mark.parametrize(
    "lines, expected",
    [
        (
            [
                context(Kind.AGENTS_MD, "rules"),
                item_line(Item.user("one")),
                context(Kind.PLAN, "# Plan"),
                reply("old"),
                {"type": "item_updated", "index": 3, "item": Item.assistant("new", "r1").to_record()},
                {"type": "review", "outcome": "allow"},
                item_line(Item.user("two")),
                {"type": "item_removed", "index": 2},
                reply("reply"),
            ],
            ["one", "new", "two", "reply"],
        ),
        (
            [
                item_line(Item.user("one")),
                reply("a1"),
                {
                    "type": "compacted",
                    "dropped_items": 2,
                    "replacement_history": [Item(Role.USER, Kind.AGENTS_MD, content="rules").to_record(), SUMMARY.to_record()],
                },
                item_line(Item.user("two")),
            ],
            ["summary", "two"],
        ),
        (
            [item_line(Item.user("one")), reply("a1"), item_line(Item.user("two")), reply("a2")]
            + [{"type": "rolled_back", "num_turns": 1}, item_line(Item.user("three"))],
            ["one", "a1", "three"],
        ),
        (
            [
                context(Kind.ENVIRONMENT, "env 1"),
                item_line(Item.user("one")),
                {"type": "resumed", "config": {}},
                context(Kind.ENVIRONMENT, "env 2"),
                context(Kind.PLAN, "plan"),
                {"type": "item_removed", "index": 2},
                {"type": "item_updated", "index": 0, "item": Item.user("one, edited").to_record()},
                item_line(Item.user("two")),
            ],
            ["one, edited", "two"],
        ),
    ],
)
def test_load_session_replays_the_records(tmp_path, lines, expected):
    _, items = load_session(write_session(tmp_path / "s.jsonl", *lines, meta={"cwd": "/w"}))
    assert [item.content for item in items] == expected


@pytest.mark.parametrize("kind", [*sorted(REBUILT_KINDS), Kind.HOOK_PROMPT, Kind.USER_SHELL_COMMAND, Kind.COMPACTION_SUMMARY])
def test_load_session_keeps_the_conversation_items_and_leaves_out_the_context_items(tmp_path, kind):
    path = write_session(tmp_path / "s.jsonl", item_line(Item(Role.USER, kind, content="text", source="hook:Stop")))
    meta, items = load_session(path)
    assert meta["id"] == "s1"
    assert [(item.kind, item.role, item.source) for item in items] == (
        [] if kind in REBUILT_KINDS else [(kind, Role.USER, "hook:Stop")]
    )


def test_load_session_ignores_an_unfinished_last_line(tmp_path):
    path = write_session(tmp_path / "s.jsonl", item_line(Item.user("one")))
    with path.open("a", encoding="utf-8") as file:
        file.write('{"type": "item", "role": "us')
    _, items = load_session(path)
    assert [item.content for item in items] == ["one"]


@pytest.mark.parametrize(
    "text",
    [
        "",
        '{"type": "item", "role": "user", "kind": "message"}\n',
        '{"type": "session_meta", "id": "s1"}\nnot json\n',
        '{"type": "session_meta", "id": "s1"}\n{"type": "item", "role": "user", "kind": "other"}\n',
        '{"type": "session_meta", "id": "s1"}\n{"type": "item_removed", "index": 4}\n',
        '{"type": "session_meta", "id": "s1"}\n{"type": "rolled_back", "num_turns": 2}\n',
        None,
    ],
)
def test_unreadable_session_files_are_errors(tmp_path, text):
    path = tmp_path / "s.jsonl"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    with pytest.raises(SessionFileError):
        load_session(path)


# --- finding sessions -------------------------------------------------------------------------


def new_file(
    sessions_dir: Path, *, started: datetime, mtime: float, parent: str | None = None, cwd: str | None = None
) -> tuple[str, Path]:
    session_id = str(uuid.uuid4())
    meta = {"id": session_id, **({"parent_session_id": parent} if parent else {}), **({"cwd": cwd} if cwd else {})}
    path = write_session(session_file(sessions_dir, session_id, started), meta=meta)
    os.utime(path, (mtime, mtime))
    return session_id, path


def test_find_session_by_id(sessions_dir):
    started = datetime(2026, 5, 6, 7, 8, 9, tzinfo=timezone.utc)
    session_id, path = new_file(sessions_dir, started=started, mtime=1_000)
    new_file(sessions_dir, started=started, mtime=2_000)
    assert find_session(sessions_dir, session_id) == path
    assert find_session(sessions_dir, session_id.upper()) == path
    with pytest.raises(SessionFileError, match="no recorded session"):
        find_session(sessions_dir, str(uuid.uuid4()))


@pytest.mark.parametrize("session_id", ["../../work/session", "*", "abc", "", "2026/05/06/x"])
def test_find_session_accepts_only_session_ids(sessions_dir, session_id):
    with pytest.raises(SessionFileError, match="not a session id"):
        find_session(sessions_dir, session_id)


def test_find_session_only_looks_in_the_dated_folders(sessions_dir):
    session_id = str(uuid.uuid4())
    write_session(sessions_dir / f"session-2026-05-06T07-08-09-{session_id}.jsonl", meta={"id": session_id})
    write_session(sessions_dir / "extra" / "2026" / "05" / "06" / f"session-x-{session_id}.jsonl")
    with pytest.raises(SessionFileError):
        find_session(sessions_dir, session_id)
    with pytest.raises(SessionFileError, match="no recorded sessions"):
        find_session(sessions_dir)


def test_newest_session_is_the_one_written_last_and_skips_subagents(sessions_dir):
    early = datetime(2026, 1, 1, tzinfo=timezone.utc)
    late = datetime(2026, 2, 1, tzinfo=timezone.utc)
    _, older = new_file(sessions_dir, started=late, mtime=1_000)
    newest_id, newest = new_file(sessions_dir, started=early, mtime=2_000)
    new_file(sessions_dir, started=late, mtime=3_000, parent=newest_id)
    assert find_session(sessions_dir) == newest
    os.utime(older, (4_000, 4_000))
    assert find_session(sessions_dir) == older


def test_the_newest_session_is_taken_from_the_current_directory(sessions_dir, tmp_path):
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _, here = new_file(sessions_dir, started=started, mtime=1_000, cwd=str(tmp_path / "here"))
    new_file(sessions_dir, started=started, mtime=2_000, cwd=str(tmp_path / "there"))
    assert find_session(sessions_dir, cwd=tmp_path / "here") == here
    with pytest.raises(SessionFileError, match="no recorded sessions"):
        find_session(sessions_dir, cwd=tmp_path / "elsewhere")


@pytest.mark.parametrize("recorded", ["../../x", "s1", str(uuid.uuid4()), 7, None])
def test_the_recorded_id_must_be_the_id_in_the_file_name(home, sessions_dir, recorded):
    session_id = str(uuid.uuid4())
    path = session_file(sessions_dir, session_id, datetime(2026, 1, 1, tzinfo=timezone.utc))
    write_session(path, meta={"id": recorded})
    with pytest.raises(ConfigError, match="no valid session id"):
        open_initial_history(StartMode.RESUMED, session_id, home=home)


def test_open_initial_history_errors_are_config_errors(home, sessions_dir):
    with pytest.raises(ConfigError, match="no recorded sessions"):
        open_initial_history(StartMode.RESUMED, None, home=home)
    parent = str(uuid.uuid4())
    child, _ = new_file(sessions_dir, started=datetime(2026, 1, 1, tzinfo=timezone.utc), mtime=1_000, parent=parent)
    with pytest.raises(ConfigError, match=f"subagent.*{parent}"):
        open_initial_history(StartMode.FORKED, child, home=home)


# --- resume -----------------------------------------------------------------------------------


def test_resumed_session_continues_the_conversation_in_the_same_file(make_session, sessions_dir):
    first = make_session()
    run(first, "list the files", ModelResponse(content="a.py b.py"))
    path, session_id = first.recorder.path, first.id
    first.close()

    resumed = make_session(mode=StartMode.RESUMED, session_id=session_id)
    assert resumed.id == session_id
    assert resumed.recorder.path == path
    assert conversation(resumed.history) == [("message", "list the files"), ("message", "a.py b.py")]
    client = run(resumed, "now count them")
    sent = [(m["role"], m["content"]) for m in client.calls[0][1:]]
    assert ("user", "list the files") in sent and ("assistant", "a.py b.py") in sent
    assert sent[-1] == ("user", "now count them")
    resumed.close()

    lines = records(path)
    assert [r["type"] for r in lines].count("session_meta") == 1
    assert [r["type"] for r in lines].count("resumed") == 1


def test_session_start_hooks_get_the_resume_source_for_resumed_and_forked_sessions(make_session, monkeypatch):
    first = make_session()
    first.close()
    sources: list[str] = []
    monkeypatch.setattr(
        HookRunner, "session_start", lambda self, source="startup": sources.append(source) or HookOutcome()
    )
    for mode in (StartMode.RESUMED, StartMode.FORKED):
        make_session(mode=mode, session_id=first.id).close()
    make_session().close()
    assert sources == ["resume", "resume", "startup"]


def test_resumed_session_uses_the_current_config(make_session):
    first = make_session(("sandbox_mode", "read-only"), ("model", "model-a"))
    run(first, "hello")
    first.close()

    resumed = make_session(("sandbox_mode", "workspace-write"), ("model", "model-b"), mode=StartMode.RESUMED, session_id=first.id)
    assert resumed.policy.mode.value == "workspace-write"
    assert resumed.config.model == "model-b"
    event = next(r for r in records(resumed.recorder.path) if r["type"] == "resumed")
    assert event["config"] == resumed.config.summary()
    assert event["config"]["sandbox_mode"] == "workspace-write"
    environment = resumed.history.last_of_kind(Kind.ENVIRONMENT)
    assert "<sandbox_mode>workspace-write</sandbox_mode>" in environment.content
    assert [item.kind for item in resumed.history].count(Kind.ENVIRONMENT) == 1


def test_resumed_session_rebuilds_context_items_from_current_files(make_session, work):
    (work / "AGENTS.md").write_text("old rules\n", encoding="utf-8")
    first = make_session()
    run(first, "hello")
    first.plan.add("step one")
    first.plan.sync()
    first.close()

    (work / "AGENTS.md").write_text("new rules\n", encoding="utf-8")
    resumed = make_session(mode=StartMode.RESUMED, session_id=first.id)
    agents_md = [item.content for item in resumed.history if item.kind == Kind.AGENTS_MD]
    assert len(agents_md) == 1 and "new rules" in agents_md[0] and "old rules" not in agents_md[0]
    assert resumed.history.last_of_kind(Kind.PLAN) is None
    assert resumed.plan.steps == []


def test_resumed_session_answers_tool_calls_left_without_a_result(make_session, sessions_dir):
    session_id = str(uuid.uuid4())
    path = session_file(sessions_dir, session_id, datetime(2026, 1, 1, tzinfo=timezone.utc))
    write_session(
        path,
        item_line(Item.user("run the tests")),
        item_line(Item.tool_call("c1", "shell", '{"command": "pytest"}', "r1")),
        meta={"id": session_id},
    )
    resumed = make_session(mode=StartMode.RESUMED, session_id=session_id)
    result = next(item for item in resumed.history if item.kind == Kind.TOOL_RESULT)
    assert result.call_id == "c1"
    kinds = [item.kind for item in resumed.history]
    assert kinds.index(Kind.TOOL_RESULT) == kinds.index(Kind.TOOL_CALL) + 1
    client = run(resumed, "again")
    assert client.calls[0][3]["role"] == "tool"


def test_session_resumed_twice_keeps_plan_updates_in_order(make_session):
    first = make_session()
    run(first, "one")
    first.close()

    second = make_session(mode=StartMode.RESUMED, session_id=first.id)
    second.plan.add("a")
    second.plan.sync()
    second.plan.set_status(1, StepStatus.COMPLETED)
    second.plan.sync()
    run(second, "two")
    second.close()

    third = make_session(mode=StartMode.RESUMED, session_id=first.id)
    assert conversation(third.history) == [("message", "one"), ("message", "done"), ("message", "two"), ("message", "done")]


# --- fork -------------------------------------------------------------------------------------


def test_fork_writes_a_new_session_with_the_copied_conversation(make_session):
    first = make_session()
    run(first, "explain the parser", ModelResponse(content="It reads tokens."))
    first.close()
    original = first.recorder.path.read_text(encoding="utf-8")

    fork = make_session(mode=StartMode.FORKED, session_id=first.id)
    assert fork.id != first.id
    assert fork.recorder.path != first.recorder.path
    assert conversation(fork.history) == [("message", "explain the parser"), ("message", "It reads tokens.")]
    run(fork, "rewrite it")
    fork.close()

    assert first.recorder.path.read_text(encoding="utf-8") == original
    meta, items = load_session(fork.recorder.path)
    assert meta["id"] == fork.id and meta["forked_from_id"] == first.id
    assert meta["config"] == fork.config.summary()
    assert conversation(items)[:3] == [
        ("message", "explain the parser"),
        ("message", "It reads tokens."),
        ("message", "rewrite it"),
    ]
    resumed = make_session(mode=StartMode.RESUMED, session_id=fork.id)
    assert conversation(resumed.history)[:2] == [("message", "explain the parser"), ("message", "It reads tokens.")]


# --- rollback ---------------------------------------------------------------------------------


def test_rollback_drops_the_last_turns_and_records_an_event(make_session):
    session = make_session()
    run(session, "one")
    run(
        session,
        "two",
        ModelResponse(content=None, tool_calls=[ToolCall("c1", "no_such_tool", "{}")]),
        ModelResponse(content="tried it"),
    )
    assert rollback(session, 1) == 4
    assert conversation(session.history) == [("message", "one"), ("message", "done")]
    assert records(session.recorder.path)[-1]["type"] == "rolled_back"
    assert records(session.recorder.path)[-1]["num_turns"] == 1
    client = run(session, "three")
    assert [m["content"] for m in client.calls[0] if m["role"] == "user" and m["content"] in ("one", "two", "three")] == [
        "one",
        "three",
    ]
    session.close()
    resumed = make_session(mode=StartMode.RESUMED, session_id=session.id)
    assert [content for _, content in conversation(resumed.history)] == ["one", "done", "three", "done"]


def test_rollback_keeps_the_plan_in_the_history(make_session):
    session = make_session()
    run(session, "one")
    run(session, "two")
    session.plan.add("step")
    session.plan.sync()
    rollback(session, 1)
    plan = session.history.last_of_kind(Kind.PLAN)
    assert plan is not None and "step" in plan.content


def test_rollback_command(make_session):
    session = make_session()
    run(session, "one")
    run(session, "two")
    assert "rolled back 1 turn" in run_rollback_command(session, "1")
    assert "usage" in run_rollback_command(session, "")
    assert "usage" in run_rollback_command(session, "two")
    assert "has 1 user turn" in run_rollback_command(session, "5")
    assert conversation(session.history) == [("message", "one"), ("message", "done")]
    assert ROLLBACK_COMMAND == "/rollback"


# --- command line -----------------------------------------------------------------------------


@pytest.fixture
def started(monkeypatch):
    calls: list[tuple[str, object, StartMode | None]] = []
    for name in ("interactive", "exec"):
        record = lambda args, name=name: calls.append((name, args, getattr(args, "start_mode", None))) or 0
        monkeypatch.setattr(cli, f"run_{name}", record)
    return calls


@pytest.mark.parametrize(
    ("argv", "runner", "mode", "session_id", "prompt"),
    [
        (["resume", "--last"], "interactive", StartMode.RESUMED, None, None),
        (["resume", "--last", "go on"], "interactive", StartMode.RESUMED, None, "go on"),
        (["resume", "abc", "go on"], "interactive", StartMode.RESUMED, "abc", "go on"),
        (["fork", "-m", "x", "abc"], "interactive", StartMode.FORKED, "abc", None),
        (["fork", "--last"], "interactive", StartMode.FORKED, None, None),
        (["exec", "resume", "--last", "go on"], "exec", StartMode.RESUMED, None, "go on"),
        (["exec", "resume", "abc", "-"], "exec", StartMode.RESUMED, "abc", "-"),
    ],
)
def test_session_commands(started, argv, runner, mode, session_id, prompt):
    assert cli.main(argv) == 0
    [(name, args, given)] = started
    assert (name, given, args.session_id, args.prompt) == (runner, mode, session_id, prompt)


@pytest.mark.parametrize(
    "argv",
    [["resume"], ["fork"], ["resume", "--last", "abc", "go on"], ["exec", "resume", "abc"], ["exec", "resume", "--last"]],
)
def test_session_commands_need_one_session(started, argv):
    with pytest.raises(SystemExit):
        cli.main(argv)
    assert started == []


def test_exec_prompt_named_resume_is_still_a_prompt(started):
    assert cli.main(["exec", "resume this"]) == 0
    [(name, args, mode)] = started
    assert (name, args.prompt, mode) == ("exec", "resume this", None)


def test_resume_with_no_sessions_is_a_config_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["exec", "resume", "--last", "go on"]) == cli.EXIT_CONFIG
    assert "no recorded sessions" in capsys.readouterr().err


def test_interactive_rollback_command(make_session, monkeypatch):
    session = make_session()
    run(session, "one")
    run(session, "two")
    stdout = io.StringIO()
    ui = TerminalUI(interactive=True, stdin=io.StringIO("/rollback 1\n/exit\n"), stdout=stdout, stderr=stdout)
    session.ui = ui
    monkeypatch.setattr(cli, "_start_session", lambda args, ui: session)
    monkeypatch.setattr(cli, "TerminalUI", lambda interactive: ui)
    args = cli.build_parsers()[0].parse_args([])
    assert cli.run_interactive(args) == cli.EXIT_OK
    assert "rolled back 1 turn" in stdout.getvalue()
    assert conversation(session.history) == [("message", "one"), ("message", "done")]
