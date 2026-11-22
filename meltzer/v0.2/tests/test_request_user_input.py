from __future__ import annotations

import io
import json
import threading
import time

import pytest

from meltzer.session import Session
from meltzer.tools.registry import ToolRegistry
from meltzer.tools.request_user_input import CANCELLED, Option, Question, RequestUserInputTool
from meltzer.ui import TerminalUI

try:
    import prompt_toolkit
except ImportError:  # the tui extra is not installed
    prompt_toolkit = None

needs_tui = pytest.mark.skipif(prompt_toolkit is None, reason="needs the tui extra (prompt_toolkit)")
pytestmark = pytest.mark.usefixtures("api_key")

OPTIONS = [{"label": "Postgres", "description": "Use the existing database."}, {"label": "SQLite", "description": "A local file."}]
QUESTION = {"id": "db", "header": "Database", "question": "Which database should the tests use?", "options": OPTIONS}
PARSED = Question("db", "Database", "Which database should the tests use?", (Option("Postgres", "Use the existing database."), Option("SQLite", "A local file.")))


def dispatch(tool: RequestUserInputTool, arguments: dict):
    return ToolRegistry([tool]).dispatch("request_user_input", json.dumps(arguments))


def test_answers_are_the_tool_result():
    asked = []
    tool = RequestUserInputTool(lambda questions: asked.append(questions) or {"db": ["SQLite"], "env": ["staging only"]})
    second = {**QUESTION, "id": "env", "header": "Env", "question": "Where?"}
    output = dispatch(tool, {"questions": [QUESTION, second]})
    assert output.success
    assert json.loads(output.text) == {"answers": {"db": {"answers": ["SQLite"]}, "env": {"answers": ["staging only"]}}}
    assert asked[0][0] == PARSED


@pytest.mark.parametrize(
    "questions",
    [
        [],
        [{**QUESTION, "id": str(n)} for n in range(4)],
        [{**QUESTION, "id": ""}],
        [QUESTION, QUESTION],
        [{**QUESTION, "question": None}],
        [{**QUESTION, "options": OPTIONS[:1]}],
        [{**QUESTION, "options": [OPTIONS[0], {"label": "", "description": "x"}]}],
        [{**QUESTION, "options": [OPTIONS[0], {"label": "B"}]}],
        "db",
    ],
)
def test_invalid_questions_are_rejected(questions):
    asked = []
    output = dispatch(RequestUserInputTool(lambda q: asked.append(q) or {}), {"questions": questions})
    assert not output.success
    assert asked == []


def test_a_question_closed_without_an_answer_is_a_tool_error():
    output = dispatch(RequestUserInputTool(lambda questions: None), {"questions": [QUESTION]})
    assert (output.success, output.text) == (False, CANCELLED)


def terminal(text: str) -> tuple[TerminalUI, io.StringIO]:
    stdout = io.StringIO()
    return TerminalUI(interactive=True, stdin=io.StringIO(text), stdout=stdout, stderr=io.StringIO()), stdout


def test_terminal_answers_by_number_or_with_own_text():
    ui, stdout = terminal("\n2\nonly in CI\n²\n")
    other = Question("ci", "CI", "Run where?", PARSED.options)
    third = Question("n", "N", "Which?\x1b[2J", PARSED.options)
    assert ui.ask_questions([PARSED, other, third]) == {"db": ["SQLite"], "ci": ["only in CI"], "n": ["²"]}
    shown = stdout.getvalue()
    assert "Which database should the tests use?" in shown
    assert "1. Postgres: Use the existing database." in shown
    assert "\x1b" not in shown


def test_terminal_end_of_input_cancels():
    ui, _ = terminal("1\n")
    assert ui.ask_questions([PARSED, PARSED]) is None


# --- full-screen interface -------------------------------------------------------------


ENTER, UP = "\r", "\x1b[A"


@needs_tui
@pytest.mark.parametrize(
    "keys, expected",
    [([UP + ENTER], ["SQLite"]), ([ENTER, "staging only" + ENTER], ["staging only"]), (["\x1b"], None)],
)
def test_full_screen_dialog_answers(keys, expected):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from meltzer.tui import FullScreenUI

    with create_pipe_input() as pipe:
        ui = FullScreenUI(input=pipe, output=DummyOutput())
        answers = []

        def send():
            for step, text in enumerate(keys):
                condition = (lambda: ui.dialog is not None) if step == 0 else (lambda: ui.waiting_for_prompt)
                deadline = time.monotonic() + 5
                while not condition() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if step == 0:
                    seen.append(ui.dialog.body_text)
                pipe.send_text(text)

        seen: list[str] = []
        driver = threading.Thread(target=send, daemon=True)
        driver.start()
        ui.run(lambda: answers.append(ui.ask_questions([PARSED])) or 0)
        driver.join(timeout=5)
    assert answers == [None if expected is None else {"db": expected}]
    assert "Which database should the tests use?" in seen[0] and "SQLite: A local file." in seen[0]


# --- session -------------------------------------------------------------------------


@pytest.fixture
def make_session(session_factory):
    def make(interactive: bool) -> Session:
        ui = TerminalUI(interactive=interactive, stdin=io.StringIO(), stdout=io.StringIO(), stderr=io.StringIO())
        return session_factory(ui=ui)

    return make


def tool_names(session: Session) -> set[str]:
    return {spec["function"]["name"] for spec in session.tools.specs()}


def test_tool_is_offered_only_in_the_top_level_interactive_session(make_session):
    interactive = make_session(True)
    assert "request_user_input" in tool_names(interactive)
    child = interactive.start_subagent()
    try:
        assert "request_user_input" not in tool_names(child)
    finally:
        child.close()
    assert "request_user_input" not in tool_names(make_session(False))
