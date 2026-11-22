from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from meltzer import __version__, cli
from meltzer.cli import EXIT_CONFIG, build_parsers, cli_overrides, main


@pytest.fixture
def parsers():
    return build_parsers()


def test_interactive_defaults(parsers):
    interactive, _ = parsers
    args = interactive.parse_args([])
    assert args.prompt is None
    assert args.cd is None and args.model is None and args.sandbox is None
    assert args.config == []
    assert cli_overrides(args) == []


def test_interactive_first_prompt(parsers):
    interactive, _ = parsers
    assert interactive.parse_args(["explain this repo"]).prompt == "explain this repo"


def test_exec_requires_prompt(parsers):
    _, headless = parsers
    with pytest.raises(SystemExit):
        headless.parse_args([])
    assert headless.parse_args(["-"]).prompt == "-"


def test_flags_become_overrides_after_dash_c(parsers):
    _, headless = parsers
    args = headless.parse_args(
        ["-C", "/repo", "-m", "gpt-x", "-c", "model=other", "-c", "max_turn_steps=5", "-s", "read-only", "-a", "never", "go"]
    )
    assert args.cd == "/repo"
    assert cli_overrides(args) == [
        ("model", "other"),
        ("max_turn_steps", 5),
        ("model", "gpt-x"),
        ("sandbox_mode", "read-only"),
        ("approval_policy", "never"),
    ]


def test_bypass_flag(parsers):
    interactive, _ = parsers
    args = interactive.parse_args(["--dangerously-bypass-approvals-and-sandbox"])
    assert cli_overrides(args) == [("sandbox_mode", "full-access"), ("approval_policy", "never")]


@pytest.mark.parametrize("argv", [["-s", "everything"], ["-a", "always"]])
def test_invalid_choices_exit(parsers, argv):
    interactive, _ = parsers
    with pytest.raises(SystemExit):
        interactive.parse_args(argv)


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"meltzer {__version__}"


def test_bad_override_is_a_config_error(capsys):
    assert main(["exec", "-c", "no-equals-sign", "go"]) == EXIT_CONFIG
    assert "expected key=value" in capsys.readouterr().err


class StubSession:
    def __init__(self, root):
        self.config = SimpleNamespace(model="gpt-test")
        self.policy = SimpleNamespace(mode=SimpleNamespace(value="workspace-write"))
        self.root = root
        self.prompts: list[str] = []
        self.closed = False

    def run_turn(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return "answer"

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("command", ["/exit", "/quit", " /exit "])
def test_exit_command_as_first_prompt_ends_the_session(tmp_path, monkeypatch, command):
    session = StubSession(tmp_path)
    monkeypatch.setattr(cli, "_start_session", lambda args, ui: session)
    assert main([command]) == 0
    assert session.prompts == [] and session.closed


def test_first_prompt_then_prompts_from_stdin(tmp_path, monkeypatch):
    session = StubSession(tmp_path)
    monkeypatch.setattr(cli, "_start_session", lambda args, ui: session)
    monkeypatch.setattr("sys.stdin", io.StringIO("second\n/exit\nnot sent\n"))
    assert main(["first"]) == 0
    assert session.prompts == ["first", "second"]


def test_missing_directory_is_a_config_error(tmp_path, capsys):
    assert main(["exec", "-C", str(tmp_path / "missing"), "go"]) == EXIT_CONFIG
    assert "directory does not exist" in capsys.readouterr().err
