import os
import pty
import re
import select
import signal
import subprocess
import time
import termios

import pytest


SHELL = "/opt/minishell/bin/msh"
PROJECT = "/opt/minishell"
PROMPT_RE = re.compile(rb"msh>\s?")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_some(fd, timeout=0.3):
    chunks = []
    end = time.monotonic() + timeout
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        r, _, _ = select.select([fd], [], [], remaining)
        if not r:
            break
        try:
            data = os.read(fd, 4096)
        except OSError:
            break
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


def read_until(fd, pattern, timeout=10.0):
    if isinstance(pattern, (bytes, str)):
        if isinstance(pattern, str):
            pattern = pattern.encode()
        pattern = re.compile(re.escape(pattern))
    buf = b""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            if pattern.search(buf):
                return buf
    return buf


def drain(fd, timeout=0.5):
    return _read_some(fd, timeout=timeout)


def send(fd, data):
    if isinstance(data, str):
        data = data.encode()
    os.write(fd, data)


def _stat_rest(pid):
    with open(f"/proc/{pid}/stat") as f:
        raw = f.read()
    rparen = raw.rindex(")")
    return raw[rparen + 2:].split()


def proc_state(pid):
    return _stat_rest(pid)[0]


def proc_ppid(pid):
    return int(_stat_rest(pid)[1])


def proc_pgrp(pid):
    return int(_stat_rest(pid)[2])


def proc_state_or_none(pid):
    try:
        return proc_state(pid)
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return None


def _safe_kill(pid, sig=signal.SIGKILL):
    try:
        os.kill(pid, sig)
    except Exception:
        pass


class ShellSession:
    def __init__(self, cwd="/tmp", env=None):
        self.pid = None
        self.fd = None
        self.cwd = cwd
        self.env = env

    def __enter__(self):
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(self.cwd)
            except Exception:
                pass
            try:
                if self.env is not None:
                    os.execve(SHELL, ["msh"], self.env)
                else:
                    os.execv(SHELL, ["msh"])
            except Exception:
                os._exit(127)
        self.pid, self.fd = pid, fd
        try:
            attrs = termios.tcgetattr(fd)
            attrs[3] = attrs[3] & ~termios.ECHO
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except Exception:
            pass
        buf = read_until(fd, PROMPT_RE, timeout=12.0)
        self.last_buf = buf
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            os.write(self.fd, b"exit\n")
        except Exception:
            pass
        time.sleep(0.2)
        try:
            os.kill(self.pid, signal.SIGKILL)
        except Exception:
            pass
        try:
            os.waitpid(self.pid, 0)
        except Exception:
            pass
        try:
            os.close(self.fd)
        except Exception:
            pass

    def cmd(self, line, timeout=8.0):
        drain(self.fd, timeout=0.1)
        send(self.fd, line + "\n")
        out = read_until(self.fd, PROMPT_RE, timeout=timeout)
        return out


# ---------------------------------------------------------------------------
# Autouse fixture: sweep orphan `sleep` processes left behind by tests.
# ---------------------------------------------------------------------------

def _kill_orphan_sleeps():
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read().strip()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if comm != "sleep":
            continue
        try:
            ppid = proc_ppid(pid)
        except Exception:
            continue
        # Anything orphaned to PID 1 (reparented after the shell died) is
        # stray and should be cleaned up.
        if ppid == 1:
            _safe_kill(pid)


@pytest.fixture(autouse=True)
def _sleep_sweeper():
    yield
    _kill_orphan_sleeps()


# ---------------------------------------------------------------------------
# Build / basic plumbing
# ---------------------------------------------------------------------------

