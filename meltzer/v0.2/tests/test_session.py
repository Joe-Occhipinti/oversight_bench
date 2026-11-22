from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from meltzer import cli
from meltzer.approval.policy import ApprovalRequest
from meltzer.client import ModelResponse
from meltzer.config import ConfigError, load_config
from meltzer.history import Item
from meltzer.monitor.review import MonitorReviewer
from meltzer.session import Session
from meltzer.ui import TerminalUI

TEXT = "Keep commits small.\nNever edit generated files.\nmarker-7f3a\n"
HEADING = "\n# Developer instructions\n"


class RecordingClient:
    """Stands in for the model client: records the messages and answers with fixed text."""

    def __init__(self, content: str):
        self.content = content
        self.calls: list[list[dict]] = []

    def complete(self, messages, tools, *, model=None) -> ModelResponse:
        self.calls.append([dict(m) for m in messages])
        return ModelResponse(content=self.content)

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("MELTZER_API_KEY", "test-key")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture
def instructions_file(tmp_path: Path) -> Path:
    path = tmp_path / "instructions.md"
    path.write_text(TEXT, encoding="utf-8")
    return path


@pytest.fixture
def make_session(tmp_path, home, work, monkeypatch):
    # The session's private /tmp is created under tmp_path.
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    sessions: list[Session] = []

    def make(path: Path | None = None) -> Session:
        overrides = [("approval_policy", "never")]
        if path is not None:
            overrides.append(("developer_instructions_file", str(path)))
        config = load_config(work, home=home, overrides=overrides)
        session = Session(config, work, TerminalUI(interactive=False, stderr=io.StringIO()), home=home)
        sessions.append(session)
        return session

    yield make
    for session in sessions:
        session.close()


def system_message(session: Session) -> str:
    first = session._messages()[0]
    assert first["role"] == "system"
    return first["content"]


def files_under(path: Path) -> list[Path]:
    return [p for p in path.rglob("*") if p.is_file()]


# --- developer_instructions_file ----------------------------------------------------------


def test_text_is_appended_to_the_system_message(make_session, instructions_file):
    session = make_session(instructions_file)
    content = system_message(session)
    assert content.endswith(HEADING + TEXT)
    assert len(session._messages()) == 1


def test_no_section_without_text(make_session, tmp_path):
    assert "Developer instructions" not in system_message(make_session())
    empty = tmp_path / "empty.md"
    empty.write_text("", encoding="utf-8")
    assert "Developer instructions" not in system_message(make_session(empty))


def test_monitor_never_receives_the_text(make_session, instructions_file):
    session = make_session(instructions_file)
    session.history.append(Item.user("install the package"))
    client = RecordingClient('{"risk_level": "low", "user_authorization": "high", "outcome": "allow", "rationale": "ok"}')
    session.client = client
    request = ApprovalRequest(tool="shell", cwd=session.root, command="pip install -e .", escalate=True)
    assert MonitorReviewer(session).review(request).approved
    (messages,) = client.calls
    assert not any("marker-7f3a" in json.dumps(message) for message in messages)
    assert "install the package" in messages[-1]["content"]


def test_text_is_not_written_to_the_session_log(make_session, instructions_file, home):
    session = make_session(instructions_file)
    session.client = RecordingClient("done")
    session.run_turn("go")
    assert "marker-7f3a" in json.dumps(session.client.calls)
    session.recorder.close()
    meta = json.loads(session.recorder.path.read_text(encoding="utf-8").splitlines()[0])
    assert meta["config"]["developer_instructions_file"] == str(instructions_file)
    for path in files_under(home):
        assert b"marker-7f3a" not in path.read_bytes(), path


def test_subagent_reuses_the_text_without_reading_the_file(make_session, instructions_file, home):
    session = make_session(instructions_file)
    instructions_file.unlink()
    subagent = session._start_subagent()
    try:
        assert system_message(subagent) == system_message(session)
        assert system_message(subagent).endswith(HEADING + TEXT)
    finally:
        subagent.close()
    for path in files_under(home):
        assert b"marker-7f3a" not in path.read_bytes(), path


@pytest.mark.skipif(sys.platform != "linux", reason="needs /dev/stdin")
def test_file_can_be_dev_stdin_while_the_prompt_is_an_argument(tmp_path, home, work):
    script = (
        "import json, sys\n"
        "from meltzer import cli\n"
        "from meltzer.session import Session\n"
        "def run_turn(self, prompt):\n"
        "    print(json.dumps({'prompt': prompt, 'system': self._messages()[0]['content']}))\n"
        "    return 'done'\n"
        "Session.run_turn = run_turn\n"
        "sys.exit(cli.main(sys.argv[1:]))\n"
    )
    argv = ["exec", "-C", str(work), "-a", "never", "-c", "developer_instructions_file=/dev/stdin", "the prompt"]
    result = subprocess.run(
        [sys.executable, "-c", script, *argv],
        input=TEXT,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home), "MELTZER_API_KEY": "test-key"},
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    reported = json.loads(result.stdout.splitlines()[0])
    assert reported["prompt"] == "the prompt"
    assert reported["system"].endswith(HEADING + TEXT)


def test_unreadable_file_is_a_config_error(tmp_path, home, work, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(home))
    missing = tmp_path / "missing.md"
    argv = ["exec", "-C", str(work), "-a", "never", "-c", f"developer_instructions_file={missing}", "go"]
    assert cli.main(argv) == cli.EXIT_CONFIG
    assert "developer_instructions_file" in capsys.readouterr().err


def test_invalid_utf8_error_does_not_show_the_text(tmp_path, make_session):
    path = tmp_path / "binary.md"
    path.write_bytes(b"marker-7f3a \xff\xfe")
    with pytest.raises(ConfigError, match="not valid UTF-8") as exc:
        make_session(path)
    assert "marker-7f3a" not in str(exc.value)
