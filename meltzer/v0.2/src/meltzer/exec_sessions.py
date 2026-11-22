"""Interactive processes: commands that keep running between tool calls.

`exec_command` starts a process, in the sandbox or directly, with a terminal
(PTY) or with pipes, and returns the output it writes until the yield time
ends. A process that is still running then stays in the session's
ProcessStore under a session id; `write_stdin` sends it input and collects
more output. A process belongs to the session that started it; only that
session can find it. The store holds at most MAX_PROCESSES processes; a
session's processes are stopped when that session ends.
"""

from __future__ import annotations

import codecs
import json
import os
import secrets
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Protocol

from meltzer.exec import (
    OUTPUT_DRAIN_SECONDS,
    POLL_SECONDS,
    TERMINATE_GRACE_SECONDS,
    ProcessRunner,
    group_alive,
    read_available,
    signal_group,
)
from meltzer.network.policy import NetworkApprovals
from meltzer.network.proxy import NetworkProxy
from meltzer.sandbox.bwrap import SandboxSetupError, exit_code_from_status
from meltzer.sandbox.policy import SandboxPolicy

# Output kept between two reads of a process: the first and the last half.
OUTPUT_MAX_BYTES = 1024 * 1024
MAX_PROCESSES = 64
# Never pruned to make room for a new process.
PROTECTED_RECENT_PROCESSES = 8

DEFAULT_EXEC_YIELD_TIME_MS = 10_000
DEFAULT_WRITE_YIELD_TIME_MS = 250
MIN_YIELD_TIME_MS = 250
MAX_YIELD_TIME_MS = 30_000
# A write_stdin call without input (a poll) waits at least this long.
MIN_POLL_YIELD_TIME_MS = 5_000

CTRL_C = "\x03"

# Set for every interactive process, so programs write plain text and do not start a pager.
PROCESS_ENV = {
    "NO_COLOR": "1",
    "TERM": "dumb",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PAGER": "cat",
    "GIT_PAGER": "cat",
    "GH_PAGER": "cat",
}


def exec_yield_seconds(yield_time_ms: int | None) -> float:
    """How long exec_command waits for output, from the requested yield time."""
    ms = DEFAULT_EXEC_YIELD_TIME_MS if yield_time_ms is None else yield_time_ms
    return min(max(ms, MIN_YIELD_TIME_MS), MAX_YIELD_TIME_MS) / 1000


def write_yield_seconds(yield_time_ms: int | None, chars: str, max_timeout_ms: int) -> float:
    """How long write_stdin waits for output. Polls (empty `chars`) may wait up to `max_timeout_ms`."""
    ms = DEFAULT_WRITE_YIELD_TIME_MS if yield_time_ms is None else yield_time_ms
    ms = max(ms, MIN_YIELD_TIME_MS)
    if chars:
        ms = min(ms, MAX_YIELD_TIME_MS)
    else:
        ms = min(max(ms, MIN_POLL_YIELD_TIME_MS), max_timeout_ms)
    return ms / 1000


def child_pid_from_status(data: bytes) -> int | None:
    """Pid of the sandboxed command, from bwrap's status output."""
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("child-pid"), int):
            return obj["child-pid"]
    return None


class HeadTailBuffer:
    """Output of a process since it was last drained: the first and last max_bytes / 2 bytes."""

    def __init__(self, max_bytes: int = OUTPUT_MAX_BYTES):
        self.head_limit = max_bytes // 2
        self.tail_limit = max_bytes - self.head_limit
        self._head = bytearray()
        self._tail = bytearray()
        self._omitted = 0

    def append(self, data: bytes) -> None:
        room = self.head_limit - len(self._head)
        if room > 0:
            self._head += data[:room]
            data = data[room:]
        if not data:
            return
        self._tail += data
        excess = len(self._tail) - self.tail_limit
        if excess > 0:
            del self._tail[:excess]
            self._omitted += excess

    def drain(self) -> bytes:
        data = bytes(self._head)
        if self._omitted:
            data += f"\n[... {self._omitted} bytes omitted ...]\n".encode()
        data += bytes(self._tail)
        self._head.clear()
        self._tail.clear()
        self._omitted = 0
        return data


