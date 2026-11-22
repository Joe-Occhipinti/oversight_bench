from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from meltzer.config.loader import ConfigError
from meltzer.telemetry.events import SOURCE_AUTOMATED_REVIEWER, SOURCE_USER, SessionTelemetry, session_telemetry
from meltzer.telemetry.feedback import FeedbackError, RingBufferHandler, run_feedback_command, upload_feedback
from meltzer.telemetry.otlp import OtlpLogExporter, parse_exporter

ENDPOINT = "https://collector.example/v1/logs"
OTLP = {"exporter": {"otlp-http": {"endpoint": ENDPOINT, "headers": {"x-team": "a"}, "protocol": "json"}}}


class Collector:
    def __init__(self, status: int = 200):
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status)

    def bodies(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]

    def records(self) -> list[dict]:
        """Name and attributes of every exported log record."""
        records = []
        for body in self.bodies():
            for record in body["resourceLogs"][0]["scopeLogs"][0]["logRecords"]:
                attributes = {a["key"]: next(iter(a["value"].values())) for a in record["attributes"]}
                records.append({"name": record["body"]["stringValue"], **attributes})
        return records


def telemetry_with(handler, notices: list[str] | None = None) -> SessionTelemetry:
    exporter = OtlpLogExporter(parse_exporter(OTLP), transport=httpx.MockTransport(handler))
    return SessionTelemetry("s-1", exporter=exporter, notice=(notices if notices is not None else []).append)


def config(**values) -> SimpleNamespace:
    defaults = {"otel": {}, "analytics_enabled": False, "feedback_enabled": False, "feedback_endpoint": None}
    return SimpleNamespace(**{**defaults, **values})


def test_events_carry_names_durations_decisions_and_exit_codes():
    collector = Collector()
    telemetry = telemetry_with(collector)
    telemetry.flush()
    assert collector.requests == []
    telemetry.conversation_start(model="gpt-x", approval_policy="on-request", reviewer="user", sandbox_mode="read-only")
    with telemetry.api_request("gpt-x"):
        pass
    with pytest.raises(RuntimeError):
        with telemetry.api_request("gpt-x"):
            raise RuntimeError("provider down")
    telemetry.tool_decision("shell", approved=True, source=SOURCE_USER)
    telemetry.tool_decision("apply_patch", approved=True, source=SOURCE_AUTOMATED_REVIEWER, for_session=True)
    telemetry.tool_decision("web_search", approved=False, source=SOURCE_USER)
    telemetry.tool_result("shell", success=False, duration=0.25)
    telemetry.sandbox_outcome(sandboxed=True, exit_code=2, duration=0.2)
    telemetry.flush()
    telemetry.flush()

    (request,) = collector.requests
    assert str(request.url) == ENDPOINT and request.headers["x-team"] == "a"
    resource = collector.bodies()[0]["resourceLogs"][0]
    assert {"key": "service.name", "value": {"stringValue": "meltzer"}} in resource["resource"]["attributes"]
    assert resource["scopeLogs"][0]["logRecords"][0]["severityText"] == "INFO"
    records = collector.records()
    names = (
        "conversation_starts api_request api_request tool_decision tool_decision tool_decision "
        "tool_result sandbox_outcome"
    )
    assert [r["name"] for r in records] == [f"meltzer.{name}" for name in names.split()]
    assert all(r["session.id"] == "s-1" and r["event.name"] == r["name"] for r in records)
    assert [r["status"] for r in records[1:3]] == ["ok", "error"]
    assert [r["decision"] for r in records[3:6]] == ["approved", "approved_for_session", "denied"]
    assert records[4]["source"] == "automated_reviewer"
    assert records[6] == {**records[6], "tool_name": "shell", "success": False, "duration_ms": "250"}
    assert records[7] == {**records[7], "sandboxed": True, "exit_code": "2", "duration_ms": "200"}
    telemetry.close()


def refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused")


@pytest.mark.parametrize("handler, message", [(Collector(500), "HTTP 500"), (refuse, "connection refused")])
def test_a_failed_export_is_a_notice_and_turns_the_export_off(handler, message):
    notices: list[str] = []
    telemetry = telemetry_with(handler, notices)
    telemetry.tool_result("shell", success=True, duration=0.0)
    telemetry.flush()
    telemetry.tool_result("shell", success=True, duration=0.0)
    telemetry.close()
    assert len(notices) == 1 and message in notices[0] and telemetry.exporter is None


