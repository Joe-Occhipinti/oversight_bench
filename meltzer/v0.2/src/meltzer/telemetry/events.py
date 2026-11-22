"""Session telemetry: events about model requests, tool decisions, tool calls and commands.

Events carry names, durations, decisions and exit codes; never prompts, tool
arguments, tool output or file contents. Each event is written as a line to
the `meltzer.telemetry` logger and, when an exporter is set up, queued for
export. Subagents record their events in their top-level session's telemetry.
"""

from __future__ import annotations

import json
import logging
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterator

from meltzer.telemetry.feedback import DEFAULT_MAX_BYTES, RingBufferHandler
from meltzer.telemetry.otlp import OtlpExportError, OtlpLogExporter, parse_exporter

if TYPE_CHECKING:
    from meltzer.config.schema import Config

logger = logging.getLogger("meltzer.telemetry")

# Where a tool decision came from.
SOURCE_CONFIG = "config"
SOURCE_USER = "user"
SOURCE_AUTOMATED_REVIEWER = "automated_reviewer"

TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
PLAIN_VALUE = re.compile(r"[A-Za-z0-9_.:/-]*")
UNKNOWN_TOOL = "unknown"


@dataclass(frozen=True)
class TelemetryEvent:
    name: str
    attributes: dict[str, str | int | bool]
    time_unix_nano: int = field(default_factory=time.time_ns)

    def log_line(self) -> str:
        """The event as one line: the name and key=value pairs; values with other characters are quoted."""
        values = {key: str(value) for key, value in self.attributes.items()}
        return " ".join(
            [self.name, *(f"{key}={v if PLAIN_VALUE.fullmatch(v) else json.dumps(v)}" for key, v in values.items())]
        )


def _tool_name(name: str) -> str:
    return name if TOOL_NAME.fullmatch(name) else UNKNOWN_TOOL


def _ms(seconds: float) -> int:
    return max(0, round(seconds * 1000))


class SessionTelemetry:
    def __init__(
        self,
        session_id: str,
        *,
        exporter: OtlpLogExporter | None = None,
        log_buffer: RingBufferHandler | None = None,
        notice: Callable[[str], None],
    ):
        self.session_id = session_id
        self.exporter = exporter
        # Recent log lines for /feedback; attached to the `meltzer` logger while the session runs.
        self.log_buffer = log_buffer
        self._notice = notice

    def _emit(self, name: str, **attributes: Any) -> None:
        event = TelemetryEvent(f"meltzer.{name}", {"session.id": self.session_id, **attributes})
        logger.info(event.log_line())
        if self.exporter is not None:
            self.exporter.add(event)

    def conversation_start(self, *, model: str, approval_policy: str, reviewer: str, sandbox_mode: str) -> None:
        if self.log_buffer is not None:
            logging.getLogger("meltzer").setLevel(logging.INFO)
            logging.getLogger("meltzer").addHandler(self.log_buffer)
        attributes = {"approval_policy": approval_policy, "reviewer": reviewer, "sandbox_mode": sandbox_mode}
        self._emit("conversation_starts", model=model, **attributes)

    @contextmanager
    def api_request(self, model: str) -> Iterator[None]:
        """Record one model request: the duration of the block, and "error" if it raises."""
        started = time.monotonic()
        try:
            yield
        except BaseException:
            self._emit("api_request", model=model, duration_ms=_ms(time.monotonic() - started), status="error")
            raise
        self._emit("api_request", model=model, duration_ms=_ms(time.monotonic() - started), status="ok")

    def tool_decision(self, tool: str, *, approved: bool, source: str, for_session: bool = False) -> None:
        decision = "denied" if not approved else "approved_for_session" if for_session else "approved"
        self._emit("tool_decision", tool_name=_tool_name(tool), decision=decision, source=source)

    def tool_result(self, tool: str, *, success: bool, duration: float) -> None:
        self._emit("tool_result", tool_name=_tool_name(tool), success=success, duration_ms=_ms(duration))

    def sandbox_outcome(self, *, sandboxed: bool, exit_code: int, duration: float) -> None:
        self._emit("sandbox_outcome", sandboxed=sandboxed, exit_code=exit_code, duration_ms=_ms(duration))

    def flush(self) -> None:
        """Send the queued events. A failed export is reported as a notice and turns the export off."""
        if self.exporter is None:
            return
        try:
            self.exporter.flush()
        except OtlpExportError as exc:
            self._notice(f"warning: telemetry export failed, export is off for the rest of the session: {exc}")
            self.exporter.close()
            self.exporter = None

    def close(self) -> None:
        self.flush()
        if self.exporter is not None:
            self.exporter.close()
        if self.log_buffer is not None:
            logging.getLogger("meltzer").removeHandler(self.log_buffer)


def session_telemetry(config: Config, session_id: str, notice: Callable[[str], None]) -> SessionTelemetry:
    """Telemetry of a top-level session. Raises ConfigError if `[otel]` is not valid.

    Events are exported only when `analytics.enabled` is true and `otel.exporter`
    names an exporter; recent log lines are kept only when `feedback.enabled` is true.
    """
    settings = parse_exporter(config.otel)
    exporter = OtlpLogExporter(settings) if settings is not None and config.analytics_enabled else None
    log_buffer = RingBufferHandler(DEFAULT_MAX_BYTES) if config.feedback_enabled else None
    return SessionTelemetry(session_id, exporter=exporter, log_buffer=log_buffer, notice=notice)
