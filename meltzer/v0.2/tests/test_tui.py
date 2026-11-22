from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from meltzer.approval.policy import ApprovalChoice, ApprovalRequest
from meltzer.ui import TerminalUI, plain_text
from meltzer.utils import file_search
from meltzer.utils.file_search import list_files, match_score, search
from meltzer.utils.git import GitResult

try:
    import prompt_toolkit
except ImportError:  # the tui extra is not installed
    prompt_toolkit = None

needs_tui = pytest.mark.skipif(prompt_toolkit is None, reason="needs the tui extra (prompt_toolkit)")

if prompt_toolkit is not None:
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from meltzer.tools.plan import PLAN_COMMAND_HELP
    from meltzer.tui import FullScreenUI
    from meltzer.tui.file_picker import FileCompleter, mention_at_cursor
    from meltzer.tui.slash import SLASH_COMMANDS, SlashCompleter, help_text, local_command
    from meltzer.tools.request_user_input import Option, Question
    from meltzer.tui.widgets import request_choices, request_text

SRC = Path(__file__).resolve().parent.parent / "src"


# --- file search ---------------------------------------------------------------


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def make_tree(root: Path, paths: list[str]) -> None:
    for name in paths:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_git_listing_has_tracked_and_untracked_files_but_not_ignored_ones(tmp_path):
    git(tmp_path, "init", "-q")
    make_tree(tmp_path, ["src/main.py", "README.md", "build/out.bin", "notes.txt"])
    (tmp_path / ".gitignore").write_text("build/\n")
    git(tmp_path, "add", "src/main.py", "README.md", ".gitignore")
    assert sorted(list_files(tmp_path)) == [".gitignore", "README.md", "notes.txt", "src/main.py"]


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_git_listing_from_a_subdirectory_is_relative_to_it(tmp_path):
    git(tmp_path, "init", "-q")
    make_tree(tmp_path, ["src/pkg/a.py", "top.py"])
    assert list_files(tmp_path / "src") == ["pkg/a.py"]


def test_directory_walk_outside_a_repository(tmp_path, monkeypatch):
    monkeypatch.setattr(file_search, "_git_files", lambda root, max_files: None)
    make_tree(tmp_path, ["a.py", "pkg/b.py", ".hidden/c.py", "pkg/.cache/d.py", ".env"])
    assert sorted(list_files(tmp_path)) == [".env", "a.py", "pkg/b.py"]


def test_directory_walk_stops_at_the_file_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(file_search, "_git_files", lambda root, max_files: None)
    make_tree(tmp_path, [f"d{n}/f{m}.txt" for n in range(5) for m in range(5)])
    assert len(list_files(tmp_path, max_files=7)) == 7


def test_git_listing_is_cut_at_the_file_limit(tmp_path, monkeypatch):
    output = b"".join(f"f{n}.py\0".encode() for n in range(10))
    monkeypatch.setattr(file_search, "run_git", lambda args, cwd: GitResult(0, output, b""))
    assert list_files(tmp_path, max_files=3) == ["f0.py", "f1.py", "f2.py"]


def test_subsequence_matching():
    assert match_score("smp", "src/main.py") is not None
    assert match_score("SMP", "src/main.py") is not None
    assert match_score("pms", "src/main.py") is None
    assert match_score("", "anything") == 0


def test_search_ranks_name_matches_first_and_respects_the_limit():
    files = ["docs/main_notes.md", "src/domain.py", "src/main.py", "tests/test_main.py", "zzz/other.txt"]
    results = search("main", files)
    assert results[0] == "src/main.py"
    assert "zzz/other.txt" not in results
    assert set(results) == {"docs/main_notes.md", "src/domain.py", "src/main.py", "tests/test_main.py"}
    assert len(search("", files, limit=2)) == 2


# --- slash commands and the file picker ------------------------------------------


def completions(completer, text: str) -> list:
    return list(completer.get_completions(Document(text), CompleteEvent(text_inserted=True)))


@needs_tui
def test_slash_completion_at_the_start_of_the_input():
    assert [c.text for c in completions(SlashCompleter(), "/st")] == ["/status"]
    assert {c.text for c in completions(SlashCompleter(), "/")} == set(SLASH_COMMANDS)
    assert completions(SlashCompleter(), "run /st") == []
    assert completions(SlashCompleter(), "/plan a") == []


@needs_tui
def test_help_lists_every_command_and_the_plan_subcommands():
    text = help_text()
    for name in SLASH_COMMANDS:
        assert name in text
    assert PLAN_COMMAND_HELP in text


