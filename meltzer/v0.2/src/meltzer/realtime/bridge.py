"""Voice conversations: a realtime model talks with the user and hands requests to the agent.

The realtime model has one function, `background_agent`. When it calls it, the
request text is submitted to the session as user input with source "voice", on
the same path as a typed prompt; the agent's final message is sent back as the
function's output and the realtime model answers the user. Each voice input
makes one request; later calls of the function are not run.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from meltzer.config.loader import ConfigError
from meltzer.realtime.client import (
    DEFAULT_MODEL,
    DEFAULT_VOICE,
    ERROR,
    HANDOFF,
    RESPONSE_DONE,
    TEXT,
    TRANSCRIPT,
    RealtimeClient,
    RealtimeError,
    RealtimeEvent,
    realtime_url,
)
from meltzer.realtime.context import startup_context

if TYPE_CHECKING:
    from meltzer.session import Session

VOICE_SOURCE = "voice"

VOICE_INSTRUCTIONS = """\
You are the voice interface of meltzer, a coding agent working in the user's workspace.
Answer short questions yourself. Send every request about the code, files or commands to
the agent with the background_agent function, in the user's own words, then tell the user
what the agent answered. Keep spoken answers brief."""


class VoiceBridge:
    def __init__(self, session: Session, client: RealtimeClient, ui: Any, *, voice: str = DEFAULT_VOICE):
        self.session = session
        self.client = client
        self.ui = ui
        self.voice = voice

    def run(self, pcm: bytes) -> str:
        """Send the audio and run handoffs until the realtime model answers without one; returns its last text."""
        self.client.update_session(VOICE_INSTRUCTIONS + "\n\n" + startup_context(self.session.root), self.voice)
        self.client.append_audio(pcm)
        self.client.commit_audio()
        self.client.create_response()
        reply = ""
        handoffs: list[RealtimeEvent] = []
        handed_off = False
        while True:
            event = self.client.next_event()
            if event.kind == TRANSCRIPT:
                self.ui.info(f"voice input: {event.text}")
            elif event.kind == TEXT:
                reply = event.text
            elif event.kind == HANDOFF:
                handoffs.append(event)
            elif event.kind == ERROR:
                raise RealtimeError(f"realtime server error: {event.text}")
            elif event.kind == RESPONSE_DONE:
                if not handoffs:
                    return reply
                if handed_off:
                    for handoff in handoffs:
                        self._ignore(handoff)
                    return reply
                if reply:
                    self.ui.assistant_message(reply)
                reply = ""
                self._hand_off(handoffs[0])
                handed_off = True
                for handoff in handoffs[1:]:
                    self._ignore(handoff)
                handoffs = []
                self.client.create_response()

    def _hand_off(self, event: RealtimeEvent) -> None:
        if not event.text:
            self.client.send_function_output(event.call_id or "", "The request was empty.")
            return
        self.ui.info(f"voice request: {event.text}")
        final = self.session.run_turn(event.text, source=VOICE_SOURCE)
        self.client.send_function_output(event.call_id or "", final)

    def _ignore(self, event: RealtimeEvent) -> None:
        self.ui.info(f"voice request not run: {event.text}")
        self.client.send_function_output(event.call_id or "", "Not run: one request is handled per voice input.")


def run_voice(session: Session, pcm: bytes, ui: Any) -> str:
    """Hold one voice conversation over the session's realtime settings; returns the realtime model's last text."""
    config = session.config
    url = realtime_url(config.realtime_base_url, config.realtime_model or DEFAULT_MODEL)
    api_key = os.environ.get(config.api_key_env)
    if not api_key:
        raise ConfigError(f"environment variable {config.api_key_env} is not set")
    client = RealtimeClient(url, api_key)
    client.connect()
    try:
        return VoiceBridge(session, client, ui, voice=config.realtime_voice or DEFAULT_VOICE).run(pcm)
    finally:
        client.close()
