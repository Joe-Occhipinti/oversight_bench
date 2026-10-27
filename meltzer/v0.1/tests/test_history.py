from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from meltzer.history import History, Item, Kind, Role, SessionRecorder, session_file


def test_items_of_one_response_form_one_assistant_message():
    history = History()
    history.append(Item.user("fix the bug"))
    history.append(Item.reasoning_fields({"reasoning_content": "thinking"}, "r1"))
    history.append(Item.assistant("Looking at it.", "r1"))
    history.append(Item.tool_call("c1", "shell", '{"command": "ls"}', "r1"))
    history.append(Item.tool_call("c2", "shell", "", "r1"))
    history.append(Item.tool_result("c1", "shell", "a.py"))
    history.append(Item.tool_result("c2", "shell", "b.py"))
    history.append(Item.assistant("Done.", "r2"))

    assert history.to_messages() == [
        {"role": "user", "content": "fix the bug"},
        {
            "role": "assistant",
            "content": "Looking at it.",
            "reasoning_content": "thinking",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "shell", "arguments": '{"command": "ls"}'}},
                {"id": "c2", "type": "function", "function": {"name": "shell", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "a.py"},
        {"role": "tool", "tool_call_id": "c2", "content": "b.py"},
        {"role": "assistant", "content": "Done."},
    ]


def test_consecutive_responses_stay_separate():
    history = History()
    history.append(Item.assistant("one", "r1"))
    history.append(Item.assistant("two", "r2"))
    assert [m["content"] for m in history.to_messages()] == ["one", "two"]


@pytest.mark.parametrize("kind", [Kind.MESSAGE, Kind.AGENTS_MD, Kind.PLAN, Kind.COMPACTION_SUMMARY])
def test_messages_follow_item_roles(kind):
    history = History()
    history.append(Item(Role.USER, kind, content="text"))
    history.append(Item.assistant("reply", "r1"))
    assert history.to_messages() == [{"role": "user", "content": "text"}, {"role": "assistant", "content": "reply"}]


def test_history_lookup_remove_and_replace():
    history = History()
    first, second = Item.user("a"), Item.user("b")
    history.append(first)
    history.append(second)
    assert len(history) == 2
    assert history.last_of_kind(Kind.MESSAGE) is second
    assert history.last_of_kind(Kind.PLAN) is None
    assert history.index_of(second) == 1
    assert history.remove(first) == 0
    assert list(history) == [second]
    with pytest.raises(ValueError):
        history.index_of(first)
    history.replace([first])
    assert list(history) == [first]


def test_to_record_drops_empty_fields():
    record = Item.tool_result("c1", "shell", "out").to_record()
    assert record["role"] == "tool"
    assert record["kind"] == "tool_result"
    assert record["call_id"] == "c1"
    assert "arguments" not in record and "reasoning" not in record and "response_id" not in record


def test_session_file_path(tmp_path):
    started = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
    path = session_file(tmp_path, "abc", started)
    assert path == tmp_path / "2026" / "03" / "04" / "session-2026-03-04T05-06-07-abc.jsonl"


def test_recorder_writes_json_lines(tmp_path):
    path = tmp_path / "2026" / "01" / "01" / "s.jsonl"
    recorder = SessionRecorder(path)
    recorder.write_meta({"id": "abc", "cwd": "/w"})
    recorder.record(Item.user("héllo"))
    recorder.event("item_removed", {"index": 0})
    # Each line is flushed as it is written.
    lines = path.read_text(encoding="utf-8").splitlines()
    recorder.close()
    recorder.close()

    records = [json.loads(line) for line in lines]
    assert [r["type"] for r in records] == ["session_meta", "item", "item_removed"]
    assert records[0]["id"] == "abc" and "timestamp" in records[0]
    assert records[1]["content"] == "héllo" and records[1]["role"] == "user"
    assert records[2]["index"] == 0
    assert "héllo" in lines[1]


def test_recorder_appends_to_existing_file(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text('{"type": "session_meta"}\n', encoding="utf-8")
    recorder = SessionRecorder(path)
    recorder.record(Item.user("again"))
    recorder.close()
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2