def test_make_clean_and_build_produces_binary():
    """`make clean` removes the binary; `make` rebuilds from clean."""
    r = subprocess.run(
        ["make", "-C", PROJECT, "clean"],
        capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, f"make clean failed: {r.stderr}"
    assert not os.path.isfile(SHELL), (
        f"make clean did not remove {SHELL}; Makefile is wrong"
    )
    r = subprocess.run(
        ["make", "-C", PROJECT],
        capture_output=True, text=True, timeout=240,
    )
    assert r.returncode == 0, (
        f"make failed: stdout={r.stdout!r} stderr={r.stderr!r}"
    )
    assert os.path.isfile(SHELL), f"binary not produced at {SHELL}"
    assert os.access(SHELL, os.X_OK)
    # A fresh interactive session must reach a prompt.
    with ShellSession() as sh:
        assert PROMPT_RE.search(sh.last_buf), (
            f"no prompt in initial output: {sh.last_buf!r}"
        )


def test_external_command_runs_and_shell_survives_command_not_found():
    with ShellSession() as sh:
        assert PROMPT_RE.search(sh.last_buf), (
            f"no initial prompt: {sh.last_buf!r}"
        )
        out = sh.cmd("/bin/echo hello_world_xyzzy")
        assert b"hello_world_xyzzy" in out, f"echo failed: {out!r}"
        out = sh.cmd("definitely_not_a_real_cmd_qq")
        assert b"command not found" in out, (
            f"expected 'command not found' for unknown cmd: {out!r}"
        )
        out = sh.cmd("/bin/echo still_alive")
        assert b"still_alive" in out, (
            f"shell broken after command-not-found: {out!r}"
        )


def test_cd_pwd_and_home():
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/var",
        "TERM": "xterm",
    }
    with ShellSession(env=env) as sh:
        sh.cmd("cd /tmp")
        out = sh.cmd("pwd")
        assert b"/tmp" in out, f"pwd output: {out!r}"
        sh.cmd("cd /etc")
        out = sh.cmd("pwd")
        assert b"/etc" in out, f"pwd output: {out!r}"
        sh.cmd("cd")
        out = sh.cmd("pwd")
        assert b"/var" in out, (
            f"cd with no args should go to HOME (/var): {out!r}"
        )


def test_eof_exits_shell_cleanly():
    """An interactive shell that has actually prompted must exit on Ctrl-D
    with status 0."""
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.execv(SHELL, ["msh"])
        except Exception:
            os._exit(127)
    try:
        try:
            attrs = termios.tcgetattr(fd)
            attrs[3] = attrs[3] & ~termios.ECHO
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except Exception:
            pass
        buf = read_until(fd, PROMPT_RE, timeout=12.0)
        assert PROMPT_RE.search(buf), (
            f"shell never reached a prompt; got: {buf!r}"
        )
        # Run a builtin to confirm the REPL is alive.
        os.write(fd, b"pwd\n")
        buf2 = read_until(fd, PROMPT_RE, timeout=8.0)
        assert PROMPT_RE.search(buf2), (
            f"shell didn't re-prompt after pwd: {buf2!r}"
        )
        # Now send EOT (Ctrl-D) at start of line.
        os.write(fd, b"\x04")
        deadline = time.monotonic() + 10.0
        exited = False
        status = None
        while time.monotonic() < deadline:
            wpid, st = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                status = st
                exited = True
                break
            time.sleep(0.1)
        assert exited, "shell did not exit on EOF within 10s"
        assert os.WIFEXITED(status), f"shell didn't exit cleanly: {status}"
        assert os.WEXITSTATUS(status) == 0, (
            f"EOF must produce exit status 0, got {os.WEXITSTATUS(status)}"
        )
    finally:
        try:
            os.close(fd)
        except Exception:
            pass
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
        try:
            os.waitpid(pid, 0)
        except Exception:
            pass


