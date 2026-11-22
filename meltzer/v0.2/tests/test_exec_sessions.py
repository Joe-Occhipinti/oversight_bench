from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from meltzer.approval.policy import ApprovalPolicy
from meltzer.exec import ProcessRunner
from meltzer.exec_sessions import (
    DEFAULT_EXEC_YIELD_TIME_MS,
    MAX_PROCESSES,
    PROTECTED_RECENT_PROCESSES,
    HeadTailBuffer,
    ProcessStore,
    exec_yield_seconds,
    write_yield_seconds,
)
from meltzer.sandbox.policy import SandboxMode
from meltzer.tools.exec_command import ExecCommandTool
from meltzer.tools.registry import ToolError
from meltzer.tools.write_stdin import WriteStdinTool

from conftest import RecordingReviewer, make_orchestrator, needs_posix, skip_without_bwrap

MAX_TIMEOUT_MS = 300_000


class Harness:
    def __init__(
        self,
        root: Path,
        tmp_path: Path,
        *,
        mode: SandboxMode = SandboxMode.FULL_ACCESS,
        approval_policy: ApprovalPolicy = ApprovalPolicy.ON_REQUEST,
        approved: bool = True,
    ):
        self.reviewer = RecordingReviewer(approved)
        self.orchestrator = make_orchestrator(root, self.reviewer, policy=approval_policy, mode=mode)
        policy = self.orchestrator.sandbox
        self.runner = ProcessRunner(policy, log_dir=tmp_path / "logs", timeout=30)
        self.store = ProcessStore()
        self.exec_command = ExecCommandTool(policy.root, self.orchestrator, self.runner, self.store, "session-a")
        self.write_stdin = WriteStdinTool(self.store, MAX_TIMEOUT_MS, "session-a")

    def start(self, cmd: str, **arguments) -> str:
        arguments.setdefault("yield_time_ms", 500)
        return self.exec_command.run({"cmd": cmd, **arguments})

    def write(self, session_id: int, chars: str, **arguments) -> str:
        arguments.setdefault("yield_time_ms", 500)
        return self.write_stdin.run({"session_id": session_id, "chars": chars, **arguments})

    def close(self) -> None:
        self.store.terminate_all()


@pytest.fixture
def harness(root, tmp_path):
    h = Harness(root, tmp_path)
    yield h
    h.close()


@pytest.fixture
def sandboxed(root, tmp_path):
    skip_without_bwrap()
    h = Harness(root, tmp_path, mode=SandboxMode.WORKSPACE_WRITE)
    yield h
    h.close()


def session_id(output: str) -> int:
    match = re.search(r"^Process running with session ID (\d+)$", output, re.MULTILINE)
    assert match is not None, output
    return int(match.group(1))


def exit_code(output: str) -> int | None:
    match = re.search(r"^Process exited with code (-?\d+)$", output, re.MULTILINE)
    return int(match.group(1)) if match else None


def output_text(output: str) -> str:
    return output.split("Output:\n", 1)[1]


# --- output buffer, yield times, store -------------------------------------------------


def test_head_tail_buffer_keeps_all_output_under_the_limit():
    buffer = HeadTailBuffer(max_bytes=16)
    buffer.append(b"hello ")
    buffer.append(b"world")
    assert buffer.drain() == b"hello world"
    assert buffer.drain() == b""
    buffer = HeadTailBuffer(max_bytes=8)
    for chunk in (b"abcdef", b"ghijkl", b"mnop"):
        buffer.append(chunk)
    assert buffer.drain() == b"abcd\n[... 8 bytes omitted ...]\nmnop"
    buffer.append(b"xyz")
    assert buffer.drain() == b"xyz"


def test_exec_yield_is_clamped():
    assert exec_yield_seconds(None) == DEFAULT_EXEC_YIELD_TIME_MS / 1000
    assert exec_yield_seconds(10) == 0.25
    assert exec_yield_seconds(1_000) == 1.0
    assert exec_yield_seconds(120_000) == 30.0


def test_write_yield_is_clamped():
    assert write_yield_seconds(None, "x", MAX_TIMEOUT_MS) == 0.25
    assert write_yield_seconds(10, "x", MAX_TIMEOUT_MS) == 0.25
    assert write_yield_seconds(2_000, "x", MAX_TIMEOUT_MS) == 2.0
    assert write_yield_seconds(120_000, "x", MAX_TIMEOUT_MS) == 30.0
    # Empty input polls: at least 5 seconds, at most background_terminal_max_timeout.
    assert write_yield_seconds(None, "", MAX_TIMEOUT_MS) == 5.0
    assert write_yield_seconds(60_000, "", MAX_TIMEOUT_MS) == 60.0
    assert write_yield_seconds(900_000, "", MAX_TIMEOUT_MS) == 300.0
    assert write_yield_seconds(60_000, "", 20_000) == 20.0


class FakeProcess:
    def __init__(self, process_id: int, exited: bool = False, owner: str = "session-a"):
        self.id = process_id
        self.owner = owner
        self.exited = exited
        self.terminated = False
        self.last_used = 0.0

    def has_exited(self) -> bool:
        return self.exited

    def terminate(self) -> None:
        self.terminated = True

    def remaining_group(self) -> int | None:
        return None


