"""WebSocket client for the OpenAI Realtime API.

The client sends `session.update`, input audio (`input_audio_buffer.append`,
`input_audio_buffer.commit`), `response.create` and function call output
(`conversation.item.create`), and reads the server events meltzer uses as
`RealtimeEvent`s. The `websockets` package (extra `meltzer[voice]`) is imported
when a connection opens.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import wave
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from meltzer import __version__
from meltzer.config.loader import ConfigError

DEFAULT_BASE_URL = "wss://api.openai.com/v1/realtime"
DEFAULT_MODEL = "gpt-realtime"
DEFAULT_VOICE = "marin"
TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"
SAMPLE_RATE = 24_000
# One second of 24 kHz 16-bit mono audio per append event.
AUDIO_CHUNK_BYTES = 48_000
OPEN_TIMEOUT = 30.0
EVENT_TIMEOUT = 120.0

HANDOFF_TOOL_NAME = "background_agent"
HANDOFF_TOOL = {
    "type": "function",
    "name": HANDOFF_TOOL_NAME,
    "description": "Send a user request to the coding agent, in the user's own words; returns its final message.",
    "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]},
}

# Event kinds.
TRANSCRIPT = "transcript"
TEXT = "text"
HANDOFF = "handoff"
RESPONSE_DONE = "response_done"
ERROR = "error"


class RealtimeError(Exception):
    """The input audio is unusable, the connection failed or the server reported an error."""


@dataclass(frozen=True)
class RealtimeEvent:
    kind: str
    text: str = ""
    call_id: str | None = None


def read_wav(path: Path) -> bytes:
    """The frames of a 24 kHz, 16-bit, mono PCM WAV file."""
    try:
        with wave.open(str(path), "rb") as wav:
            if (wav.getsampwidth(), wav.getnchannels(), wav.getframerate()) != (2, 1, SAMPLE_RATE):
                raise RealtimeError(f"{path}: expected {SAMPLE_RATE} Hz, 16-bit, mono audio")
            frames = wav.readframes(wav.getnframes())
    except OSError as exc:
        raise RealtimeError(f"cannot read {path}: {exc.strerror or exc}") from None
    except (wave.Error, EOFError) as exc:
        raise RealtimeError(f"{path} is not a WAV file: {exc}") from None
    if not frames:
        raise RealtimeError(f"{path} has no audio")
    return frames


def realtime_url(base_url: str | None, model: str) -> str:
    """The WebSocket URL for `model`: `base_url` (or the OpenAI endpoint) with `model=` added to the query."""
    url = base_url or DEFAULT_BASE_URL
    parts = urlsplit(url)
    if parts.scheme not in ("ws", "wss") or not parts.hostname:
        raise ConfigError(f"realtime.base_url: expected a ws or wss URL, got {url!r}")
    if parts.scheme == "ws" and not _is_loopback(parts.hostname):
        raise ConfigError(f"realtime.base_url: ws is accepted only for localhost, got {url!r}; use wss")
    query = "&".join(part for part in (parts.query, urlencode({"model": model})) if part)
    return urlunsplit(parts._replace(query=query))


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_event(message: dict[str, Any]) -> RealtimeEvent | None:
    """The event for a server message, or None for messages meltzer does not use."""
    kind = message.get("type")
    if kind == "conversation.item.input_audio_transcription.completed":
        return RealtimeEvent(TRANSCRIPT, _string(message.get("transcript")))
    if kind == "response.output_text.done":
        return RealtimeEvent(TEXT, _string(message.get("text")))
    if kind == "response.output_audio_transcript.done":
        return RealtimeEvent(TEXT, _string(message.get("transcript")))
    if kind == "conversation.item.done":
        item = message.get("item")
        if not isinstance(item, dict) or item.get("type") != "function_call" or item.get("name") != HANDOFF_TOOL_NAME:
            return None
        call_id = item.get("call_id") or item.get("id")
        if not isinstance(call_id, str):
            return None
        return RealtimeEvent(HANDOFF, handoff_text(_string(item.get("arguments"))), call_id=call_id)
    if kind == "response.done":
        return RealtimeEvent(RESPONSE_DONE)
    if kind == "error":
        error = message.get("error")
        text = error.get("message") if isinstance(error, dict) else None
        return RealtimeEvent(ERROR, _string(text) or json.dumps(message)[:500])
    return None


def handoff_text(arguments: str) -> str:
    """The `prompt` argument of a handoff call; the raw arguments when they are not a JSON object with one."""
    try:
        value = json.loads(arguments)
    except ValueError:
        return arguments.strip()
    prompt = value.get("prompt") if isinstance(value, dict) else None
    if isinstance(prompt, str) and prompt.strip():
        return prompt.strip()
    return arguments.strip()


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


class RealtimeClient:
    def __init__(self, url: str, api_key: str, *, open_timeout: float = OPEN_TIMEOUT, timeout: float = EVENT_TIMEOUT):
        self.url = url
        self.api_key = api_key
        self.open_timeout = open_timeout
        self.timeout = timeout
        self._connection: Any = None
        self._stack = ExitStack()

    def connect(self) -> None:
        try:
            from websockets.exceptions import WebSocketException
            from websockets.sync.client import connect
        except ImportError:
            raise ConfigError("voice needs the voice extra: pip install 'meltzer[voice]'") from None
        headers = {"Authorization": f"Bearer {self.api_key}", "User-Agent": f"meltzer/{__version__}"}
        try:
            connection = connect(self.url, additional_headers=headers, open_timeout=self.open_timeout)
            self._connection = self._stack.enter_context(connection)
        except (OSError, TimeoutError, WebSocketException) as exc:
            raise RealtimeError(f"cannot connect to the realtime server: {exc}") from None

    def close(self) -> None:
        self._stack.close()
        self._connection = None

    def update_session(self, instructions: str, voice: str) -> None:
        audio_format = {"type": "audio/pcm", "rate": SAMPLE_RATE}
        session = {
            "type": "realtime",
            "instructions": instructions,
            "output_modalities": ["text"],
            "audio": {
                "input": {
                    "format": audio_format,
                    "transcription": {"model": TRANSCRIPTION_MODEL},
                    "turn_detection": None,
                },
                "output": {"format": audio_format, "voice": voice},
            },
            "tools": [HANDOFF_TOOL],
            "tool_choice": "auto",
        }
        self._send({"type": "session.update", "session": session})

    def append_audio(self, pcm: bytes) -> None:
        for start in range(0, len(pcm), AUDIO_CHUNK_BYTES):
            chunk = base64.b64encode(pcm[start : start + AUDIO_CHUNK_BYTES]).decode("ascii")
            self._send({"type": "input_audio_buffer.append", "audio": chunk})

    def commit_audio(self) -> None:
        self._send({"type": "input_audio_buffer.commit"})

    def create_response(self) -> None:
        self._send({"type": "response.create"})

    def send_function_output(self, call_id: str, output: str) -> None:
        item = {"type": "function_call_output", "call_id": call_id, "output": output}
        self._send({"type": "conversation.item.create", "item": item})

    def next_event(self) -> RealtimeEvent:
        """The next server event meltzer uses; other messages are skipped."""
        from websockets.exceptions import WebSocketException

        while True:
            try:
                raw = self._connection.recv(timeout=self.timeout)
            except TimeoutError:
                raise RealtimeError(f"no event from the realtime server within {self.timeout:g} seconds") from None
            except WebSocketException as exc:
                raise RealtimeError(f"realtime connection closed: {exc}") from None
            try:
                message = json.loads(raw)
            except ValueError:
                raise RealtimeError("the realtime server sent a message that is not JSON") from None
            event = parse_event(message) if isinstance(message, dict) else None
            if event is not None:
                return event

    def _send(self, payload: dict[str, Any]) -> None:
        from websockets.exceptions import WebSocketException

        try:
            self._connection.send(json.dumps(payload))
        except WebSocketException as exc:
            raise RealtimeError(f"realtime connection closed: {exc}") from None
