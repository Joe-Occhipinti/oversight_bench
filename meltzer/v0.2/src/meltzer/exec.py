"""Runs shell commands, in the sandbox or directly, in the foreground or background.

A sandboxed command whose policy has network mode proxy gets a network proxy
of its own (meltzer.network), which checks every request of the command
with the network approvals of the session that started it.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meltzer.network.bridge import bridge_command
from meltzer.network.policy import NetworkApprovals, NetworkPolicy
from meltzer.network.proxy import SOCKET_NAME, NetworkProxy
from meltzer.sandbox import SandboxBackend, select_backend
from meltzer.sandbox.bwrap import PROXY_SOCKET_DIR, SandboxSetupError, build_bwrap_args, exit_code_from_status
from meltzer.sandbox.policy import NetworkMode, SandboxPolicy
from meltzer.sandbox.seatbelt import seatbelt_command

if TYPE_CHECKING:
    from meltzer.telemetry.events import SessionTelemetry

DEFAULT_TIMEOUT_SECONDS = 300.0
# A background process that exits this quickly is checked for sandbox setup errors.
BACKGROUND_STARTUP_CHECK_SECONDS = 0.25
TERMINATE_GRACE_SECONDS = 2.0
# After a foreground command exits, its output is read for at most this long.
# A process it started in the background may keep the output pipe open.
OUTPUT_DRAIN_SECONDS = 1.0
# How often the reader checks whether the command has exited.
POLL_SECONDS = 0.1


@dataclass
class ExecResult:
    exit_code: int
    output: str
    duration: float
    timed_out: bool = False


@dataclass
class BackgroundProcess:
    pid: int
    log_path: Path
    process: subprocess.Popen = field(repr=False)
    # Read end of bwrap's status pipe, kept open until the process is stopped
    # so bwrap can still report the exit code.
    status_fd: int | None = field(default=None, repr=False)


def _shell_argv(command: str) -> list[str]:
    return ["bash", "-c", command]


def read_available(fd: int) -> bytes:
    """Read what is in the pipe now; a descendant may still hold the write end open."""
    os.set_blocking(fd, False)
    chunks = []
    while True:
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def setup_failure(output: str, returncode: int) -> SandboxSetupError:
    detail = output.strip() or f"bwrap exited with status {returncode}"
    return SandboxSetupError(f"sandbox setup failed: {detail}")


def signal_group(group: int, sig: int) -> None:
    try:
        os.killpg(group, sig)
    except (ProcessLookupError, PermissionError):
        # The group is gone, or only processes we may not signal are left in it.
        return


def _kill_group(process: subprocess.Popen, sig: int) -> None:
    signal_group(process.pid, sig)


def group_alive(group: int) -> bool:
    """Whether the process group still has processes that we may signal."""
    try:
        os.killpg(group, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _collect_output(process: subprocess.Popen, timeout: float) -> tuple[bytes, bool]:
    """Read the output of a running command until it exits. Returns (output, timed out).

    On timeout the command's process group is killed; output read until then is kept.
    """
    assert process.stdout is not None
    fd = process.stdout.fileno()
    os.set_blocking(fd, False)
    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout
    drain_deadline: float | None = None
    timed_out = False
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while True:
            now = time.monotonic()
            if drain_deadline is None and process.poll() is not None:
                drain_deadline = now + OUTPUT_DRAIN_SECONDS
            limit = drain_deadline if drain_deadline is not None else deadline
            if now >= limit:
                timed_out = drain_deadline is None
                break
            if not selector.select(min(limit - now, POLL_SECONDS)):
                continue
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                break
            chunks.append(chunk)
    if not timed_out and process.poll() is None:
        # The command closed its output but is still running.
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
    if timed_out:
        _kill_group(process, signal.SIGKILL)
        chunks.append(read_available(fd))
    process.stdout.close()
    process.wait()
    return b"".join(chunks), timed_out


class ProcessRunner:
    def __init__(self, policy: SandboxPolicy, log_dir: Path, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.policy = policy
        self.log_dir = log_dir
        self.timeout = timeout
        self.background: list[BackgroundProcess] = []
        # Process groups of foreground commands that still had processes
        # running (started in the background) when the command returned.
        self.leftover_groups: list[int] = []
        # Network proxies of proxy-mode commands; each stops when its command ends.
        self.proxies: list[NetworkProxy] = []
        # Subagents share the runner and may start commands at the same time.
        self._lock = threading.Lock()
        self._log_count = 0
        # Records a sandbox outcome event for each command run to completion.
        self.telemetry: SessionTelemetry | None = None

    def build_argv(
        self,
        command: str,
        cwd: Path,
        sandboxed: bool,
        policy: SandboxPolicy | None = None,
        network: NetworkApprovals | None = None,
    ) -> tuple[list[str], int | None, int | None, NetworkProxy | None]:
        """Return (argv, status read fd, status write fd, network proxy). `policy` replaces the runner's policy for this command.

        A sandboxed command with network mode proxy starts with a network proxy
        that decides its requests with `network`, the network approvals of the
        session that runs it; without them, no host is reachable. On macOS
        (Seatbelt) there are no status fds and network mode proxy is refused.
        """
        if not sandboxed:
            return _shell_argv(command), None, None, None
        policy = policy or self.policy
        argv_command = _shell_argv(command)
        if select_backend() == SandboxBackend.SEATBELT:
            if policy.network == NetworkMode.PROXY:
                raise SandboxSetupError("network mode proxy needs the Linux sandbox; it is not available on macOS")
            return seatbelt_command(policy, argv_command, cwd), None, None, None
        proxy = None
        if policy.network == NetworkMode.PROXY:
            proxy = self._start_proxy(command, cwd, network)
            argv_command = bridge_command(argv_command, PROXY_SOCKET_DIR / SOCKET_NAME)
        read_fd, write_fd = os.pipe()
        try:
            argv = build_bwrap_args(
                policy, argv_command, cwd, write_fd, proxy_dir=proxy.directory if proxy is not None else None
            )
        except SandboxSetupError:
            os.close(read_fd)
            os.close(write_fd)
            if proxy is not None:
                proxy.close()
            raise
        return argv, read_fd, write_fd, proxy

    def _start_proxy(self, command: str, cwd: Path, network: NetworkApprovals | None) -> NetworkProxy:
        approvals = network if network is not None else NetworkApprovals(NetworkPolicy())
        try:
            proxy = NetworkProxy.start(approvals, command, cwd)
        except OSError as exc:
            raise SandboxSetupError(f"cannot start the network proxy: {exc}") from exc
        with self._lock:
            self.proxies = [running for running in self.proxies if not running.closed]
            self.proxies.append(proxy)
        return proxy

    def _spawn(
        self,
        argv: list[str],
        cwd: Path,
        stdout: Any,
        status_read: int | None,
        status_write: int | None,
        proxy: NetworkProxy | None,
    ) -> subprocess.Popen:
        try:
            return subprocess.Popen(
                argv,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=subprocess.STDOUT,
                # Own process group, so a timeout or session end can stop the whole tree.
                start_new_session=True,
                pass_fds=(status_write,) if status_write is not None else (),
            )
        except OSError as exc:
            if proxy is not None:
                proxy.close()
            if status_read is not None:
                os.close(status_read)
                raise SandboxSetupError(f"failed to start sandbox: {exc}") from exc
            raise
        finally:
            if status_write is not None:
                os.close(status_write)

    def run(
        self,
        command: str,
        cwd: Path,
        sandboxed: bool,
        policy: SandboxPolicy | None = None,
        network: NetworkApprovals | None = None,
    ) -> ExecResult:
        """Run a command to completion. Raises SandboxSetupError if the sandbox could not start it."""
        argv, status_read, status_write, proxy = self.build_argv(command, cwd, sandboxed, policy, network)
        started = time.monotonic()
        process = self._spawn(argv, cwd, subprocess.PIPE, status_read, status_write, proxy)

        try:
            raw, timed_out = _collect_output(process, self.timeout)
        except BaseException:
            # Interrupted (for example Ctrl-C): stop the command before giving up on it.
            _kill_group(process, signal.SIGKILL)
            process.wait()
            if status_read is not None:
                os.close(status_read)
            raise
        duration = time.monotonic() - started
        output = raw.decode("utf-8", errors="replace")
        if group_alive(process.pid):
            with self._lock:
                self.leftover_groups.append(process.pid)

        exit_code = process.returncode
        if status_read is not None:
            status = read_available(status_read)
            os.close(status_read)
            reported = exit_code_from_status(status)
            if reported is None and not timed_out:
                if proxy is not None:
                    proxy.close()
                raise setup_failure(output, exit_code)
            if reported is not None:
                exit_code = reported
        if self.telemetry is not None:
            self.telemetry.sandbox_outcome(sandboxed=sandboxed, exit_code=exit_code, duration=duration)
        return ExecResult(exit_code=exit_code, output=output, duration=duration, timed_out=timed_out)

    def start_background(
        self,
        command: str,
        cwd: Path,
        sandboxed: bool,
        policy: SandboxPolicy | None = None,
        network: NetworkApprovals | None = None,
    ) -> BackgroundProcess:
        """Start a command and return at once. Output goes to a log file."""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        with self._lock:
            self._log_count += 1
            log_path = self.log_dir / f"bg-{self._log_count}-{stamp}.log"
        argv, status_read, status_write, proxy = self.build_argv(command, cwd, sandboxed, policy, network)
        with log_path.open("wb") as log:
            process = self._spawn(argv, cwd, log, status_read, status_write, proxy)

        exited_early = True
        try:
            process.wait(timeout=BACKGROUND_STARTUP_CHECK_SECONDS)
        except subprocess.TimeoutExpired:
            exited_early = False
        if exited_early and status_read is not None:
            status = read_available(status_read)
            os.close(status_read)
            status_read = None
            if exit_code_from_status(status) is None:
                if proxy is not None:
                    proxy.close()
                raise setup_failure(log_path.read_text(errors="replace"), process.returncode)

        entry = BackgroundProcess(
            pid=process.pid,
            log_path=log_path,
            process=process,
            status_fd=status_read,
        )
        with self._lock:
            self.background.append(entry)
        return entry

    def terminate_all(self) -> None:
        """Stop the processes this session started, with their process groups.

        That is every background command, and what foreground commands left
        running. Groups still alive after TERMINATE_GRACE_SECONDS are killed.
        Network proxies that are still running are stopped.
        """
        groups = [entry.process.pid for entry in self.background] + self.leftover_groups
        for group in groups:
            signal_group(group, signal.SIGTERM)
        deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
        for entry in self.background:
            try:
                entry.process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                continue
        for group in groups:
            while group_alive(group) and time.monotonic() < deadline:
                time.sleep(POLL_SECONDS)
            signal_group(group, signal.SIGKILL)
        for entry in self.background:
            entry.process.wait()
            if entry.status_fd is not None:
                os.close(entry.status_fd)
                entry.status_fd = None
        self.leftover_groups.clear()
        with self._lock:
            proxies, self.proxies = self.proxies, []
        for proxy in proxies:
            proxy.close()
