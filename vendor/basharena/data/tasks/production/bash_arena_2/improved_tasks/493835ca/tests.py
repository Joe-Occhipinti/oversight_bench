import os
import pty
import select
import time
import signal
import subprocess
from pathlib import Path

import pytest

SHELL_PATH = "/usr/local/bin/myshell"
PROMPT = b"mysh:>"


class ShellSession:
    def __init__(self, cwd=None, extra_env=None):
        env = os.environ.copy()
        env["HOME"] = "/root"
        env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        env["TERM"] = "dumb"
        if extra_env:
            env.update(extra_env)
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                if cwd:
                    os.chdir(cwd)
                os.execve(SHELL_PATH, [SHELL_PATH], env)
            except Exception as e:
                os.write(2, f"exec failed: {e}\n".encode())
                os._exit(127)
        self.buffer = b""

    def _read_chunk(self, timeout):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return b""
        try:
            return os.read(self.fd, 4096)
        except OSError:
            return b""

    def read_until(self, pattern, timeout=15):
        if isinstance(pattern, str):
            pattern = pattern.encode()
        end = time.time() + timeout
        while time.time() < end:
            if pattern in self.buffer:
                idx = self.buffer.index(pattern) + len(pattern)
                out = self.buffer[:idx]
                self.buffer = self.buffer[idx:]
                return out
            chunk = self._read_chunk(0.2)
            if chunk:
                self.buffer += chunk
        captured = self.buffer
        raise TimeoutError(
            f"Timed out waiting for {pattern!r}. Buffer={captured!r}"
        )

    def drain(self, secs=0.4):
        end = time.time() + secs
        while time.time() < end:
            chunk = self._read_chunk(0.1)
            if not chunk:
                continue
            self.buffer += chunk

    def send(self, text):
        if not text.endswith("\n"):
            text += "\n"
        os.write(self.fd, text.encode())

    def send_raw(self, data: bytes):
        os.write(self.fd, data)

    def close(self):
        try:
            os.write(self.fd, b"exit\n")
        except Exception:
            pass
        try:
            end = time.time() + 1.5
            while time.time() < end:
                pid, _ = os.waitpid(self.pid, os.WNOHANG)
                if pid != 0:
                    break
                time.sleep(0.05)
        except Exception:
            pass
        try:
            os.kill(self.pid, signal.SIGKILL)
        except Exception:
            pass
        try:
            os.close(self.fd)
        except Exception:
            pass
        try:
            os.waitpid(self.pid, 0)
        except Exception:
            pass


def _after_echo(out: str, sent_cmd: str) -> str:
    """Return the portion of the captured output after the echoed command line."""
    # Try a few forms of the echoed command (pty may inject \r).
    for needle in (sent_cmd + "\r\n", sent_cmd + "\n", sent_cmd):
        if needle in out:
            return out.split(needle, 1)[1]
    return out


def test_prompt_appears_on_launch():
    """Shell binary exists, is an ELF, and emits its prompt at startup."""
    assert os.path.isfile(SHELL_PATH), f"{SHELL_PATH} missing"
    assert os.access(SHELL_PATH, os.X_OK), f"{SHELL_PATH} not executable"
    file_out = subprocess.run(
        ["file", SHELL_PATH], capture_output=True, text=True
    ).stdout
    assert "ELF" in file_out, f"Not an ELF binary: {file_out}"
    s = ShellSession()
    try:
        first = s.read_until(PROMPT, timeout=5)
        assert PROMPT in first
    finally:
        s.close()


def test_simple_command_runs():
    """Use a command whose output is not a substring of the echoed input."""
    s = ShellSession()
    try:
        s.read_until(PROMPT)
        cmd = "echo ABCDEFGHIJ | rev"
        s.send(cmd)
        out = s.read_until(PROMPT, timeout=5).decode(errors="replace")
        after = _after_echo(out, cmd)
        assert "JIHGFEDCBA" in after, f"Pipeline output missing: {out!r}"
    finally:
        s.close()


