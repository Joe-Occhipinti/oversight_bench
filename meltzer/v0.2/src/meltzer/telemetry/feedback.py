"""Feedback: recent log lines kept in memory, sent to the meltzer developers on request.

With `[feedback] enabled = true`, an interactive session keeps its recent log
lines (telemetry events and notices) in memory. `/feedback [note]` writes the
report to ~/.meltzer/feedback/ and sends it only after the user confirms.
"""

from __future__ import annotations

import logging
import os
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import httpx

from meltzer import __version__
from meltzer.config.loader import meltzer_home

if TYPE_CHECKING:
    from meltzer.session import Session

FEEDBACK_COMMAND = "/feedback"
FEEDBACK_DIR_NAME = "feedback"
DEFAULT_MAX_BYTES = 4 * 1024 * 1024
REQUEST_TIMEOUT = 30.0


class FeedbackError(Exception):
    """The feedback was not sent."""


class RingBufferHandler(logging.Handler):
    """Keeps the most recent formatted log lines, up to `max_bytes` in total."""

    def __init__(self, max_bytes: int = DEFAULT_MAX_BYTES):
        super().__init__(level=logging.INFO)
        self.max_bytes = max_bytes
        self._lines: deque[str] = deque()
        self._size = 0
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        # Called by Handler.handle() with self.lock held.
        line = self.format(record)
        self._lines.append(line)
        self._size += len(line.encode("utf-8")) + 1
        while self._size > self.max_bytes and self._lines:
            self._size -= len(self._lines.popleft().encode("utf-8")) + 1

    def lines(self) -> list[str]:
        with self.lock:
            return list(self._lines)


def write_report(directory: Path, session_id: str, note: str, logs: str) -> Path:
    """Write the note and the log lines of a feedback report to a new file in directory."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}-{datetime.now():%Y%m%dT%H%M%S%f}.txt"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8") as report:
        report.write(f"meltzer {__version__}, session {session_id}\nnote: {note}\n\n{logs}")
    return path


def upload_feedback(
    endpoint: str,
    consent: bool,
    *,
    session_id: str,
    logs: str,
    note: str = "",
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Send a feedback report. Raises FeedbackError without sending anything if `consent` is false."""
    if not consent:
        raise FeedbackError("feedback is sent only after the user confirms")
    payload = {"meltzer_version": __version__, "session_id": session_id, "note": note, "logs": logs}
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT, transport=transport) as http:
            response = http.post(endpoint, json=payload, headers={"User-Agent": f"meltzer/{__version__}"})
    except httpx.HTTPError as exc:
        raise FeedbackError(f"{endpoint}: {exc}") from exc
    if response.status_code >= 400:
        raise FeedbackError(f"{endpoint} returned HTTP {response.status_code}")


def run_feedback_command(
    session: Session,
    arguments: str,
    confirm: Callable[[str], bool],
    *,
    endpoint: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Apply a `/feedback [note]` command typed by the user and return what to show them."""
    if not session.ui.interactive:
        return "/feedback is available only in interactive sessions"
    buffer = session.telemetry.log_buffer
    # The session keeps log lines only with feedback.enabled.
    if buffer is None:
        return "feedback is off; set [feedback] enabled = true in ~/.meltzer/config.toml to turn it on"
    endpoint = endpoint or session.config.feedback_endpoint
    if endpoint is None:
        return "feedback has no address; set [feedback] endpoint in ~/.meltzer/config.toml"
    lines = buffer.lines()
    logs = "".join(line + "\n" for line in lines)
    note = arguments.strip()
    try:
        report = write_report(meltzer_home(session.home) / FEEDBACK_DIR_NAME, session.id, note, logs)
    except OSError as exc:
        return f"feedback not sent: cannot write the report: {exc.strerror}"
    consent = confirm(f"Send the report in {report} (your note and {len(lines)} log lines) to {endpoint}?")
    if not consent:
        return "feedback not sent"
    try:
        upload_feedback(endpoint, consent, session_id=session.id, logs=logs, note=note, transport=transport)
    except FeedbackError as exc:
        return f"feedback not sent: {exc}"
    return f"feedback sent (session {session.id})"