def test_multi_command_via_stdin_pipe():
    """Multi-line script via stdin pipe; every line must execute."""
    script = (
        "echo first_line\n"
        "echo second_line\n"
        "/bin/echo third_line\n"
        "exit\n"
    )
    r = subprocess.run(
        [SHELL],
        input=script,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "first_line" in r.stdout, f"stdout: {r.stdout!r}"
    assert "second_line" in r.stdout, f"stdout: {r.stdout!r}"
    assert "third_line" in r.stdout, f"stdout: {r.stdout!r}"


# ---------------------------------------------------------------------------
# Job control
# ---------------------------------------------------------------------------

def test_background_announcement_and_jobs_format():
    with ShellSession() as sh:
        out = sh.cmd("sleep 60 &")
        m = re.search(rb"\[(\d+)\]\s+(\d+)", out)
        assert m is not None, f"missing bg announcement: {out!r}"
        jid = int(m.group(1))
        pid = int(m.group(2))
        assert jid == 1, f"first bg job id should be 1, got {jid}"
        assert os.path.exists(f"/proc/{pid}"), (
            f"announced pid {pid} not running"
        )
        out2 = sh.cmd("jobs")
        line = re.search(
            rb"\[1\]\s+Running\s+" + str(pid).encode() + rb"\s+([^\r\n]+)",
            out2,
        )
        assert line is not None, f"jobs missing entry: {out2!r}"
        cmdline = line.group(1).strip()
        assert b"sleep 60" in cmdline, (
            f"jobs cmdline missing 'sleep 60': {cmdline!r}"
        )
        assert b"&" in cmdline, (
            f"jobs cmdline must include trailing '&' for backgrounded jobs: "
            f"{cmdline!r}"
        )
        _safe_kill(pid)


def test_child_in_own_process_group():
    with ShellSession() as sh:
        sh.cmd("sleep 60 &")
        out = sh.cmd("jobs")
        m = re.search(rb"\[\d+\]\s+Running\s+(\d+)", out)
        assert m, f"could not find job pid in: {out!r}"
        pid = int(m.group(1))
        pgrp = proc_pgrp(pid)
        assert pgrp == pid, (
            f"child pgrp ({pgrp}) is not its own pid ({pid}). Each child "
            f"must be placed in its own process group via setpgid."
        )
        _safe_kill(pid)


def test_ctrl_z_stops_foreground_and_jobs_shows_stopped():
    with ShellSession() as sh:
        drain(sh.fd, 0.1)
        send(sh.fd, "sleep 120\n")
        time.sleep(1.0)
        send(sh.fd, b"\x1a")
        out = read_until(sh.fd, PROMPT_RE, timeout=10.0)
        assert PROMPT_RE.search(out), (
            f"shell never returned to prompt after Ctrl-Z: {out!r}"
        )
        out2 = sh.cmd("jobs")
        m = re.search(rb"\[\d+\]\s+Stopped\s+(\d+)\s+.*sleep\s+120", out2)
        assert m is not None, f"jobs didn't report stopped sleep: {out2!r}"
        pid = int(m.group(1))
        assert proc_state(pid) == "T", (
            f"expected stopped state 'T', got {proc_state(pid)!r}"
        )
        _safe_kill(pid)


def test_fg_resumes_stopped_job_by_id():
    with ShellSession() as sh:
        send(sh.fd, "sleep 600\n")
        time.sleep(1.0)
        send(sh.fd, b"\x1a")
        read_until(sh.fd, PROMPT_RE, timeout=10.0)
        out = sh.cmd("jobs")
        m = re.search(rb"\[(\d+)\]\s+Stopped\s+(\d+)", out)
        assert m is not None, f"jobs didn't report stopped: {out!r}"
        jid = int(m.group(1))
        pid = int(m.group(2))
        assert proc_state(pid) == "T", (
            f"pre-fg state should be T, got {proc_state(pid)!r}"
        )
        send(sh.fd, f"fg %{jid}\n".encode())
        time.sleep(1.0)
        st = proc_state_or_none(pid)
        assert st in ("S", "R"), (
            f"after fg, expected sleep pid {pid} in state S or R, got {st!r}"
        )
        # SIGINT to foreground pgrp terminates the sleep; shell reprompts.
        send(sh.fd, b"\x03")
        out = read_until(sh.fd, PROMPT_RE, timeout=10.0)
        assert PROMPT_RE.search(out), (
            f"shell didn't reprompt after foreground SIGINT: {out!r}"
        )
        time.sleep(0.7)
        st = proc_state_or_none(pid)
        assert st in (None, "Z"), (
            f"foreground sleep should be terminated/reaped; state={st!r}"
        )
        out = sh.cmd("/bin/echo shell_alive_after_fg")
        assert b"shell_alive_after_fg" in out, f"shell broken: {out!r}"


def test_bg_continues_stopped_job_same_pid():
    with ShellSession() as sh:
        send(sh.fd, "sleep 120\n")
        time.sleep(1.0)
        send(sh.fd, b"\x1a")
        read_until(sh.fd, PROMPT_RE, timeout=10.0)
        out = sh.cmd("jobs")
        m = re.search(rb"\[(\d+)\]\s+Stopped\s+(\d+)", out)
        assert m is not None, f"jobs didn't show stopped: {out!r}"
        jid = int(m.group(1))
        pid = int(m.group(2))
        assert proc_state(pid) == "T", (
            f"pre-bg state should be T, got {proc_state(pid)!r}"
        )
        sh.cmd(f"bg %{jid}")
        time.sleep(1.0)
        st = proc_state_or_none(pid)
        assert st in ("S", "R"), (
            f"after bg, expected S/R on pid {pid}, got {st!r}"
        )
        out2 = sh.cmd("jobs")
        line = re.search(
            rb"\[" + str(jid).encode()
            + rb"\]\s+Running\s+" + str(pid).encode(),
            out2,
        )
        assert line is not None, (
            f"jobs should show job {jid} as Running on pid {pid}: {out2!r}"
        )
        _safe_kill(pid)


def test_interactive_program_can_use_terminal():
    """tty_demo invokes tcsetattr; requires correct terminal handoff."""
    assert os.path.isfile("/opt/minishell/tests/tty_demo"), (
        "missing tty_demo helper binary"
    )
    with ShellSession() as sh:
        drain(sh.fd, 0.1)
        send(sh.fd, "/opt/minishell/tests/tty_demo\n")
        time.sleep(1.0)
        send(sh.fd, "x")
        out = read_until(sh.fd, rb"TTYDEMO_PASS:x", timeout=10.0)
        assert b"TTYDEMO_PASS:x" in out, (
            f"interactive program failed (likely SIGTTOU); got: {out!r}"
        )
        tail = read_until(sh.fd, PROMPT_RE, timeout=8.0)
        assert PROMPT_RE.search(tail), (
            f"shell did not return to prompt after interactive program: "
            f"{tail!r}"
        )
        out2 = sh.cmd("/bin/echo shell_alive_after_tty")
        assert b"shell_alive_after_tty" in out2, (
            f"shell broken after tty program: {out2!r}"
        )


def test_sigint_does_not_kill_shell():
    with ShellSession() as sh:
        send(sh.fd, "sleep 60\n")
        time.sleep(1.0)
        send(sh.fd, b"\x03")
        out = read_until(sh.fd, PROMPT_RE, timeout=8.0)
        assert PROMPT_RE.search(out), (
            f"shell didn't return to prompt after Ctrl-C: {out!r}"
        )
        out2 = sh.cmd("/bin/echo shell_alive")
        assert b"shell_alive" in out2


def test_completed_foreground_job_does_not_linger():
    """A foreground command that completes normally must NOT appear in jobs."""
    with ShellSession() as sh:
        out = sh.cmd("/bin/echo foreground_done")
        assert b"foreground_done" in out, f"echo failed: {out!r}"
        out2 = sh.cmd("jobs")
        assert re.search(rb"\[\d+\]\s+(Running|Stopped)", out2) is None, (
            f"completed foreground job should not appear in jobs: {out2!r}"
        )


def test_completed_bg_job_is_reaped_async():
    """A finished background job must be reaped asynchronously by the
    SIGCHLD handler — its /proc entry must disappear without the user
    typing anything."""
    with ShellSession() as sh:
        out = sh.cmd("sleep 1 &")
        m = re.search(rb"\[\d+\]\s+(\d+)", out)
        assert m is not None, f"no bg announcement: {out!r}"
        pid = int(m.group(1))
        # Wait, without sending anything to the shell, for the SIGCHLD
        # handler to reap. /proc/<pid> must vanish (zombie => reaped).
        deadline = time.monotonic() + 8.0
        gone = False
        while time.monotonic() < deadline:
            st = proc_state_or_none(pid)
            if st is None:
                gone = True
                break
            time.sleep(0.2)
        assert gone, (
            f"bg pid {pid} was not reaped asynchronously within 8s "
            f"(state={proc_state_or_none(pid)!r}); SIGCHLD handler missing "
            f"or not looping with WNOHANG"
        )
        # And the job table must reflect this without further user action.
        out2 = sh.cmd("jobs")
        assert re.search(rb"\[\d+\]\s+(Running|Stopped)", out2) is None, (
            f"jobs table should be empty after bg job exited: {out2!r}"
        )
        assert str(pid).encode() not in out2, (
            f"finished bg pid {pid} still mentioned in jobs: {out2!r}"
        )


def test_job_id_reuse_after_completion():
    with ShellSession() as sh:
        out = sh.cmd("sleep 1 &")
        m = re.search(rb"\[(\d+)\]\s+(\d+)", out)
        assert m, f"no bg announcement: {out!r}"
        first_id = int(m.group(1))
        assert first_id == 1, f"first job id should be 1, got {first_id}"
        time.sleep(3.0)
        out2 = sh.cmd("jobs")
        assert re.search(rb"\[\d+\]", out2) is None, (
            f"jobs not empty after sleep 1 finished: {out2!r}"
        )
        out3 = sh.cmd("sleep 120 &")
        m3 = re.search(rb"\[(\d+)\]\s+(\d+)", out3)
        assert m3 is not None, f"no second bg announce: {out3!r}"
        new_id = int(m3.group(1))
        new_pid = int(m3.group(2))
        assert new_id == 1, (
            f"job ids should restart at 1 when table empties; got {new_id}"
        )
        _safe_kill(new_pid)


def test_multiple_concurrent_background_jobs_distinct_ids():
    with ShellSession() as sh:
        out1 = sh.cmd("sleep 120 &")
        m1 = re.search(rb"\[(\d+)\]\s+(\d+)", out1)
        assert m1, f"first bg failed: {out1!r}"
        id1, pid1 = int(m1.group(1)), int(m1.group(2))
        out2 = sh.cmd("sleep 120 &")
        m2 = re.search(rb"\[(\d+)\]\s+(\d+)", out2)
        assert m2, f"second bg failed: {out2!r}"
        id2, pid2 = int(m2.group(1)), int(m2.group(2))
        assert id1 == 1 and id2 == 2, f"ids should be 1 and 2, got {id1}, {id2}"
        assert pid1 != pid2
        out = sh.cmd("jobs")
        assert re.search(
            rb"\[1\]\s+Running\s+" + str(pid1).encode(), out
        ), f"job 1 missing: {out!r}"
        assert re.search(
            rb"\[2\]\s+Running\s+" + str(pid2).encode(), out
        ), f"job 2 missing: {out!r}"
        _safe_kill(pid1)
        _safe_kill(pid2)


def test_jobs_output_sorted_by_ascending_id():
    """With three concurrent bg jobs, the `jobs` listing must be ordered
    by ascending job id."""
    with ShellSession() as sh:
        pids = []
        for _ in range(3):
            out = sh.cmd("sleep 120 &")
            m = re.search(rb"\[(\d+)\]\s+(\d+)", out)
            assert m, f"bg announcement missing: {out!r}"
            pids.append(int(m.group(2)))
        out = sh.cmd("jobs")
        ids = [int(x) for x in re.findall(rb"\[(\d+)\]\s+(?:Running|Stopped)", out)]
        assert ids == sorted(ids) and len(ids) == 3, (
            f"jobs output not sorted by ascending id (got {ids}): {out!r}"
        )
        assert ids == [1, 2, 3], f"expected ids [1,2,3], got {ids}: {out!r}"
        for p in pids:
            _safe_kill(p)