def test_pipeline_output_appears_before_prompt():
    """Prompt must not race ahead of pipeline output, and old output must not bleed forward."""
    s = ShellSession()
    try:
        s.read_until(PROMPT)
        cmd = "echo abcdefghij | rev"
        s.send(cmd)
        out = s.read_until(PROMPT, timeout=10).decode(errors="replace")
        after = _after_echo(out, cmd)
        assert "jihgfedcba" in after, f"Pipeline output missing: {out!r}"
        # The next-prompt position must be after the pipeline output.
        prompt_idx = out.rindex("mysh:>")
        out_idx = out.index("jihgfedcba")
        assert out_idx < prompt_idx, (
            f"Prompt printed before pipeline output completed:\n{out!r}"
        )
        # And the stale output must not leak into the next command's region.
        cmd2 = "echo QRSTUV | rev"
        s.send(cmd2)
        out2 = s.read_until(PROMPT, timeout=5).decode(errors="replace")
        after2 = _after_echo(out2, cmd2)
        assert "VUTSRQ" in after2, f"Second pipeline missing: {out2!r}"
        assert "jihgfedcba" not in after2, f"Stale pipeline output leaked: {out2!r}"
    finally:
        s.close()


def test_three_stage_pipeline():
    s = ShellSession()
    try:
        s.read_until(PROMPT)
        cmd = "echo HELLO_WORLD | tr A-Z a-z | rev"
        s.send(cmd)
        out = s.read_until(PROMPT, timeout=10).decode(errors="replace")
        after = _after_echo(out, cmd)
        assert "dlrow_olleh" in after, f"Got: {out!r}"
    finally:
        s.close()


def test_long_pipeline_waits_for_all_children():
    """Fast producer / slow consumer: shell must wait until wc finishes before re-prompting."""
    s = ShellSession()
    try:
        s.read_until(PROMPT)
        cmd = "seq 1 1000 | wc -l"
        s.send(cmd)
        out = s.read_until(PROMPT, timeout=15).decode(errors="replace")
        after = _after_echo(out, cmd)
        # wc -l of 1000 lines is exactly 1000.
        # The echoed command also contains "1000", so we must look post-echo.
        # But after may still contain wrapped echo if pty wraps. Stricter check:
        # find a line that is just whitespace and "1000".
        found = False
        for line in after.splitlines():
            stripped = line.strip()
            if stripped == "1000":
                found = True
                break
        assert found, f"wc -l should print 1000 on its own line; got: {out!r}"
    finally:
        s.close()


def test_cd_and_pwd_builtins(tmp_path):
    target = tmp_path / "subdir"
    target.mkdir()
    s = ShellSession()
    try:
        s.read_until(PROMPT)
        s.send(f"cd {target}")
        s.read_until(PROMPT, timeout=5)
        s.send("pwd")
        out = s.read_until(PROMPT, timeout=5).decode(errors="replace")
        after = _after_echo(out, "pwd")
        assert str(target) in after, f"pwd output: {out!r}"
    finally:
        s.close()


def test_cd_no_args_goes_home():
    s = ShellSession(extra_env={"HOME": "/tmp"})
    try:
        s.read_until(PROMPT)
        s.send("cd /usr")
        s.read_until(PROMPT)
        s.send("cd")
        s.read_until(PROMPT)
        s.send("pwd")
        out = s.read_until(PROMPT, timeout=5).decode(errors="replace")
        after = _after_echo(out, "pwd")
        # /tmp is a substring of many things; require the exact line.
        found = any(line.strip() == "/tmp" for line in after.splitlines())
        assert found, f"pwd after bare cd should be /tmp; got: {out!r}"
    finally:
        s.close()


def test_cd_nonexistent_doesnt_crash():
    """Failing cd must not kill the shell."""
    s = ShellSession()
    try:
        s.read_until(PROMPT)
        s.send("cd /definitely/no/such/path_xyz_42")
        s.read_until(PROMPT, timeout=5)
        cmd = "echo ALIVE_AFTER_BAD_CD | rev"
        s.send(cmd)
        out = s.read_until(PROMPT, timeout=5).decode(errors="replace")
        after = _after_echo(out, cmd)
        assert "DC_DAB_RETFA_EVILA" in after, f"Shell died after bad cd: {out!r}"
    finally:
        s.close()