@pytest.mark.parametrize(
    "text, expected",
    [("/help", "/help"), (" /status ", "/status"), ("/clear now", "/clear"), ("/plan", None), ("/quit", None), ("hello", None)],
)
@needs_tui
def test_local_commands(text, expected):
    assert local_command(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [("look at @src/ma", "src/ma"), ("@", ""), ("@a", "a"), ("mail me@host", None), ("@done ", None), ("plain", None)],
)
@needs_tui
def test_mention_at_cursor(text, expected):
    assert mention_at_cursor(text) == expected


@needs_tui
def test_file_completer_replaces_the_mention_with_the_path(tmp_path):
    completer = FileCompleter(lambda: tmp_path, list_files=lambda root: ["src/main.py", "docs/my notes.md", "x\x1b[2Jy.py"])
    found = completions(completer, "open @smp")
    assert [c.text for c in found] == ["src/main.py"]
    assert found[0].start_position == -len("@smp")
    assert [c.text for c in completions(completer, "@notes")] == ['"docs/my notes.md"']
    escaped = completions(completer, "@xy")
    assert "\x1b" not in "".join(text for _, text in escaped[0].display)
    assert completions(completer, "no mention") == []


@needs_tui
def test_file_completer_lists_files_of_the_current_root_once(tmp_path):
    roots = []
    completer = FileCompleter(lambda: tmp_path, list_files=lambda root: roots.append(root) or ["a.py"])
    completions(completer, "@a")
    completions(completer, "@a")
    assert roots == [tmp_path]


# --- plain text and approval requests ---------------------------------------------


def test_plain_text_removes_escape_sequences_and_control_characters():
    text = "red \x1b[31mtext\x1b[0m\r\n\x1b]0;title\x07bell\x07 tab\there ‮evil\x9b é"
    assert plain_text(text) == "red text\nbell tab\there evil é"


def test_line_interface_prints_plain_text():
    stdout, stderr = io.StringIO(), io.StringIO()
    ui = TerminalUI(interactive=True, stdin=io.StringIO("n\n"), stdout=stdout, stderr=stderr)
    patch = "*** Begin Patch\n+x\x1b[2K\n*** End Patch\n"
    ui.assistant_message("hi \x1b[31mred\x1b[0m")
    ui.tool_call("shell", json.dumps({"command": "ls \x1b[2J"}))
    ui.tool_call("odd\x1b]0;x\x07name", "{}")
    ui.ask_approval(ApprovalRequest(tool="apply_patch", cwd=Path("/repo"), paths=(Path("/etc/x"),), patch=patch))
    assert "\x1b" not in stdout.getvalue() + stderr.getvalue() and "\x07" not in stderr.getvalue()
    assert "hi red" in stdout.getvalue() and "$ ls " in stderr.getvalue() and "[oddname]" in stderr.getvalue()


def command_request(**fields) -> ApprovalRequest:
    return ApprovalRequest(tool="shell", cwd=Path("/repo"), command="make deploy", escalate=True, **fields)


@needs_tui
def test_request_text_has_the_heading_and_the_planned_action():
    request = command_request(justification="needs the network")
    text = request_text(request)
    assert text.splitlines()[0] == "meltzer wants to run outside the sandbox:"
    assert json.dumps(request.planned_action(), indent=2) in text
    mcp = ApprovalRequest(tool="mcp_tool_call", cwd=Path("/repo"), server="ops", mcp_tool="restart", arguments={})
    assert request_text(mcp).startswith("meltzer wants to call an MCP tool:")
    rule = ApprovalRequest(tool="shell", cwd=Path("/repo"), command="git push")
    assert request_text(rule).startswith("a rule sends this command to review")


@needs_tui
def test_request_text_for_a_patch_lists_paths_and_the_patch_as_plain_text():
    patch = "*** Begin Patch\n+x\n*** End Patch\n"
    request = ApprovalRequest(tool="apply_patch", cwd=Path("/repo"), paths=(Path("/etc/hosts"),), patch=patch)
    assert f"  {Path('/etc/hosts')}" in request_text(request) and patch.rstrip() in request_text(request)
    request = ApprovalRequest(
        tool="apply_patch", cwd=Path("/repo"), paths=(Path("/repo/a" + chr(27) + "[8mb"),), patch="+<b>x</b>" + chr(27) + "[2K\n"
    )
    text = request_text(request)
    assert chr(27) not in text and "+<b>x</b>" in text and str(Path("/repo/ab")) in text


