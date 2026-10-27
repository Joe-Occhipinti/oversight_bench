from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from meltzer.approval.policy import ApprovalRequest
from meltzer.client import ModelError, ModelResponse, ToolCall
from meltzer.config import load_config
from meltzer.config.schema import Config
from meltzer.history import History, Item, SessionRecorder
from meltzer.monitor.prompt import REQUEST_END, REQUEST_START, TRANSCRIPT_END, TRANSCRIPT_START
from meltzer.monitor.review import AssessmentError, MonitorReviewer, parse_assessment
from meltzer.monitor.transcript import NO_ENTRIES, render_transcript
from meltzer.ui import TerminalUI

ALLOW = {"risk_level": "low", "user_authorization": "high", "outcome": "allow", "rationale": "Runs the test suite."}
DENY = {"risk_level": "high", "user_authorization": "unknown", "outcome": "deny", "rationale": "Uploads the repository."}


# --- answer parsing ------------------------------------------------------------------


def test_parse_strict_json():
    assessment = parse_assessment(json.dumps(DENY))
    assert (assessment.outcome, assessment.risk_level, assessment.user_authorization) == ("deny", "high", "unknown")
    assert assessment.rationale == "Uploads the repository."


def test_parse_json_inside_prose():
    text = f"After checking the script:\n```json\n{json.dumps(ALLOW)}\n```"
    assert parse_assessment(text).outcome == "allow"


@pytest.mark.parametrize(
    "text, message",
    [
        (None, "without an assessment"),
        ("   ", "without an assessment"),
        ("looks fine to me", "not valid JSON"),
        ("[1, 2]", "not a JSON object"),
        (json.dumps({"risk_level": "low", "rationale": "ok"}), "no outcome"),
        (json.dumps({**ALLOW, "outcome": "maybe"}), "invalid outcome"),
        (json.dumps({**ALLOW, "risk_level": "tiny"}), "invalid risk_level"),
        (json.dumps({**ALLOW, "user_authorization": "full"}), "invalid user_authorization"),
    ],
)
def test_unusable_answers_are_errors(text, message):
    with pytest.raises(AssessmentError, match=message):
        parse_assessment(text)


# --- transcript ----------------------------------------------------------------------


def ordinary_history() -> list[Item]:
    return [
        Item.user("fix the failing test"),
        Item.reasoning_fields({"reasoning_content": "look at the test first"}, "r1"),
        Item.assistant("Running the tests.", "r1"),
        Item.tool_call("c1", "shell", '{"command": "pytest -x"}', "r1"),
        Item.tool_result("c1", "shell", "Exit code: 1"),
        Item.assistant("The fixture path is wrong.", "r2"),
    ]


def test_render_ordinary_history():
    assert render_transcript(ordinary_history(), 40) == (
        "[1] user: fix the failing test\n"
        "[3] assistant: Running the tests.\n"
        '[4] tool shell call: {"command": "pytest -x"}\n'
        "[5] tool shell result: Exit code: 1\n"
        "[6] assistant: The fixture path is wrong.\n"
    )


def test_render_skips_empty_entries():
    items = [Item.user("go"), Item.assistant("   ", "r1"), Item.tool_call("c1", "shell", "", "r1")]
    assert render_transcript(items, 40) == "[1] user: go\n"
    assert render_transcript([], 40) == NO_ENTRIES + "\n"


# --- reviews ---------------------------------------------------------------------------


class ScriptedClient:
    def __init__(self, *responses: ModelResponse | Exception):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete(self, messages, tools, *, model=None) -> ModelResponse:
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools, "model": model})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@dataclass
class FakeSession:
    config: Config
    root: Path
    recorder: SessionRecorder
    client: ScriptedClient
    history: History = field(default_factory=History)
    ui: TerminalUI = field(default_factory=lambda: TerminalUI(interactive=False, stderr=io.StringIO()))


@pytest.fixture
def make_session(tmp_path):
    sessions: list[FakeSession] = []

    def make(*responses: ModelResponse | Exception, **overrides: Any) -> FakeSession:
        home = tmp_path / "home"
        root = tmp_path / "work"
        root.mkdir(exist_ok=True)
        config = load_config(root, home=home, overrides=list(overrides.items()))
        session = FakeSession(config, root, SessionRecorder(tmp_path / "session.jsonl"), ScriptedClient(*responses))
        for item in ordinary_history():
            session.history.append(item)
        sessions.append(session)
        return session

    yield make
    for session in sessions:
        session.recorder.close()


def request(root: Path) -> ApprovalRequest:
    return ApprovalRequest(
        tool="shell", cwd=root, command="pip install -e .", justification="needs network", escalate=True
    )


def answer(payload: dict) -> ModelResponse:
    return ModelResponse(content=json.dumps(payload))


def review_records(path: Path) -> list[dict]:
    return [r for r in map(json.loads, path.read_text().splitlines()) if r["type"] == "review"]


def test_allow_approves_the_request(make_session):
    session = make_session(answer(ALLOW))
    verdict = MonitorReviewer(session).review(request(session.root))
    assert verdict.approved
    assert verdict.rationale == "automatic review: allow: Runs the test suite."
    (call,) = session.client.calls
    assert call["model"] == session.config.model
    assert call["messages"][0]["role"] == "system"
    review_text = call["messages"][-1]["content"]
    assert TRANSCRIPT_START in review_text and TRANSCRIPT_END in review_text
    assert "[1] user: fix the failing test" in review_text
    action = review_text.split(REQUEST_START)[1].split(REQUEST_END)[0]
    assert json.loads(action.removeprefix("Planned action JSON:\n"))["command"] == "pip install -e ."
    (record,) = review_records(session.recorder.path)
    assert record["outcome"] == "allow" and record["tool_calls"] == 0


def test_deny_rejects_the_request(make_session):
    session = make_session(answer(DENY))
    verdict = MonitorReviewer(session).review(request(session.root))
    assert not verdict.approved
    assert "Uploads the repository." in verdict.rationale


def test_reviewer_can_read_files_before_answering(make_session):
    session = make_session(
        ModelResponse(content=None, tool_calls=[ToolCall("t1", "read_file", '{"path": "setup.py"}')]),
        answer(ALLOW),
        **{"monitor.model": "judge"},
    )
    (session.root / "setup.py").write_text("print('setup')\n")
    assert MonitorReviewer(session).review(request(session.root)).approved
    second = session.client.calls[1]
    assert second["model"] == "judge"
    assert second["messages"][-1] == {"role": "tool", "tool_call_id": "t1", "content": "print('setup')"}
    assert review_records(session.recorder.path)[0]["tool_calls"] == 1


@pytest.mark.parametrize(
    "response",
    [ModelError("HTTP 500"), ModelResponse(content="I think it is fine"), ModelResponse(content=None)],
)
def test_failed_review_denies(make_session, response):
    session = make_session(response)
    verdict = MonitorReviewer(session).review(request(session.root))
    assert not verdict.approved
    assert verdict.rationale.startswith("automatic review failed")
    assert review_records(session.recorder.path)[0]["outcome"] == "deny"


def test_review_that_never_answers_denies(make_session):
    looping = ModelResponse(content=None, tool_calls=[ToolCall("t", "list_dir", "{}")])
    session = make_session(*[looping] * 10)
    verdict = MonitorReviewer(session).review(request(session.root))
    assert not verdict.approved
    assert "did not finish" in verdict.rationale