class InteractiveProcess:
    """A command started by exec_command, with a thread that reads its output."""

    def __init__(
        self,
        process_id: int,
        owner: str,
        command: str,
        process: subprocess.Popen,
        *,
        output_fd: int,
        tty: bool,
        status_fd: int | None,
        proxy: NetworkProxy | None = None,
    ):
        self.id = process_id
        # The id of the session that started the process.
        self.owner = owner
        self.command = command
        self.process = process
        self.tty = tty
        self.sandboxed = status_fd is not None
        self.last_used = time.monotonic()
        # Held by write_stdin while it writes and collects output.
        self.lock = threading.Lock()
        # True when bwrap exited without starting the command.
        self.setup_failed = False
        self._output_fd: int | None = output_fd
        # Read end of bwrap's status pipe, kept open until the process exits.
        self._status_fd = status_fd
        self._proxy = proxy
        self._status = b""
        self._exit_code: int | None = None
        self._buffer = HeadTailBuffer()
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._condition = threading.Condition()
        self._eof = False
        self._reader = threading.Thread(target=self._read_output, name=f"exec-{process_id}", daemon=True)
        self._reader.start()

    @classmethod
    def start(
        cls,
        runner: ProcessRunner,
        process_id: int,
        owner: str,
        command: str,
        cwd: Path,
        *,
        sandboxed: bool,
        policy: SandboxPolicy | None,
        tty: bool,
        network: NetworkApprovals | None = None,
    ) -> InteractiveProcess:
        """Start `command` with the runner's argv. Raises SandboxSetupError if the sandbox could not start it.

        `network`: the session's network approvals, for a command that uses the network proxy.
        """
        argv, status_read, status_write, proxy = runner.build_argv(command, cwd, sandboxed, policy, network)
        if tty:
            # pty is only available on POSIX systems.
            import pty

            output_fd, child_fd = pty.openpty()
            stdin: int = child_fd
        else:
            output_fd, child_fd = os.pipe()
            stdin = subprocess.DEVNULL
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env={**os.environ, **PROCESS_ENV},
                stdin=stdin,
                stdout=child_fd,
                stderr=child_fd,
                # Own process group, so an interrupt or session end reaches the whole tree.
                start_new_session=True,
                pass_fds=(status_write,) if status_write is not None else (),
            )
        except OSError as exc:
            os.close(output_fd)
            if proxy is not None:
                proxy.close()
            if status_read is not None:
                os.close(status_read)
                raise SandboxSetupError(f"failed to start sandbox: {exc}") from exc
            raise
        finally:
            os.close(child_fd)
            if status_write is not None:
                os.close(status_write)
        return cls(process_id, owner, command, process, output_fd=output_fd, tty=tty, status_fd=status_read, proxy=proxy)

    def _read_output(self) -> None:
        assert self._output_fd is not None
        while True:
            try:
                chunk = os.read(self._output_fd, 65536)
            except OSError:
                # A terminal returns EIO once every process has closed its side.
                chunk = b""
            with self._condition:
                if chunk:
                    self._buffer.append(chunk)
                else:
                    self._eof = True
                self._condition.notify_all()
            if not chunk:
                return

    def _read_status(self) -> None:
        if self._status_fd is not None:
            self._status += read_available(self._status_fd)

    def poll(self) -> int | None:
        """The exit code, or None while the process runs."""
        with self._condition:
            if self._exit_code is None and self.process.poll() is not None:
                code = self.process.returncode
                if self._status_fd is not None:
                    self._read_status()
                    os.close(self._status_fd)
                    self._status_fd = None
                    reported = exit_code_from_status(self._status)
                    if reported is not None:
                        code = reported
                    elif code >= 0:
                        self.setup_failed = True
                        if self._proxy is not None:
                            self._proxy.close()
                self._exit_code = code
            return self._exit_code

    def has_exited(self) -> bool:
        return self.poll() is not None

    def write(self, data: bytes) -> None:
        """Write to the process's terminal."""
        if not self.tty or self._output_fd is None:
            raise OSError("the process has no terminal")
        view = memoryview(data)
        while view:
            written = os.write(self._output_fd, view)
            view = view[written:]

    def _command_group(self) -> int:
        """Process group of the command. In the sandbox the command runs in a session of its own."""
        if self._status_fd is not None:
            self._read_status()
            pid = child_pid_from_status(self._status)
            if pid is not None:
                return pid
        return self.process.pid

    def interrupt(self) -> None:
        """Send SIGINT to the command's process group, as Ctrl-C in a terminal does."""
        with self._condition:
            if self._exit_code is None:
                signal_group(self._command_group(), signal.SIGINT)

    def collect(self, deadline: float) -> str:
        """Output written since the last call. Waits until `deadline`, or until the process exits."""
        drain_deadline: float | None = None
        with self._condition:
            while True:
                now = time.monotonic()
                if drain_deadline is None and self.poll() is not None:
                    drain_deadline = now + OUTPUT_DRAIN_SECONDS
                if drain_deadline is not None and self._eof:
                    break
                limit = drain_deadline if drain_deadline is not None else deadline
                if now >= limit:
                    break
                self._condition.wait(min(limit - now, POLL_SECONDS))
            data = self._buffer.drain()
            final = self._eof and self._exit_code is not None
        return self._decoder.decode(data, final=final)

    def terminate(self) -> None:
        """Stop the process with its process group; kill it after TERMINATE_GRACE_SECONDS."""
        if self.poll() is None:
            with self._condition:
                groups = [self.process.pid]
                if self.sandboxed:
                    groups.append(self._command_group())
            for group in groups:
                signal_group(group, signal.SIGTERM)
            deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
            try:
                self.process.wait(timeout=TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass
            for group in groups:
                while group_alive(group) and time.monotonic() < deadline:
                    time.sleep(POLL_SECONDS)
                signal_group(group, signal.SIGKILL)
            self.process.wait()
            self.poll()
        self.close()

    def close(self) -> None:
        """Close the output and status descriptors of an exited process."""
        self._reader.join(timeout=OUTPUT_DRAIN_SECONDS)
        with self._condition:
            # A process the command left running may still hold the output open;
            # the descriptor stays with the reader thread then.
            if self._output_fd is not None and not self._reader.is_alive():
                os.close(self._output_fd)
                self._output_fd = None
            if self._status_fd is not None:
                os.close(self._status_fd)
                self._status_fd = None

    def remaining_group(self) -> int | None:
        """The process group, if processes the command started are still running after it exited."""
        if self.process.returncode is not None and group_alive(self.process.pid):
            return self.process.pid
        return None


class StoredProcess(Protocol):
    id: int
    owner: str
    last_used: float

    def has_exited(self) -> bool: ...

    def terminate(self) -> None: ...

    def remaining_group(self) -> int | None: ...


class ProcessStore:
    """The interactive processes of a session and its subagents, by process id."""

    def __init__(self, max_processes: int = MAX_PROCESSES):
        self.max_processes = max_processes
        self._processes: dict[int, StoredProcess] = {}
        # Ids handed out for processes that are being started.
        self._reserved: set[int] = set()
        # Owner and process group of exited processes that still had processes running.
        self._leftover_groups: list[tuple[str, int]] = []
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._processes)

    def ids(self) -> list[int]:
        with self._lock:
            return list(self._processes)

    def allocate_id(self) -> int:
        with self._lock:
            while True:
                process_id = 1_000 + secrets.randbelow(99_000)
                if process_id not in self._processes and process_id not in self._reserved:
                    self._reserved.add(process_id)
                    return process_id

    def release(self, process_id: int) -> None:
        """Give back an id from allocate_id whose process was not started."""
        with self._lock:
            self._reserved.discard(process_id)

    def add(self, process: StoredProcess) -> None:
        """Store a process. When the store is full, one older process is stopped and removed first."""
        pruned = None
        with self._lock:
            if len(self._processes) >= self.max_processes:
                pruned = self._processes.pop(self._prune_candidate())
            process.last_used = time.monotonic()
            self._reserved.discard(process.id)
            self._processes[process.id] = process
        if pruned is not None:
            self._discard(pruned)

    def _prune_candidate(self) -> int:
        """Least recently used process outside the most recent ones; exited processes first."""
        by_recency = sorted(self._processes.values(), key=lambda p: p.last_used, reverse=True)
        protected = {p.id for p in by_recency[:PROTECTED_RECENT_PROCESSES]}
        candidates = [p for p in reversed(by_recency) if p.id not in protected] or list(reversed(by_recency))
        for process in candidates:
            if process.has_exited():
                return process.id
        return candidates[0].id

    def get(self, process_id: int, owner: str) -> StoredProcess:
        """The process with this id that `owner` started, marked as used now. Raises KeyError if there is none."""
        with self._lock:
            process = self._processes[process_id]
            if process.owner != owner:
                raise KeyError(process_id)
            process.last_used = time.monotonic()
            return process

    def remove(self, process_id: int) -> None:
        with self._lock:
            process = self._processes.pop(process_id, None)
        if process is not None:
            self._discard(process)

    def _discard(self, process: StoredProcess) -> None:
        process.terminate()
        group = process.remaining_group()
        if group is not None:
            with self._lock:
                self._leftover_groups.append((process.owner, group))

    def terminate_owner(self, owner: str) -> None:
        """Stop the processes a session started, and what its exited processes left running."""
        with self._lock:
            processes = [p for p in self._processes.values() if p.owner == owner]
            for process in processes:
                del self._processes[process.id]
        for process in processes:
            self._discard(process)
        with self._lock:
            groups = [group for name, group in self._leftover_groups if name == owner]
            self._leftover_groups = [entry for entry in self._leftover_groups if entry[0] != owner]
        _stop_groups(groups)

    def terminate_all(self) -> None:
        """Stop every process, and what exited processes left running. Called when the session ends."""
        with self._lock:
            processes = list(self._processes.values())
            self._processes.clear()
        for process in processes:
            self._discard(process)
        with self._lock:
            groups = [group for _, group in self._leftover_groups]
            self._leftover_groups = []
        _stop_groups(groups)


def _stop_groups(groups: list[int]) -> None:
    for group in groups:
        signal_group(group, signal.SIGTERM)
    deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
    for group in groups:
        while group_alive(group) and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
        signal_group(group, signal.SIGKILL)