@needs_tui
def test_request_choices():
    assert [c for c, _ in request_choices(command_request())] == [ApprovalChoice.APPROVE_ONCE, ApprovalChoice.DENY]
    prefixed = request_choices(command_request(suggested_prefix=("make", "deploy")))
    assert [c for c, _ in prefixed] == [
        ApprovalChoice.APPROVE_ONCE,
        ApprovalChoice.ALWAYS_ALLOW_PREFIX,
        ApprovalChoice.DENY,
    ]
    assert "`make deploy`" in prefixed[1][1]
    for tool in ("request_permissions", "network_access"):
        request = ApprovalRequest(tool=tool, cwd=Path("/repo"), permissions={"network": True})
        choices = [c for c, _ in request_choices(request)]
        assert choices == [ApprovalChoice.APPROVE_ONCE, ApprovalChoice.APPROVE_FOR_SESSION, ApprovalChoice.DENY]


# --- the full-screen UI -------------------------------------------------------------

ENTER, ESC, CTRL_C, CTRL_D, UP, DOWN, TAB = "\r", "\x1b", "\x03", "\x04", "\x1b[A", "\x1b[B", "\t"


@pytest.fixture
def pipe():
    if prompt_toolkit is None:
        pytest.skip("needs the tui extra (prompt_toolkit)")
    with create_pipe_input() as pipe_input:
        yield pipe_input


@pytest.fixture
def ui(pipe):
    return FullScreenUI(input=pipe, output=DummyOutput())


def wait_for(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.01)


def drive(ui, pipe, worker, steps=()):
    """Run worker under the UI; each step waits for its condition, then sends its keys."""

    def send():
        for condition, keys in steps:
            wait_for(condition)
            pipe.send_text(keys)

    driver = threading.Thread(target=send, daemon=True)
    driver.start()
    result = ui.run(worker)
    driver.join(timeout=5)
    return result


def dialog_open(ui):
    return lambda: ui.dialog is not None


def waiting(ui):
    return lambda: ui.waiting_for_prompt


def stub_session(root: Path):
    return SimpleNamespace(
        id="1234",
        root=root,
        config=SimpleNamespace(model="gpt-test"),
        policy=SimpleNamespace(mode=SimpleNamespace(value="workspace-write")),
        settings=SimpleNamespace(policy=SimpleNamespace(value="on-request")),
    )


def test_worker_exception_is_raised_after_the_ui_closes(ui, pipe):
    def worker():
        raise ValueError("broken")

    with pytest.raises(ValueError, match="broken"):
        drive(ui, pipe, worker)


def read_prompt(ui, pipe, steps, first=lambda: None):
    """The prompt the worker reads (after running `first`) once the steps have sent their keys."""
    prompts = []
    drive(ui, pipe, lambda: first() or prompts.append(ui.read_prompt()) or 0, steps)
    return prompts


def test_prompts_typed_in_the_input_box(ui, pipe):
    assert read_prompt(ui, pipe, [(waiting(ui), "fix the build" + ENTER)]) == ["fix the build"]
    assert "> fix the build" in ui.transcript.text


def test_empty_input_is_not_sent_and_ctrl_c_clears_typed_text_first(ui, pipe):
    steps = [(waiting(ui), "   " + ENTER), (waiting(ui), "draft" + CTRL_C + "real" + ENTER)]
    assert read_prompt(ui, pipe, steps) == ["real"]


@pytest.mark.parametrize("keys", [CTRL_D, CTRL_C])
def test_ctrl_d_and_ctrl_c_at_an_empty_prompt_end_the_input(ui, pipe, keys):
    assert read_prompt(ui, pipe, [(waiting(ui), keys)]) == [None]


def test_ctrl_c_interrupts_a_running_turn(ui, pipe):
    events = []
    running = threading.Event()

    def worker():
        try:
            running.set()
            while True:
                time.sleep(0.01)
        except KeyboardInterrupt:
            events.append("interrupted")
        return 0

    drive(ui, pipe, worker, [(running.is_set, CTRL_C)])
    assert events == ["interrupted"]


def test_prompt_typed_during_a_turn_is_sent_when_the_turn_ends(ui, pipe):
    prompts = []

    def worker():
        wait_for(lambda: "> next" in ui.transcript.text)
        assert not ui.waiting_for_prompt
        prompts.append(ui.read_prompt())
        return 0

    drive(ui, pipe, worker, [(lambda: ui.started, "next" + ENTER)])
    assert prompts == ["next"]


