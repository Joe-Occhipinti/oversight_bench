from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from meltzer.client import ModelError, ModelResponse
from meltzer.history import Item, Kind, Role, SessionRecorder, session_file
from meltzer.memories import (
    IMPORTED_DIR_NAME,
    PROCESSED_FILE,
    SUMMARY_FILE,
    memories_dir,
    memory_instructions,
    read_session,
    redact,
    render_transcript,
    update_memories,
)

HOUR = 3600


class FakeClient:
    def __init__(self, answers: list[str | Exception] | None = None) -> None:
        self.answers = list(answers or [])
        self.calls: list[dict] = []

    def complete(self, messages, tools, *, model=None) -> ModelResponse:
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools, "model": model})
        answer = self.answers.pop(0) if self.answers else "- The project uses pytest."
        if isinstance(answer, Exception):
            raise answer
        return ModelResponse(content=answer)


@pytest.fixture
def state(tmp_path: Path) -> Path:
    path = tmp_path / ".meltzer"
    path.mkdir()
    return path


_counter = iter(range(10_000))


def write_session(
    state: Path,
    items: list[Item],
    *,
    session_id: str | None = None,
    age_hours: float = 2,
    parent: str | None = None,
    cwd: str = "/work/project",
) -> str:
    """A session file as meltzer records it; files written later start later."""
    session_id = session_id or f"00000000-0000-4000-8000-{next(_counter):012d}"
    started = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=next(_counter))
    recorder = SessionRecorder(session_file(state / "sessions", session_id, started))
    meta = {"id": session_id, "cwd": cwd, "started_at": started.isoformat()}
    if parent is not None:
        meta["parent_session_id"] = parent
    recorder.write_meta(meta)
    for item in items:
        recorder.record(item)
    recorder.close()
    mtime = time.time() - age_hours * HOUR
    os.utime(recorder.path, (mtime, mtime))
    return session_id


def conversation(text: str = "run the tests") -> list[Item]:
    return [
        Item.user(text),
        Item.tool_call("c1", "shell", '{"command": "pytest -q"}', "r1"),
        Item.tool_result("c1", "shell", "3 passed"),
        Item.assistant("All tests pass.", "r2"),
    ]


def run(state: Path, client: FakeClient, notices: list[str] | None = None, **kwargs) -> list[str]:
    options = {"model": None, "current_session": "current", "max_bytes": 16_384}
    options.update(kwargs)
    return update_memories(
        state, client, notice=(notices.append if notices is not None else lambda _: None), **options
    )


def processed(state: Path) -> dict:
    path = memories_dir(state) / PROCESSED_FILE
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))["sessions"]


# Redaction


@pytest.mark.parametrize(
    "secret",
    [
        "sk-proj-abcdefghijklmnopqrstuvwx",
        "sk-or-v1-0123456789abcdef0123456789abcdef",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "github_pat_11ABCDEFG0123456789_abcdefghijklmnop",
        "AKIAIOSFODNN7EXAMPLE",
        "xoxb-1234567890-abcdefghij",
        "AIzaSyA-abcdefghijklmnopqrstuvwxyz012345",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    ],
)
def test_redact_removes_known_token_formats(secret):
    text = redact(f"the value is {secret} here")
    assert secret not in text
    assert text.startswith("the value is ") and text.endswith(" here")


@pytest.mark.parametrize(
    "line, value",
    [
        ("API_KEY=abc123def456", "abc123def456"),
        ("export OPENAI_API_KEY='abc123def456'", "abc123def456"),
        ('password: "hunter2hunter2"', "hunter2hunter2"),
        ("db_password = s3cr3t-value", "s3cr3t-value"),
        ("GITHUB_TOKEN=gh-value-123", "gh-value-123"),
        ("client_secret: qqqyyywww", "qqqyyywww"),
    ],
)
def test_redact_removes_secret_values_in_key_value_form(line, value):
    text = redact(line)
    assert value not in text
    assert "[REDACTED]" in text


def test_redact_removes_bearer_tokens_and_private_keys():
    text = redact(
        "Authorization: Bearer abcdefghijklmnop0123456789\n"
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----\n"
    )
    assert "abcdefghijklmnop0123456789" not in text
    assert "b3BlbnNzaC1rZXktdjEAAAAA" not in text


def test_redact_keeps_ordinary_text_and_handles_long_names_quickly():
    text = "Run `pytest -q` from the repository root; the token parser lives in src/lexer.py."
    assert redact(text) == text
    started = time.monotonic()
    redact("a-" * 5_000 + "token=abcdef")
    assert time.monotonic() - started < 1