def fill(store: ProcessStore, count: int, exited: set[int] = frozenset()) -> list[FakeProcess]:
    processes = []
    for n in range(count):
        process = FakeProcess(n, exited=n in exited)
        store.add(process)
        process.last_used = float(n)
        processes.append(process)
    return processes


def test_store_prunes_the_least_recently_used_process():
    store = ProcessStore()
    processes = fill(store, MAX_PROCESSES)
    processes[3].last_used = -1.0
    store.add(FakeProcess(1000))
    assert len(store) == MAX_PROCESSES
    assert processes[3].terminated
    assert 3 not in store.ids() and 1000 in store.ids()


def test_store_prunes_exited_processes_first():
    store = ProcessStore()
    processes = fill(store, MAX_PROCESSES, exited={20})
    store.add(FakeProcess(1000))
    assert processes[20].terminated and not processes[0].terminated
    assert 20 not in store.ids()


def test_store_keeps_the_most_recent_processes():
    store = ProcessStore(max_processes=10)
    # Only the most recently used processes have exited.
    processes = fill(store, 10, exited=set(range(10 - PROTECTED_RECENT_PROCESSES, 10)))
    store.add(FakeProcess(1000))
    pruned = [p.id for p in processes if p.terminated]
    assert pruned == [0]


def test_store_lookup_marks_a_process_as_used_and_finds_only_the_processes_of_the_asking_session():
    store = ProcessStore()
    process = FakeProcess(7)
    store.add(process)
    process.last_used = 0.0
    assert store.get(7, "session-a") is process
    assert process.last_used > 0.0
    for process_id, owner in ((8, "session-a"), (7, "session-b")):
        with pytest.raises(KeyError):
            store.get(process_id, owner)
    store.remove(7)
    assert len(store) == 0


def test_store_terminate_all():
    store = ProcessStore()
    processes = fill(store, 3)
    store.terminate_all()
    assert all(p.terminated for p in processes)
    assert len(store) == 0