def test_tool_names_and_values_are_checked_and_quoted():
    telemetry = session_telemetry(config(feedback_enabled=True), "s-1", print)
    telemetry.conversation_start(model="a model", approval_policy="never", reviewer="user", sandbox_mode="read-only")
    telemetry.tool_result("fake\nmeltzer.tool_result tool_name=shell", success=True, duration=0)
    telemetry.tool_result("x" * 65, success=True, duration=0)
    telemetry.tool_result("mcp__db__query", success=True, duration=0)
    telemetry.close()
    start, forged, long, known = telemetry.log_buffer.lines()
    assert 'model="a model"' in start
    assert "tool_name=unknown" in forged and "tool_name=unknown" in long and "tool_name=mcp__db__query" in known


def test_exporter_is_off_by_default():
    assert session_telemetry(config(), "s-1", print).exporter is None
    assert session_telemetry(config(otel={"exporter": "none"}, analytics_enabled=True), "s-1", print).exporter is None
    assert session_telemetry(config(otel=OTLP), "s-1", print).exporter is None
    assert session_telemetry(config(otel=OTLP, analytics_enabled=True), "s-1", print).exporter is not None


@pytest.mark.parametrize(
    "otel, message",
    [
        ({"exporter": {"otlp-grpc": {"endpoint": ENDPOINT}}}, "otel.exporter"),
        ({"exporter": {"otlp-http": {"endpoint": "collector:4318"}}}, "endpoint"),
        ({"exporter": {"otlp-http": {"endpoint": "http://[collector"}}}, "endpoint"),
        ({"exporter": {"otlp-http": {"endpoint": ENDPOINT, "protocol": "binary"}}}, "protocol"),
        ({"exporter": {"otlp-http": {"endpoint": ENDPOINT, "headers": {"x": 1}}}}, "headers"),
        ({"exporter": {"otlp-http": {"endpoint": ENDPOINT, "tls": {}}}}, "tls"),
    ],
)
def test_invalid_exporter_settings(otel, message):
    with pytest.raises(ConfigError, match=message):
        parse_exporter(otel)


def test_ring_buffer_keeps_the_most_recent_lines():
    handler = RingBufferHandler(max_bytes=40)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger("meltzer.test_ring_buffer")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    for n in range(10):
        log.info("line %d", n)
    log.removeHandler(handler)
    assert handler.lines() == ["line 5", "line 6", "line 7", "line 8", "line 9"]


def test_upload_feedback_needs_consent():
    collector = Collector()
    with pytest.raises(FeedbackError):
        upload_feedback(ENDPOINT, False, session_id="s-1", logs="x", transport=httpx.MockTransport(collector))
    assert collector.requests == []


def feedback_session(
    home, enabled: bool = True, interactive: bool = True, endpoint: str | None = "https://feedback.example.com/v1"
) -> SimpleNamespace:
    telemetry = session_telemetry(config(feedback_enabled=enabled), "s-1", print)
    telemetry.conversation_start(model="gpt-x", approval_policy="never", reviewer="user", sandbox_mode="read-only")
    settings = config(feedback_enabled=enabled, feedback_endpoint=endpoint)
    return SimpleNamespace(
        id="s-1", config=settings, ui=SimpleNamespace(interactive=interactive), telemetry=telemetry, home=home
    )


@pytest.mark.parametrize(
    "options, answer, sent",
    [({}, True, True), ({}, False, False), ({"enabled": False}, True, False), ({"interactive": False}, True, False)],
)
def test_feedback_command_sends_only_after_confirmation(home, options, answer, sent):
    collector = Collector()
    questions: list[str] = []

    def confirm(question: str) -> bool:
        questions.append(question)
        return answer

    session = feedback_session(home, **options)
    reply = run_feedback_command(session, " the turn hung ", confirm, transport=httpx.MockTransport(collector))
    assert len(collector.requests) == int(sent)
    if sent:
        assert "feedback sent" in reply
        assert collector.bodies()[0] == {**collector.bodies()[0], "session_id": "s-1", "note": "the turn hung"}
    if questions:
        (report,) = (home / ".meltzer" / "feedback").glob("s-1-*.txt")
        assert str(report) in questions[0]
        assert "the turn hung" in report.read_text(encoding="utf-8")
        assert "conversation_starts" in report.read_text(encoding="utf-8")


def test_feedback_command_needs_an_address(home):
    reply = run_feedback_command(feedback_session(home, endpoint=None), "note", lambda question: True)
    assert "no address" in reply
