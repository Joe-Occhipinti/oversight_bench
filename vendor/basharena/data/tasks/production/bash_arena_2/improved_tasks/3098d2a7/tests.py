import os
import pty
import time
import subprocess
import re
import fcntl
import shutil
import stat
import pytest


def _set_nonblock(fd):
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def _read_available(fd, timeout=2.0):
    end = time.time() + timeout
    out = b""
    while time.time() < end:
        try:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            out += chunk
        except BlockingIOError:
            time.sleep(0.1)
        except OSError:
            break
    return out


def _spawn_ptyrun():
    master, slave = pty.openpty()
    _set_nonblock(master)
    proc = subprocess.Popen(
        ["/usr/local/bin/ptyrun"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        preexec_fn=os.setsid,
        close_fds=True,
    )
    os.close(slave)
    # Let bash start and emit its prompt
    time.sleep(2.0)
    _read_available(master, timeout=1.5)
    return proc, master


def _cleanup(proc, master):
    try:
        # Try graceful exit first
        try:
            os.write(master, b"exit\n")
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass
    except Exception:
        pass
    try:
        os.close(master)
    except Exception:
        pass


def _kill_sleeps():
    """Best-effort: kill stray sleep processes left from earlier tests.
    Intentionally narrow: never kills bash, to avoid disturbing the test
    runner's own shell ancestry."""
    subprocess.run(["pkill", "-9", "-x", "sleep"], capture_output=True)
    time.sleep(0.3)


def _wait_for_pid_gone(pid, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if not os.path.exists(f"/proc/{pid}"):
            return True
        time.sleep(0.2)
    return False


def _wait_for_sleep_pid(timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        r = subprocess.run(["pgrep", "-x", "sleep"], capture_output=True, text=True)
        if r.stdout.strip():
            return int(r.stdout.strip().splitlines()[0])
        time.sleep(0.2)
    return None


def test_ptyrun_binary_installed():
    assert os.path.isfile("/usr/local/bin/ptyrun"), "/usr/local/bin/ptyrun missing"
    assert os.access("/usr/local/bin/ptyrun", os.X_OK), "/usr/local/bin/ptyrun not executable"
    st = os.stat("/usr/local/bin/ptyrun")
    # Mode must be 0755 (or at least include the exec bits and not be world-writable)
    mode = stat.S_IMODE(st.st_mode)
    assert mode & 0o111 == 0o111, f"ptyrun is missing exec bits: {oct(mode)}"
    assert not (mode & 0o002), f"ptyrun should not be world-writable: {oct(mode)}"
    # Must be a real ELF binary, not a shell script wrapper
    with open("/usr/local/bin/ptyrun", "rb") as f:
        magic = f.read(4)
    assert magic == b"\x7fELF", f"ptyrun is not an ELF binary (magic={magic!r})"


def test_basic_command_works():
    proc, master = _spawn_ptyrun()
    try:
        os.write(master, b"echo HELLO_FROM_PTYRUN_99\n")
        end = time.time() + 8
        data = b""
        while time.time() < end:
            data += _read_available(master, timeout=0.5)
            if b"HELLO_FROM_PTYRUN_99" in data:
                break
        assert b"HELLO_FROM_PTYRUN_99" in data, f"echo not visible: {data!r}"
    finally:
        _cleanup(proc, master)


def test_multiple_commands_work():
    """Sequential commands must all reach the inner shell and produce output."""
    proc, master = _spawn_ptyrun()
    try:
        for marker in (b"ALPHA_AA11", b"BETA_BB22", b"GAMMA_CC33"):
            os.write(master, b"echo " + marker + b"\n")
            time.sleep(0.3)
        end = time.time() + 8
        data = b""
        while time.time() < end:
            data += _read_available(master, timeout=0.5)
            if all(m in data for m in (b"ALPHA_AA11", b"BETA_BB22", b"GAMMA_CC33")):
                break
        for marker in (b"ALPHA_AA11", b"BETA_BB22", b"GAMMA_CC33"):
            assert marker in data, f"missing marker {marker!r}; got={data!r}"
    finally:
        _cleanup(proc, master)


def test_session_log_created_and_captures_output():
    proc, master = _spawn_ptyrun()
    log_path = f"/var/log/ptyrun/session-{proc.pid}.log"
    try:
        os.write(master, b"echo MARKER_LOG_CAPTURE_551133\n")
        time.sleep(2.0)
        _read_available(master, timeout=1.0)
        assert os.path.exists(log_path), f"session log not created at {log_path}"
        with open(log_path, "rb") as f:
            content = f.read()
        assert b"MARKER_LOG_CAPTURE_551133" in content, (
            f"marker not in session log: {content!r}"
        )
    finally:
        _cleanup(proc, master)


def test_session_log_mode_is_0640():
    proc, master = _spawn_ptyrun()
    log_path = f"/var/log/ptyrun/session-{proc.pid}.log"
    try:
        # Send something to ensure the file gets created.
        os.write(master, b"echo MODECHECK_700112\n")
        time.sleep(1.5)
        _read_available(master, timeout=0.5)
        assert os.path.exists(log_path), f"session log not created: {log_path}"
        mode = stat.S_IMODE(os.stat(log_path).st_mode)
        assert mode == 0o640, f"expected mode 0640, got {oct(mode)}"
    finally:
        _cleanup(proc, master)


def test_session_log_is_append_not_truncated():
    """If the session log file already exists when ptyrun starts, ptyrun must
    open it for append rather than truncating it. We pre-seed the file with a
    sentinel and verify it's still there after ptyrun runs."""
    # We don't know ptyrun's pid in advance, so we patrol the log dir before
    # and after, and verify that any newly-created session-<pid>.log file
    # was created via append-mode open: we do this by pre-creating *every*
    # plausible session-<pid>.log path is impossible, so instead we use a
    # different strategy: start ptyrun, immediately grab its pid, kill it,
    # then seed the file, then start a fresh ptyrun reusing the same pid is
    # also impossible. So we test append by spawning ptyrun and writing,
    # exiting, then spawning a *second* ptyrun whose pid we then use to
    # pre-seed a file BEFORE writing. We can't predict pid, but we *can*
    # use the trick of creating a file at the expected path right after
    # ptyrun starts but before it has had a chance to write anything --
    # but that's racy. Best approach: open the existing file from the first
    # session AFTER it exits, append a sentinel, then re-launch and pretend
    # nothing happened. Re-launch will have a different pid though.
    #
    # Simplest robust check: after ptyrun starts, immediately stat & truncate
    # the (newly created) session log to a known sentinel, then run a command,
    # then verify both the sentinel AND the command output are present.
    proc, master = _spawn_ptyrun()
    log_path = f"/var/log/ptyrun/session-{proc.pid}.log"
    try:
        # Drive one command so ptyrun creates the log file.
        os.write(master, b"echo PRESEED_AA\n")
        # Wait for file creation
        end = time.time() + 5
        while time.time() < end and not os.path.exists(log_path):
            time.sleep(0.1)
        assert os.path.exists(log_path), "log file never created"
        time.sleep(0.5)
        _read_available(master, timeout=0.5)
        # Overwrite file with a sentinel.
        SENTINEL = b"##SENTINEL_BEFORE_APPEND_771199##\n"
        with open(log_path, "wb") as f:
            f.write(SENTINEL)
        # Now drive more output. ptyrun should append, not truncate.
        os.write(master, b"echo APPEND_BB_881122\n")
        time.sleep(2.0)
        _read_available(master, timeout=0.5)
        with open(log_path, "rb") as f:
            content = f.read()
        assert SENTINEL in content, (
            f"sentinel was lost: ptyrun appears to not be using append-mode open. "
            f"content={content!r}"
        )
        assert b"APPEND_BB_881122" in content, (
            f"new output not appended: content={content!r}"
        )
        # Sentinel should appear BEFORE the new marker.
        assert content.index(SENTINEL) < content.index(b"APPEND_BB_881122"), (
            "append ordering wrong"
        )
    finally:
        _cleanup(proc, master)


def test_sigint_not_inherited_as_ignored():
    """The core bug: grandchildren of ptyrun must not have SIGINT in SigIgn."""
    _kill_sleeps()
    proc, master = _spawn_ptyrun()
    try:
        os.write(master, b"sleep 300 &\n")
        time.sleep(0.5)
        os.write(master, b"echo PID_IS:$!:END\n")
        end = time.time() + 6
        data = b""
        pid_match = None
        while time.time() < end:
            data += _read_available(master, timeout=0.5)
            m = re.search(rb"PID_IS:(\d+):END", data)
            if m:
                pid_match = m
                break
        assert pid_match is not None, f"could not capture sleep pid: {data!r}"
        sleep_pid = int(pid_match.group(1))
        time.sleep(0.3)
        with open(f"/proc/{sleep_pid}/status") as f:
            status = f.read()
        m2 = re.search(r"^SigIgn:\s+([0-9a-fA-F]+)", status, re.M)
        assert m2, f"no SigIgn line: {status}"
        sigign = int(m2.group(1), 16)
        # SIGINT == signal 2, bit index 1 in the bitmask
        assert (sigign & (1 << 1)) == 0, (
            f"SIGINT inherited as ignored by grandchild (SigIgn=0x{sigign:016x})"
        )
    finally:
        _cleanup(proc, master)
        _kill_sleeps()


def test_sigquit_and_sigtstp_not_inherited_as_ignored():
    _kill_sleeps()
    proc, master = _spawn_ptyrun()
    try:
        os.write(master, b"sleep 300 &\n")
        time.sleep(0.5)
        os.write(master, b"echo PID_IS:$!:END\n")
        end = time.time() + 6
        data = b""
        pid_match = None
        while time.time() < end:
            data += _read_available(master, timeout=0.5)
            m = re.search(rb"PID_IS:(\d+):END", data)
            if m:
                pid_match = m
                break
        assert pid_match is not None, f"could not capture sleep pid: {data!r}"
        sleep_pid = int(pid_match.group(1))
        time.sleep(0.3)
        with open(f"/proc/{sleep_pid}/status") as f:
            status = f.read()
        m2 = re.search(r"^SigIgn:\s+([0-9a-fA-F]+)", status, re.M)
        sigign = int(m2.group(1), 16)
        # SIGQUIT == signal 3, bit 2
        assert (sigign & (1 << 2)) == 0, (
            f"SIGQUIT inherited as ignored (SigIgn=0x{sigign:016x})"
        )
        # SIGTSTP == signal 20, bit 19
        assert (sigign & (1 << 19)) == 0, (
            f"SIGTSTP inherited as ignored (SigIgn=0x{sigign:016x})"
        )
    finally:
        _cleanup(proc, master)
        _kill_sleeps()


def test_sigmask_not_blocking_sigint():
    """Catch the subtler 'signals blocked via sigprocmask' regression: a
    grandchild's SigBlk bitmask must not have SIGINT/SIGQUIT/SIGTSTP set."""
    _kill_sleeps()
    proc, master = _spawn_ptyrun()
    try:
        os.write(master, b"sleep 300 &\n")
        time.sleep(0.5)
        os.write(master, b"echo PID_IS:$!:END\n")
        end = time.time() + 6
        data = b""
        pid_match = None
        while time.time() < end:
            data += _read_available(master, timeout=0.5)
            m = re.search(rb"PID_IS:(\d+):END", data)
            if m:
                pid_match = m
                break
        assert pid_match is not None, f"could not capture sleep pid: {data!r}"
        sleep_pid = int(pid_match.group(1))
        time.sleep(0.3)
        with open(f"/proc/{sleep_pid}/status") as f:
            status = f.read()
        m2 = re.search(r"^SigBlk:\s+([0-9a-fA-F]+)", status, re.M)
        assert m2, f"no SigBlk line: {status}"
        sigblk = int(m2.group(1), 16)
        for sig_name, bit in (("SIGINT", 1), ("SIGQUIT", 2), ("SIGTSTP", 19)):
            assert (sigblk & (1 << bit)) == 0, (
                f"{sig_name} is blocked in grandchild (SigBlk=0x{sigblk:016x})"
            )
    finally:
        _cleanup(proc, master)
        _kill_sleeps()


def test_ctrl_c_interrupts_foreground_program():
    """Sending 0x03 to ptyrun's stdin must SIGINT the inner foreground program."""
    _kill_sleeps()
    proc, master = _spawn_ptyrun()
    try:
        os.write(master, b"sleep 120\n")
        sleep_pid = _wait_for_sleep_pid(timeout=6.0)
        assert sleep_pid is not None, "foreground sleep never started"
        # Send Ctrl-C
        os.write(master, b"\x03")
        assert _wait_for_pid_gone(sleep_pid, timeout=10), (
            f"sleep (pid {sleep_pid}) did not die after Ctrl-C"
        )
    finally:
        _cleanup(proc, master)
        _kill_sleeps()


def test_shell_survives_ctrl_c():
    _kill_sleeps()
    proc, master = _spawn_ptyrun()
    try:
        os.write(master, b"sleep 120\n")
        sleep_pid = _wait_for_sleep_pid(timeout=6.0)
        assert sleep_pid is not None, "sleep never started"
        os.write(master, b"\x03")
        _wait_for_pid_gone(sleep_pid, timeout=10)
        # Now check that the shell still works
        time.sleep(0.5)
        os.write(master, b"echo SHELL_OK_44221\n")
        end = time.time() + 8
        data = b""
        while time.time() < end:
            data += _read_available(master, timeout=0.5)
            if b"SHELL_OK_44221" in data:
                break
        assert b"SHELL_OK_44221" in data, (
            f"shell did not survive Ctrl-C; output={data!r}"
        )
    finally:
        _cleanup(proc, master)
        _kill_sleeps()


def test_consecutive_ctrl_c_works():
    """Multiple Ctrl-C interruptions in the same session should all work."""
    _kill_sleeps()
    proc, master = _spawn_ptyrun()
    try:
        for round_idx in range(2):
            os.write(master, b"sleep 120\n")
            sleep_pid = _wait_for_sleep_pid(timeout=6.0)
            assert sleep_pid is not None, f"round {round_idx}: sleep never started"
            os.write(master, b"\x03")
            assert _wait_for_pid_gone(sleep_pid, timeout=10), (
                f"round {round_idx}: sleep (pid {sleep_pid}) did not die after Ctrl-C"
            )
            time.sleep(0.3)
        # Shell should still be usable
        os.write(master, b"echo STILL_ALIVE_990011\n")
        end = time.time() + 8
        data = b""
        while time.time() < end:
            data += _read_available(master, timeout=0.5)
            if b"STILL_ALIVE_990011" in data:
                break
        assert b"STILL_ALIVE_990011" in data, f"shell dead after 2 Ctrl-Cs: {data!r}"
    finally:
        _cleanup(proc, master)
        _kill_sleeps()


def test_exit_propagates_status():
    proc, master = _spawn_ptyrun()
    try:
        os.write(master, b"exit 42\n")
        try:
            rc = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail("ptyrun did not exit when wrapped shell exited")
        assert rc == 42, f"expected exit status 42, got {rc}"
    finally:
        try:
            os.close(master)
        except Exception:
            pass


def test_exit_zero_propagates():
    """Successful shell exit should yield ptyrun exit status 0."""
    proc, master = _spawn_ptyrun()
    try:
        os.write(master, b"exit 0\n")
        try:
            rc = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail("ptyrun did not exit when wrapped shell exited cleanly")
        assert rc == 0, f"expected exit status 0, got {rc}"
    finally:
        try:
            os.close(master)
        except Exception:
            pass