def test_store_allocates_unused_ids():
    store = ProcessStore()
    ids = {store.allocate_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(1_000 <= process_id <= 99_999 for process_id in ids)


def test_store_terminate_owner_stops_only_that_sessions_processes():
    store = ProcessStore()
    mine = [FakeProcess(1, owner="session-a"), FakeProcess(2, owner="session-a")]
    other = FakeProcess(3, owner="session-b")
    for process in (*mine, other):
        store.add(process)
    store.terminate_owner("session-a")
    assert all(p.terminated for p in mine) and not other.terminated
    assert store.ids() == [3]


# --- argument checks and decisions ------------------------------------------------------


@pytest.mark.parametrize(
    "tool, arguments, message",
    [
        ("exec_command", {}, "'cmd' must be a non-empty string"),
        ("exec_command", {"cmd": "ls", "escalate": True}, "'justification' is required"),
        ("exec_command", {"cmd": "ls", "tty": "yes"}, "'tty' must be true or false"),
        ("exec_command", {"cmd": "ls", "yield_time_ms": "1000"}, "'yield_time_ms' must be a non-negative integer"),
        ("exec_command", {"cmd": "ls", "max_output_tokens": 0}, "'max_output_tokens' must be a positive integer"),
        ("exec_command", {"cmd": "ls", "workdir": "missing"}, "workdir does not exist"),
        ("write_stdin", {"chars": "x"}, "'session_id' must be an integer"),
        ("write_stdin", {"session_id": True, "chars": "x"}, "'session_id' must be an integer"),
        ("write_stdin", {"session_id": 1, "chars": 3}, "'chars' must be a string"),
    ],
)
def test_argument_errors(harness, tool, arguments, message):
    with pytest.raises(ToolError, match=re.escape(message)):
        getattr(harness, tool).run(arguments)


@needs_posix
def test_write_stdin_cannot_reach_a_process_of_another_session(harness):
    sid = session_id(harness.start("cat", tty=True))
    other = WriteStdinTool(harness.store, MAX_TIMEOUT_MS, "session-b")
    with pytest.raises(ToolError, match=f"Unknown process id {sid}"):
        other.run({"session_id": sid, "chars": "x", "yield_time_ms": 500})
    with pytest.raises(ToolError, match="Unknown process id 4242"):
        harness.write(4242, "x")
    assert sid in harness.store.ids()
    assert "hello" in output_text(harness.write(sid, "hello\n"))


def test_denied_escalation_does_not_start_a_process(root, tmp_path):
    h = Harness(root, tmp_path, mode=SandboxMode.WORKSPACE_WRITE, approved=False)
    with pytest.raises(ToolError, match="command not run: denied by user"):
        h.start("python3 manage.py shell", tty=True, escalate=True, justification="Open the Django shell.")
    assert len(h.store) == 0
    [request] = h.reviewer.requests
    assert request.planned_action() == {
        "tool": "exec_command",
        "command": "python3 manage.py shell",
        "cwd": str(h.orchestrator.sandbox.root),
        "justification": "Open the Django shell.",
        "escalate": True,
        "tty": True,
    }


# --- processes --------------------------------------------------------------------------


@needs_posix
def test_command_that_exits_returns_its_exit_code(harness):
    output = harness.start("echo out; echo err >&2; exit 3")
    assert exit_code(output) == 3
    assert "session ID" not in output
    assert "out\nerr\n" in output_text(output)
    assert re.search(r"^Chunk ID: [0-9a-f]{6}$", output, re.MULTILINE)
    assert re.search(r"^Wall time: \d+\.\d seconds$", output, re.MULTILINE)
    assert len(harness.store) == 0


@needs_posix
def test_workdir_and_environment_of_the_process(harness, root):
    (root / "pkg").mkdir()
    output = harness.start("pwd; echo $TERM $PAGER $NO_COLOR", workdir="pkg")
    assert output_text(output).splitlines()[:2] == [str(harness.orchestrator.sandbox.root / "pkg"), "dumb cat 1"]


@needs_posix
def test_tty_session_echoes_input(harness):
    output = harness.start("cat", tty=True)
    sid = session_id(output)
    assert exit_code(output) is None
    output = harness.write(sid, "hello\n")
    assert session_id(output) == sid
    assert output_text(output).count("hello") == 2  # the terminal echo and cat's output
    output = harness.write(sid, "\x04")
    assert exit_code(output) == 0
    assert len(harness.store) == 0
    with pytest.raises(ToolError, match=f"Unknown process id {sid}"):
        harness.write(sid, "")


@needs_posix
def test_tty_process_sees_a_terminal_and_a_pipe_process_has_no_stdin(harness):
    output = harness.start("test -t 0 && test -t 1 && echo terminal", tty=True)
    assert "terminal" in output_text(output) and exit_code(output) == 0
    output = harness.start("test -t 0 || echo no-terminal; sleep 30")
    assert "no-terminal" in output_text(output)
    with pytest.raises(ToolError, match="stdin is closed for this session"):
        harness.write(session_id(output), "input\n")


@needs_posix
def test_empty_chars_polls_until_the_process_exits_and_returns_new_output_once(harness):
    output = harness.start("echo first; sleep 1; echo second", yield_time_ms=500)
    assert "first" in output_text(output)
    started = time.monotonic()
    output = harness.write(session_id(output), "")
    # The poll waits up to 5 seconds and returns when the process exits.
    assert time.monotonic() - started < 4
    text = output_text(output)
    assert exit_code(output) == 0 and "second" in text and "first" not in text


@needs_posix
@pytest.mark.parametrize("tty", [False, True])
def test_ctrl_c_interrupts_the_process_group(harness, tty):
    command = "trap 'echo interrupted' INT; while true; do sleep 0.1; done"
    sid = session_id(harness.start(command, tty=tty))
    output = harness.write(sid, "\x03")
    assert "interrupted" in output_text(output)
    assert session_id(output) == sid
    # A process without a handler stops.
    plain = harness.store.get(session_id(harness.start("sleep 30")), "session-a")
    output = harness.write(plain.id, "\x03")
    assert exit_code(output) not in (None, 0) and plain.id not in harness.store.ids()


@needs_posix
def test_max_output_tokens_truncates_the_output(harness):
    output = harness.start("head -c 4000 /dev/zero | tr '\\0' a", max_output_tokens=100)
    assert "Original token count: 1000" in output
    text = output_text(output)
    assert len(text) < 600 and "omitted" in text


@needs_posix
def test_approved_escalation_starts_the_process(root, tmp_path):
    h = Harness(root, tmp_path, mode=SandboxMode.WORKSPACE_WRITE, approved=True)
    try:
        output = h.start("echo started", escalate=True, justification="Run outside the sandbox.")
    finally:
        h.close()
    assert exit_code(output) == 0 and "started" in output_text(output)
    [request] = h.reviewer.requests
    assert request.tool == "exec_command" and request.escalate and not request.tty


# --- sandboxed processes ----------------------------------------------------------------


def test_sandboxed_exit_code(sandboxed, root):
    output = sandboxed.start("echo inside > out.txt; exit 7")
    assert exit_code(output) == 7 and (root / "out.txt").read_text() == "inside\n"
    assert sandboxed.reviewer.requests == []


def test_sandboxed_process_cannot_write_protected_paths(sandboxed, root):
    output = sandboxed.start("echo x > .git/config")
    assert exit_code(output) != 0
    assert "Read-only file system" in output_text(output)
    assert not (root / ".git" / "config").exists()


def test_sandboxed_tty_session(sandboxed):
    sid = session_id(sandboxed.start("cat", tty=True))
    assert "ping" in output_text(sandboxed.write(sid, "ping\n"))
    assert exit_code(sandboxed.write(sid, "\x04")) == 0


def test_sandbox_setup_failure_is_reported(sandboxed, root):
    (root / ".git").rmdir()
    (root / ".git").symlink_to(root / "elsewhere")
    with pytest.raises(ToolError, match="command not run: protected path .* is a symlink"):
        sandboxed.start("true")
    assert len(sandboxed.store) == 0