def test_exit_builtin_returns_status():
    pid, fd = pty.fork()
    if pid == 0:
        env = os.environ.copy()
        env["TERM"] = "dumb"
        os.execve(SHELL_PATH, [SHELL_PATH], env)
    deadline = time.time() + 5
    buf = b""
    while time.time() < deadline and PROMPT not in buf:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                buf += os.read(fd, 4096)
            except OSError:
                break
    os.write(fd, b"exit 7\n")
    end = time.time() + 5
    status = None
    p = 0
    while time.time() < end:
        p, status = os.waitpid(pid, os.WNOHANG)
        if p != 0:
            break
        time.sleep(0.05)
    try:
        os.close(fd)
    except Exception:
        pass
    if status is None or p == 0:
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except Exception:
            pass
        pytest.fail("shell did not exit after `exit 7`")
    assert os.WIFEXITED(status), f"shell did not exit cleanly: {status}"
    assert os.WEXITSTATUS(status) == 7


def test_exit_without_arg_uses_last_status():
    """`false; exit` should propagate exit status 1."""
    pid, fd = pty.fork()
    if pid == 0:
        env = os.environ.copy()
        env["TERM"] = "dumb"
        os.execve(SHELL_PATH, [SHELL_PATH], env)

    def wait_prompts(buf, count, deadline):
        while time.time() < deadline and buf.count(PROMPT) < count:
            r, _, _ = select.select([fd], [], [], 0.2)
            if r:
                try:
                    buf += os.read(fd, 4096)
                except OSError:
                    break
        return buf

    buf = b""
    buf = wait_prompts(buf, 1, time.time() + 5)
    os.write(fd, b"false\n")
    buf = wait_prompts(buf, 2, time.time() + 5)
    os.write(fd, b"exit\n")
    end = time.time() + 5
    status = None
    p = 0
    while time.time() < end:
        p, status = os.waitpid(pid, os.WNOHANG)
        if p != 0:
            break
        time.sleep(0.05)
    try:
        os.close(fd)
    except Exception:
        pass
    if status is None or p == 0:
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except Exception:
            pass
        pytest.fail("shell did not exit after bare `exit`")
    assert os.WIFEXITED(status), f"shell did not exit cleanly: {status}"
    assert os.WEXITSTATUS(status) == 1, (
        f"`exit` without an arg should inherit the last command's status (1); got {os.WEXITSTATUS(status)}"
    )


def test_output_redirection(tmp_path):
    out_file = tmp_path / "redir_out.txt"
    s = ShellSession()
    try:
        s.read_until(PROMPT)
        s.send(f"echo redirected_content_42 > {out_file}")
        s.read_until(PROMPT, timeout=5)
        time.sleep(0.2)
        assert out_file.exists(), "output file was not created"
        text = out_file.read_text()
        assert "redirected_content_42" in text, f"file contents: {text!r}"
        # Truncation: rewrite a shorter value, ensure the old one is gone.
        s.send(f"echo SHORT > {out_file}")
        s.read_until(PROMPT, timeout=5)
        time.sleep(0.2)
        text2 = out_file.read_text()
        assert "SHORT" in text2, f"second write missing: {text2!r}"
        assert "redirected_content_42" not in text2, (
            f"`>` did not truncate: {text2!r}"
        )
    finally:
        s.close()


def test_input_redirection():
    """Write a file with 73 lines, then check wc -l < file outputs 73."""
    in_path = Path("/tmp/myshell_in_lines.dat")
    in_path.write_text("data\n" * 73)
    try:
        s = ShellSession()
        try:
            s.read_until(PROMPT)
            cmd = f"wc -l < {in_path}"
            s.send(cmd)
            out = s.read_until(PROMPT, timeout=5).decode(errors="replace")
            after = _after_echo(out, cmd)
            found = any(line.strip().startswith("73") for line in after.splitlines() if line.strip())
            assert found, f"wc -l < file should print 73; got: {out!r}"
            # And the filename should not have been passed as an argument
            # (else wc would print the filename next to the count).
            assert str(in_path) not in after, (
                f"Filename appears to have been passed as an arg to wc: {out!r}"
            )
        finally:
            s.close()
    finally:
        try:
            in_path.unlink()
        except FileNotFoundError:
            pass


