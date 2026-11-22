from __future__ import annotations

import io

import pytest

from meltzer import cli
from meltzer.history import Kind
from meltzer.session import Session
from meltzer.ui import TerminalUI
from meltzer.user_shell import run_user_shell_command, user_shell_command_text

from conftest import needs_posix

pytestmark = pytest.mark.usefixtures("api_key")



class NoModel:
    def complete(self, messages, tools, *, model=None):
        raise AssertionError("no model request expected")

    def close(self) -> None:
        pass


@pytest.mark.parametrize(
    "prompt, command",
    [("!ls -la", "ls -la"), ("  ! git status ", "git status"), ("!", ""), ("ls", None), ("run !ls", None)],
)
def test_user_shell_command_text(prompt, command):
    assert user_shell_command_text(prompt) == command


@pytest.fixture
def session(session_factory):
    ui = TerminalUI(interactive=True, stdin=io.StringIO(), stdout=io.StringIO(), stderr=io.StringIO())
    session = session_factory(("sandbox_mode", "read-only"), ui=ui)
    session.client = NoModel()
    return session


def shell_items(session: Session):
    return [item for item in session.history if item.kind == Kind.USER_SHELL_COMMAND]


@needs_posix
def test_command_runs_as_the_user_in_the_root_and_is_added_to_the_conversation(session, tmp_path):
    shown = run_user_shell_command(session, "pwd; touch ../made-by-user; exit 3")
    assert (tmp_path / "made-by-user").exists()
    assert str(session.root) in shown and "Exit code: 3" in shown
    [item] = shell_items(session)
    assert "<command>\npwd; touch ../made-by-user; exit 3\n</command>" in item.content
    assert "Exit code: 3" in item.content and str(session.root) in item.content
    assert session.history.to_messages()[-1]["role"] == "user"


@needs_posix
def test_command_stops_at_the_shell_timeout(session):
    session.runner.timeout = 0.5
    shown = run_user_shell_command(session, "echo started; sleep 10")
    assert "timed out" in shown and "started" in shown
    [item] = shell_items(session)
    assert "timed out" in item.content


def test_empty_command_shows_usage(session):
    assert "usage" in run_user_shell_command(session, "")
    assert shell_items(session) == []


@needs_posix
def test_interactive_prompt_with_an_exclamation_mark_runs_a_command(session, monkeypatch):
    stdout = io.StringIO()
    ui = TerminalUI(interactive=True, stdin=io.StringIO("!echo from-the-user\n/exit\n"), stdout=stdout, stderr=stdout)
    session.ui = ui
    monkeypatch.setattr(cli, "_start_session", lambda args, ui: session)
    monkeypatch.setattr(cli, "TerminalUI", lambda interactive: ui)
    assert cli.run_interactive(cli.build_parsers()[0].parse_args([])) == cli.EXIT_OK
    assert "from-the-user" in stdout.getvalue()
    [item] = shell_items(session)
    assert "echo from-the-user" in item.content