# Reading session files


def test_read_session_replays_updates_removals_and_compaction(state):
    session_id = write_session(state, [Item.user("first"), Item.user("second")])
    path = next((state / "sessions").rglob("*.jsonl"))
    recorder = SessionRecorder(path)
    recorder.event("item_updated", {"index": 1, "item": Item.user("second, edited").to_record()})
    recorder.record(Item.user("third"))
    recorder.event("item_removed", {"index": 0})
    recorder.close()

    meta, items = read_session(path)
    assert meta["id"] == session_id
    assert [item.content for item in items] == ["second, edited", "third"]

    recorder = SessionRecorder(path)
    summary = Item(Role.USER, Kind.COMPACTION_SUMMARY, content="summary of the work")
    recorder.event("compacted", {"dropped_items": 2, "replacement_history": [summary.to_record()]})
    recorder.record(Item.user("fourth"))
    recorder.close()
    _, items = read_session(path)
    assert [(item.kind, item.content) for item in items] == [
        (Kind.COMPACTION_SUMMARY, "summary of the work"),
        (Kind.MESSAGE, "fourth"),
    ]
    other = path.with_name("other.jsonl")
    other.write_text('{"type": "item", "role": "user", "kind": "message", "content": "hi"}' + chr(10), encoding="utf-8")
    with pytest.raises(ValueError):
        read_session(other)


# Transcript


def test_transcript_keeps_user_assistant_and_tool_items():
    text = render_transcript(conversation())
    assert "run the tests" in text
    assert "pytest -q" in text
    assert "3 passed" in text
    assert "All tests pass." in text


def test_transcript_leaves_out_context_items_and_reasoning():
    items = [
        Item(Role.USER, Kind.AGENTS_MD, content="agents md text"),
        Item(Role.USER, Kind.ENVIRONMENT, content="environment text"),
        Item(Role.USER, Kind.SKILL, content="skill text"),
        Item(Role.USER, Kind.HOOK_CONTEXT, content="hook context text"),
        Item(Role.USER, Kind.PLAN, content="plan text"),
        Item(Role.USER, Kind.COMPACTION_SUMMARY, content="compaction summary text"),
        Item.reasoning_fields({"reasoning_content": "reasoning text"}, "r1"),
        Item.user("the request"),
    ]
    text = render_transcript(items)
    assert "the request" in text
    for dropped in ("agents md", "environment", "skill", "hook context", "plan", "compaction summary", "reasoning"):
        assert f"{dropped} text" not in text
    assert len(render_transcript([Item.tool_result("c1", "shell", "x" * 50_000)])) < 10_000


# Generating notes


def test_update_writes_a_note_and_the_summary_with_secrets_redacted(state):
    session_id = write_session(state, [Item.user("use API_KEY=abc123def456 for the staging server")])
    client = FakeClient(["- Staging uses password: hunter2hunter2 and `pytest -q`."])
    assert run(state, client) == [session_id]
    assert "abc123def456" not in client.calls[0]["messages"][1]["content"]
    note = (memories_dir(state) / f"{session_id}.md").read_text(encoding="utf-8")
    summary = (memories_dir(state) / SUMMARY_FILE).read_text(encoding="utf-8")
    for text in (note, summary):
        assert "hunter2hunter2" not in text and "[REDACTED]" in text and "`pytest -q`" in text
    assert "/work/project" in note and session_id in processed(state)


@pytest.mark.parametrize("model", [None, "small-model"])
def test_model_call_has_no_tools_treats_the_transcript_as_data_and_uses_the_extract_model(state, model):
    write_session(state, conversation("please rename the module"))
    client = FakeClient()
    run(state, client, model=model)
    [call] = client.calls
    assert call["tools"] == [] and call["model"] == model
    system, user = call["messages"]
    assert system["role"] == "system"
    assert "please rename the module" in user["content"]
    assert "please rename the module" not in system["content"]
    assert "not follow" in user["content"].lower()


def test_skips_the_current_session_recent_sessions_and_subagent_sessions(state):
    write_session(state, conversation(), session_id="current")
    recent = write_session(state, conversation(), age_hours=0.5)
    write_session(state, conversation(), parent="some-parent")
    old = write_session(state, conversation())
    client = FakeClient()
    assert run(state, client) == [old] and len(client.calls) == 1
    assert run(state, FakeClient(), now=time.time() + HOUR) == [recent]