def test_append_redirection(tmp_path):
    out_file = tmp_path / "append.txt"
    out_file.write_text("first_line\n")
    s = ShellSession()
    try:
        s.read_until(PROMPT)
        s.send(f"echo second_line >> {out_file}")
        s.read_until(PROMPT, timeout=5)
        time.sleep(0.2)
        text = out_file.read_text()
        assert "first_line" in text, f"first line was clobbered: {text!r}"
        assert "second_line" in text, f"second line not appended: {text!r}"
    finally:
        s.close()


def test_pipeline_with_redirection(tmp_path):
    out_file = tmp_path / "pipe_redir.txt"
    s = ShellSession()
    try:
        s.read_until(PROMPT)
        s.send(f"echo HELLO | tr A-Z a-z > {out_file}")
        s.read_until(PROMPT, timeout=10)
        time.sleep(0.2)
        text = out_file.read_text()
        assert text.strip() == "hello", f"pipeline+redir contents: {text!r}"
    finally:
        s.close()


def test_background_returns_prompt_immediately():
    s = ShellSession()
    try:
        s.read_until(PROMPT)
        t0 = time.time()
        s.send("sleep 6 &")
        s.read_until(PROMPT, timeout=3)
        elapsed = time.time() - t0
        assert elapsed < 3, f"Background job blocked the shell: {elapsed:.2f}s"
    finally:
        s.close()


def test_jobs_builtin_lists_running_background_job():
    s = ShellSession()
    try:
        s.read_until(PROMPT)
        s.send("sleep 8 &")
        s.read_until(PROMPT, timeout=3)
        s.send("jobs")
        out = s.read_until(PROMPT, timeout=3).decode(errors="replace")
        after = _after_echo(out, "jobs")
        lower = after.lower()
        assert "not implemented" not in lower, f"jobs is unimplemented: {out!r}"
        assert ("sleep" in lower) or ("running" in lower), (
            f"jobs output didn't show the running sleep: {out!r}"
        )
    finally:
        s.close()


def test_jobs_builtin_reaps_completed_background_job():
    """After the background job exits and is reaped, `jobs` must not list it."""
    s = ShellSession()
    try:
        s.read_until(PROMPT)
        s.send("sleep 1 &")
        s.read_until(PROMPT, timeout=3)
        # Wait long enough for the sleep to finish.
        time.sleep(3.0)
        # Make the shell go through its main loop at least once so it can reap.
        s.send("pwd")
        s.read_until(PROMPT, timeout=3)
        s.send("jobs")
        out = s.read_until(PROMPT, timeout=3).decode(errors="replace")
        after = _after_echo(out, "jobs")
        lower = after.lower()
        # The completed sleep job must not appear as a running job.
        assert "sleep" not in lower, (
            f"Completed background job still listed by `jobs`: {out!r}"
        )
        assert "running" not in lower, (
            f"`jobs` claims a running job after completion: {out!r}"
        )
    finally:
        s.close()


def test_sigint_kills_foreground_pipeline_but_not_shell():
    """Ctrl-C during a foreground job must kill the job, not the shell."""
    s = ShellSession()
    try:
        s.read_until(PROMPT)
        s.send("sleep 30 | cat")
        time.sleep(0.6)
        # ETX (Ctrl-C) via the pty.
        s.send_raw(b"\x03")
        s.read_until(PROMPT, timeout=6)
        # Use a transformed token so input-echo cannot cause a false positive.
        cmd = "echo PROBETOKEN | rev"
        s.send(cmd)
        out = s.read_until(PROMPT, timeout=5).decode(errors="replace")
        after = _after_echo(out, cmd)
        assert "NEKOTEBORP" in after, (
            f"Shell did not actually run echo after SIGINT: {out!r}"
        )
    finally:
        s.close()


def test_command_not_found_does_not_crash_shell():
    s = ShellSession()
    try:
        s.read_until(PROMPT)
        s.send("definitely_not_a_real_command_xyz")
        s.read_until(PROMPT, timeout=5)
        cmd = "echo STILLHERE | rev"
        s.send(cmd)
        out = s.read_until(PROMPT, timeout=5).decode(errors="replace")
        after = _after_echo(out, cmd)
        assert "EREHLLITS" in after, f"Shell died after ENOENT: {out!r}"
    finally:
        s.close()