def test_transcript_shows_messages_and_tool_calls_as_plain_text(ui, pipe):
    def worker():
        ui.info("warning: \x1b[31mred\x1b[0m")
        ui.assistant_message("<b>done</b> [ok] \x1b]0;x\x07")
        ui.tool_call("shell", json.dumps({"command": "ls -la", "escalate": True}))
        ui.tool_call("exec_command", json.dumps({"cmd": "top", "tty": True}))
        ui.tool_call("update_plan", "{}")
        wait_for(lambda: "[update_plan]" in ui.transcript.text)
        return 0

    drive(ui, pipe, worker)
    text = ui.transcript.text
    assert "warning: red" in text
    assert "<b>done</b> [ok]" in text
    assert "$ ls -la  [escalate]" in text
    assert "$ top  [tty]" in text
    assert "\x1b" not in text and "\x07" not in text


def test_status_line_shows_model_sandbox_and_cwd(ui, pipe, tmp_path):
    def worker():
        ui.attach_session(stub_session(tmp_path))
        return 0

    drive(ui, pipe, worker)
    status = ui.status_text()
    assert "gpt-test" in status and "workspace-write" in status and str(tmp_path) in status


def test_local_slash_commands_run_in_the_ui(ui, pipe, tmp_path):
    prompts = []

    def worker():
        ui.attach_session(stub_session(tmp_path))
        prompts.append(ui.read_prompt())
        return 0

    steps = [
        (waiting(ui), "/help" + ENTER),
        (lambda: "/status" in ui.transcript.text and "Ctrl-C" in ui.transcript.text, "/status" + ENTER),
        (lambda: "on-request" in ui.transcript.text, "/plan add write tests" + ENTER),
    ]
    drive(ui, pipe, worker, steps)
    assert prompts == ["/plan add write tests"]
    assert "approval policy: on-request" in ui.transcript.text
    assert "session: 1234" in ui.transcript.text


def test_clear_empties_the_transcript(ui, pipe):
    steps = [(lambda: "old output" in ui.transcript.text, "/clear" + ENTER), (lambda: ui.transcript.text == "", "/quit" + ENTER)]
    assert read_prompt(ui, pipe, steps, lambda: ui.info("old output")) == ["/quit"] and "old output" not in ui.transcript.text


def ask(ui, pipe, call, keys):
    answers = []

    def worker():
        answers.append(call())
        return 0

    drive(ui, pipe, worker, [(dialog_open(ui), keys)])
    return answers[0]


def test_typed_answer_does_not_come_from_prompts_typed_during_the_turn(ui, pipe):
    question = Question("db", "Database", "Which?", (Option("A", "a"), Option("B", "b")))
    answers, prompts = [], []

    def worker():
        wait_for(lambda: "> also run the tests" in ui.transcript.text)
        answers.append(ui.ask_questions([question]))
        prompts.append(ui.read_prompt())
        return 0

    steps = [
        (lambda: ui.started, "also run the tests" + ENTER),
        (dialog_open(ui), ENTER),
        (lambda: "Type your answer" in ui.transcript.text, "/help" + ENTER),
    ]
    drive(ui, pipe, worker, steps)
    assert answers == [{"db": ["/help"]}] and prompts == ["also run the tests"]


def test_approval_dialog_shows_the_full_request_and_focuses_deny(ui, pipe):
    request = command_request(justification="deploy to staging")
    seen = {}

    def check():
        if ui.dialog is None:
            return False
        seen["body"] = ui.dialog.body_text
        seen["focus"] = ui.dialog.selected_value
        seen["focused"] = ui.dialog.has_focus()
        return True

    answers = []
    drive(ui, pipe, lambda: answers.append(ui.ask_approval(request)) or 0, [(check, ENTER)])
    assert answers == [ApprovalChoice.DENY]
    assert seen["body"] == request_text(request)
    assert seen["focus"] == ApprovalChoice.DENY and seen["focused"]
    assert ui.dialog is None
    patch = "*** Begin Patch\n" + "".join(f"+line {n}\n" for n in range(500)) + "*** End Patch\n"
    long_patch = ApprovalRequest(tool="apply_patch", cwd=Path("/repo"), paths=(Path("/etc/x"),), patch=patch)
    assert "+line 499" in request_text(long_patch)


