from __future__ import annotations

import base64
import json
import sys
import threading
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from meltzer.config.loader import ConfigError
from meltzer.realtime.bridge import VOICE_SOURCE, run_voice
from meltzer.realtime.client import (
    HANDOFF_TOOL_NAME,
    SAMPLE_RATE,
    RealtimeClient,
    RealtimeError,
    parse_event,
    read_wav,
    realtime_url,
)
from meltzer.realtime.context import DIR_ENTRY_LIMIT, startup_context

websockets_server = pytest.importorskip("websockets.sync.server")


def write_wav(path: Path, frames: bytes, *, rate: int = SAMPLE_RATE, channels: int = 1, width: int = 2) -> Path:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(frames)
    return path


def handoff_item(call_id: str, arguments: str, name: str = HANDOFF_TOOL_NAME) -> dict:
    item = {"id": f"item_{call_id}", "type": "function_call", "name": name, "call_id": call_id, "arguments": arguments}
    return {"type": "conversation.item.done", "item": item}


class FakeRealtimeServer:
    """A local realtime server: records client messages and answers each `response.create` with the next batch."""

    def __init__(self, batches: list[list[dict]]):
        self.batches = list(batches)
        self.received: list[dict] = []
        self.path = ""
        self.authorization: str | None = None
        self.server = websockets_server.serve(self._handle, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.socket.getsockname()[:2]
        return f"ws://{host}:{port}/v1/realtime"

    def _handle(self, connection) -> None:
        self.path = connection.request.path
        self.authorization = connection.request.headers.get("Authorization")
        for raw in connection:
            message = json.loads(raw)
            self.received.append(message)
            if message["type"] == "response.create" and self.batches:
                for event in self.batches.pop(0):
                    connection.send(json.dumps(event))

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)


class FakeSession:
    def __init__(self, root: Path, base_url: str):
        self.root = root
        realtime = {"realtime_base_url": base_url, "realtime_model": "test-realtime", "realtime_voice": "cedar"}
        self.config = SimpleNamespace(api_key_env="TEST_KEY", **realtime)
        self.turns: list[tuple[str, str | None]] = []

    def run_turn(self, prompt: str, source: str | None = None) -> str:
        self.turns.append((prompt, source))
        return "Tests pass."


def converse(tmp_path: Path, batches: list[list[dict]], pcm: bytes = b"\x00\x00"):
    """Run a voice conversation against a fake server; returns the final text, session, server and UI lines."""
    server = FakeRealtimeServer(batches)
    session, lines = FakeSession(tmp_path, server.url), []
    ui = SimpleNamespace(info=lines.append, assistant_message=lines.append)
    try:
        return run_voice(session, pcm, ui), session, server, lines
    finally:
        server.close()


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test")


def test_read_wav_returns_pcm16_frames(tmp_path):
    frames = bytes(range(200)) * 4
    assert read_wav(write_wav(tmp_path / "in.wav", frames)) == frames


@pytest.mark.parametrize("options", [{"rate": 16_000}, {"channels": 2}, {"width": 1}])
def test_read_wav_rejects_other_formats(tmp_path, options):
    path = write_wav(tmp_path / "in.wav", b"\x00" * 400, **options)
    with pytest.raises(RealtimeError, match="expected 24000 Hz, 16-bit, mono audio"):
        read_wav(path)


def test_read_wav_rejects_missing_empty_and_non_wav_files(tmp_path):
    with pytest.raises(RealtimeError, match="cannot read"):
        read_wav(tmp_path / "missing.wav")
    (tmp_path / "text.wav").write_text("not audio")
    with pytest.raises(RealtimeError, match="not a WAV file"):
        read_wav(tmp_path / "text.wav")
    with pytest.raises(RealtimeError, match="no audio"):
        read_wav(write_wav(tmp_path / "empty.wav", b""))


def test_realtime_url_adds_the_model():
    assert realtime_url(None, "gpt-realtime") == "wss://api.openai.com/v1/realtime?model=gpt-realtime"
    assert realtime_url("ws://localhost:9000/rt?x=1", "m") == "ws://localhost:9000/rt?x=1&model=m"


@pytest.mark.parametrize(
    "base_url", ["https://api.openai.com/v1/realtime", "wss://", "not a url", "ws://api.example.com/rt"]
)
def test_realtime_url_rejects_non_websocket_urls_and_ws_beyond_localhost(base_url):
    with pytest.raises(ConfigError, match="realtime.base_url"):
        realtime_url(base_url, "m")


def test_parse_event_reads_handoffs_text_and_errors():
    event = parse_event(handoff_item("call_1", json.dumps({"prompt": "  run the tests  "})))
    assert (event.kind, event.call_id, event.text) == ("handoff", "call_1", "run the tests")
    assert parse_event(handoff_item("call_2", "run the tests")).text == "run the tests"
    assert parse_event(handoff_item("call_3", "{}", name="remain_silent")) is None
    assert parse_event({"type": "response.output_text.done", "text": "Done."}).text == "Done."
    assert parse_event({"type": "response.output_audio_transcript.done", "transcript": "Hi."}).text == "Hi."
    transcript = parse_event({"type": "conversation.item.input_audio_transcription.completed", "transcript": "hey"})
    assert (transcript.kind, transcript.text) == ("transcript", "hey")
    assert parse_event({"type": "error", "error": {"message": "bad audio"}}).text == "bad audio"
    assert parse_event({"type": "response.done"}).kind == "response_done"
    assert parse_event({"type": "session.updated"}) is None