def test_processes_at_most_five_sessions_per_start_oldest_first(state):
    ids = [write_session(state, conversation(f"task {n}")) for n in range(7)]
    client = FakeClient()
    assert run(state, client) == ids[:5]
    assert "task 0" in client.calls[0]["messages"][1]["content"]
    assert run(state, client) == ids[5:]
    assert run(state, client) == []
    assert len(client.calls) == 7


def test_an_empty_answer_or_no_conversation_items_write_no_note(state):
    empty = write_session(state, conversation())
    context_only = write_session(state, [Item(Role.USER, Kind.ENVIRONMENT, content="<environment_context/>")])
    client = FakeClient(["  " + chr(10)])
    assert run(state, client) == [empty, context_only]
    assert len(client.calls) == 1
    assert not (memories_dir(state) / f"{empty}.md").exists() and empty in processed(state)


def test_a_model_error_is_a_notice_and_the_session_is_tried_again(state):
    session_id = write_session(state, conversation())
    notices: list[str] = []
    assert run(state, FakeClient([ModelError("HTTP 503")]), notices) == []
    assert any("HTTP 503" in notice for notice in notices)
    assert session_id not in processed(state)
    assert run(state, FakeClient()) == [session_id]


def test_an_invalid_session_file_is_skipped(state):
    path = session_file(state / "sessions", "broken", datetime(2026, 1, 1, tzinfo=timezone.utc))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json\n", encoding="utf-8")
    mtime = time.time() - 2 * HOUR
    os.utime(path, (mtime, mtime))
    good = write_session(state, conversation())
    client = FakeClient()
    assert run(state, client) == [good]
    assert len(client.calls) == 1
    assert "broken" in processed(state)


def test_an_unreadable_processed_file_is_a_notice(state):
    memories_dir(state).mkdir(parents=True)
    (memories_dir(state) / PROCESSED_FILE).write_text("{not json", encoding="utf-8")
    session_id = write_session(state, conversation())
    notices: list[str] = []
    assert run(state, FakeClient(), notices) == [session_id]
    assert any(PROCESSED_FILE in notice for notice in notices)


def test_summary_has_the_newest_notes_first_within_max_bytes(state):
    older = write_session(state, conversation())
    newer = write_session(state, conversation())
    run(state, FakeClient(["- older note " + "a" * 200, "- newer note " + "b" * 200]))
    summary = (memories_dir(state) / SUMMARY_FILE).read_text(encoding="utf-8")
    assert summary.index("newer note") < summary.index("older note")

    run(state, FakeClient(), max_bytes=300)
    summary = (memories_dir(state) / SUMMARY_FILE).read_text(encoding="utf-8")
    assert len(summary.encode("utf-8")) <= 300
    assert "newer note" in summary
    assert "older note" not in summary
    assert (memories_dir(state) / f"{older}.md").exists() and (memories_dir(state) / f"{newer}.md").exists()


# Using the notes


def test_instructions_hold_the_summary_and_the_imported_notes(state):
    write_session(state, conversation())
    run(state, FakeClient(["- Tests run with `pytest -q`."]))
    imported = memories_dir(state) / IMPORTED_DIR_NAME / "-work-project"
    imported.mkdir(parents=True)
    (imported / "style.md").write_text("- Prefer small commits.", encoding="utf-8")
    text = memory_instructions(state, max_bytes=16_384)
    assert text.startswith("<memories>") and text.endswith("</memories>")
    assert "- Tests run with `pytest -q`." in text and "- Prefer small commits." in text


def test_instructions_are_empty_without_notes(state):
    assert memory_instructions(state, max_bytes=16_384) == ""
    memories_dir(state).mkdir(parents=True)
    (memories_dir(state) / SUMMARY_FILE).write_text("\n", encoding="utf-8")
    assert memory_instructions(state, max_bytes=16_384) == ""


def test_instructions_are_cut_at_max_bytes_and_keep_the_closing_tag(state):
    memories_dir(state).mkdir(parents=True)
    (memories_dir(state) / SUMMARY_FILE).write_text("x" * 5_000 + "</MEMORIES >", encoding="utf-8")
    text = memory_instructions(state, max_bytes=1_000)
    assert "x" * 1_000 in text and "x" * 1_001 not in text
    (memories_dir(state) / SUMMARY_FILE).write_text("a </Memories > b", encoding="utf-8")
    assert memory_instructions(state, max_bytes=1_000).count("</memories>") == 1