def test_enter_in_the_request_text_does_not_answer(ui, pipe):
    request = command_request()
    answers = []
    steps = [(dialog_open(ui), TAB + ENTER + TAB + UP), (lambda: ui.dialog.selected_value == ApprovalChoice.APPROVE_ONCE, ENTER)]
    drive(ui, pipe, lambda: answers.append(ui.ask_approval(request)) or 0, steps)
    assert answers == [ApprovalChoice.APPROVE_ONCE]


PERMISSIONS = ApprovalRequest(tool="request_permissions", cwd=Path("/repo"), permissions={"network": True})
REPO = Path("/repo")


@pytest.mark.parametrize(
    "keys, expected, make_call",
    [
        (ESC, ApprovalChoice.DENY, lambda ui: ui.ask_approval(command_request(suggested_prefix=("make",)))),
        (CTRL_C, ApprovalChoice.DENY, lambda ui: ui.ask_approval(command_request(suggested_prefix=("make",)))),
        ("yes y a" + ENTER, ApprovalChoice.DENY, lambda ui: ui.ask_approval(command_request())),
        (UP + ENTER, ApprovalChoice.APPROVE_ONCE, lambda ui: ui.ask_approval(command_request())),
        (UP + ENTER, ApprovalChoice.ALWAYS_ALLOW_PREFIX, lambda ui: ui.ask_approval(command_request(suggested_prefix=("make",)))),
        (DOWN + UP + UP + UP + ENTER, ApprovalChoice.APPROVE_ONCE, lambda ui: ui.ask_approval(command_request(suggested_prefix=("make",)))),
        (UP + ENTER, ApprovalChoice.APPROVE_FOR_SESSION, lambda ui: ui.ask_approval(PERMISSIONS)),
        (UP + UP + ENTER, ApprovalChoice.APPROVE_ONCE, lambda ui: ui.ask_approval(PERMISSIONS)),
        (ENTER, False, lambda ui: ui.confirm_hook("PreToolUse: ./check.sh")),
        (UP + ENTER, True, lambda ui: ui.confirm_hook("PreToolUse: ./check.sh")),
        (UP + ENTER, True, lambda ui: ui.confirm_project_trust(REPO, REPO)),
        (ENTER, False, lambda ui: ui.confirm_project_trust(REPO, REPO / "sub")),
        (ESC, None, lambda ui: ui.confirm_project_trust(REPO, REPO / "sub")),
    ],
)
def test_dialog_answers(ui, pipe, keys, expected, make_call):
    assert ask(ui, pipe, lambda: make_call(ui), keys) is expected


def test_dialogs_after_the_ui_closed_deny(ui, pipe):
    drive(ui, pipe, lambda: 0)
    assert ui.ask_approval(command_request()) == ApprovalChoice.DENY
    assert ui.confirm_hook("hook") is False
    assert ui.read_prompt() is None


def test_stderr_during_the_session_is_shown_and_printed_after_exit(ui, pipe, capsys):
    def worker():
        print("meltzer: config error: bad key", file=sys.stderr)
        wait_for(lambda: "bad key" in ui.transcript.text)
        return 2

    assert drive(ui, pipe, worker) == 2
    assert "meltzer: config error: bad key" in capsys.readouterr().err


def test_exec_never_imports_the_terminal_ui(tmp_path):
    script = """
import sys
from types import SimpleNamespace
from meltzer import cli

class StubSession:
    def run_turn(self, prompt):
        return "done"
    def close(self):
        pass

cli._start_session = lambda args, ui: StubSession()
code = cli.main(["exec", "go"])
print(code, "meltzer.tui" in sys.modules, "prompt_toolkit" in sys.modules)
"""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(SRC), os.environ.get("PYTHONPATH", "")])}
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=tmp_path)
    assert result.stdout.split() == ["done", "0", "False", "False"]


@needs_tui
def test_interactive_session_uses_the_full_screen_ui_when_enabled(tmp_path, monkeypatch):
    from meltzer import cli

    seen = []
    session = SimpleNamespace(
        **vars(stub_session(tmp_path)),
        closed=False,
        agent_control=SimpleNamespace(prompt_lock=threading.Lock()),
        run_turn=lambda p: "",
        close=lambda: None,
    )
    monkeypatch.setattr(cli, "_start_session", lambda args, ui: seen.append(type(ui).__name__) or session)
    monkeypatch.setattr(FullScreenUI, "run", lambda self, worker: worker())
    monkeypatch.setattr(FullScreenUI, "read_prompt", lambda self: "/quit")
    assert cli.main(["-C", str(tmp_path), "-c", "tui.enabled=true"]) == 0
    assert seen == ["FullScreenUI"]