def test_voice_handoff_is_submitted_as_voice_user_input(tmp_path, api_key):
    first = [
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "run the tests"},
        {"type": "response.output_text.done", "text": "Checking."},
        handoff_item("call_1", json.dumps({"prompt": "run the tests"})),
        {"type": "response.done"},
    ]
    second = [{"type": "response.output_text.done", "text": "The tests pass."}, {"type": "response.done"}]
    pcm = b"\x01\x02" * 30_000
    final, session, server, lines = converse(tmp_path, [first, second], pcm)

    assert final == "The tests pass."
    assert session.turns == [("run the tests", VOICE_SOURCE)]
    assert "Checking." in lines
    assert server.path == "/v1/realtime?model=test-realtime"
    assert server.authorization == "Bearer sk-test"
    types = [message["type"] for message in server.received]
    assert types[0] == "session.update"
    assert types[-3:] == ["response.create", "conversation.item.create", "response.create"]
    session_update = server.received[0]["session"]
    assert [tool["name"] for tool in session_update["tools"]] == [HANDOFF_TOOL_NAME]
    assert session_update["audio"]["output"]["voice"] == "cedar"
    assert str(tmp_path) in session_update["instructions"]
    sent = b"".join(base64.b64decode(m["audio"]) for m in server.received if m["type"] == "input_audio_buffer.append")
    assert sent == pcm
    assert types.index("input_audio_buffer.commit") < types.index("response.create")
    output = server.received[-2]["item"]
    assert output == {"type": "function_call_output", "call_id": "call_1", "output": "Tests pass."}


def test_one_request_is_run_per_voice_input(tmp_path, api_key):
    request = json.dumps({"prompt": "run the tests"})
    first = [handoff_item("call_1", request), handoff_item("call_2", request), {"type": "response.done"}]
    second = [handoff_item("call_3", request), {"type": "response.output_text.done", "text": "Done."}]
    final, session, server, lines = converse(tmp_path, [first, [*second, {"type": "response.done"}]])
    assert final == "Done." and session.turns == [("run the tests", VOICE_SOURCE)]
    outputs = {m["item"]["call_id"]: m["item"]["output"] for m in server.received if m["type"] == "conversation.item.create"}
    assert outputs["call_1"] == "Tests pass." and "Not run" in outputs["call_2"] and "Not run" in outputs["call_3"]
    assert [line for line in lines if "not run" in line] == ["voice request not run: run the tests"] * 2


def test_answer_without_handoff_runs_no_turn(tmp_path, api_key):
    answer = [{"type": "response.output_text.done", "text": "Hello."}, {"type": "response.done"}]
    final, session, _, _ = converse(tmp_path, [answer])
    assert final == "Hello." and session.turns == []


def test_server_error_event_stops_the_conversation(tmp_path, api_key):
    with pytest.raises(RealtimeError, match="invalid audio"):
        converse(tmp_path, [[{"type": "error", "error": {"message": "invalid audio"}}]])


def test_connection_failure_is_a_realtime_error():
    client = RealtimeClient("ws://127.0.0.1:1/v1/realtime", "sk-test", open_timeout=2)
    with pytest.raises(RealtimeError, match="cannot connect"):
        client.connect()


def test_missing_voice_extra_is_a_config_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "websockets.sync.client", None)
    with pytest.raises(ConfigError, match="meltzer\\[voice\\]"):
        RealtimeClient("ws://127.0.0.1:1/", "sk-test").connect()


def test_startup_context_lists_the_workspace_to_depth_two(tmp_path):
    (tmp_path / "src" / "pkg" / "deep").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "deep" / "hidden.py").write_text("")
    (tmp_path / "src" / "main.py").write_text("")
    (tmp_path / "node_modules" / "lib").mkdir(parents=True)
    (tmp_path / ".env").write_text("")
    (tmp_path / "README.md").write_text("")
    text = startup_context(tmp_path)
    assert f"Workspace root: {tmp_path}" in text
    assert "- src/\n  - pkg/\n  - main.py\n- README.md\n" in text
    assert "deep" not in text and "node_modules" not in text and ".env" not in text


def test_startup_context_limits_entries_and_size(tmp_path):
    for number in range(DIR_ENTRY_LIMIT + 5):
        (tmp_path / f"file{number:02d}.txt").write_text("")
    text = startup_context(tmp_path)
    assert "file19.txt" in text and "file20.txt" not in text and "- ... 5 more entries" in text
    short = startup_context(tmp_path, max_chars=len(str(tmp_path)) + 120)
    assert len(short) <= len(str(tmp_path)) + 120 and short.endswith("</startup_context>")
