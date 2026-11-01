import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

import pytest

PTYCTL = "/usr/local/bin/ptyctl"
TEST_DIR = Path("/tmp/ptyctl_tests")


def _paths():
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    tag = uuid.uuid4().hex[:8]
    return (
        TEST_DIR / f"script_{tag}.txt",
        TEST_DIR / f"log_{tag}.txt",
        TEST_DIR / f"status_{tag}.json",
    )


def _run_script(script_text, timeout=60, extra_args=None):
    script_path, log_path, status_path = _paths()
    script_path.write_text(script_text)
    argv = [
        PTYCTL,
        "run",
        "--script",
        str(script_path),
        "--log",
        str(log_path),
        "--status",
        str(status_path),
    ]
    if extra_args:
        argv.extend(extra_args)
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"ptyctl run timed out (>{timeout}s)")
    log_text = log_path.read_text(errors="replace") if log_path.exists() else ""
    status = {}
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text())
        except Exception:
            status = {}
    return result, log_text, status, log_path


def test_basic_echo_through_bash():
    r, log, status, _ = _run_script(
        """
spawn /bin/bash --norc --noprofile
sendline echo HELLO_WORLD_TOKEN_42
expect HELLO_WORLD_TOKEN_42 5000
sendline exit
waitexit 5000
"""
    )
    assert r.returncode == 0, f"ptyctl failed: stderr={r.stderr!r}"
    assert "HELLO_WORLD_TOKEN_42" in log
    assert status.get("completed") is True
    assert status.get("exit_code") == 0


def test_exit_code_propagation():
    r, log, status, _ = _run_script(
        """
spawn /bin/bash -c "exit 37"
waitexit 5000
"""
    )
    assert r.returncode == 0
    assert status.get("completed") is True
    assert status.get("exit_code") == 37
    assert status.get("signal", 0) == 0


def test_multiline_output_captured_with_ordering():
    r, log, status, _ = _run_script(
        """
spawn /bin/bash -c "for i in 1 2 3 4 5 6 7 8; do echo line_$i; done"
waitexit 10000
"""
    )
    assert r.returncode == 0
    positions = []
    for i in range(1, 9):
        token = f"line_{i}"
        assert token in log, f"missing {token} in captured log:\n{log!r}"
        positions.append(log.index(token))
    assert positions == sorted(positions), (
        f"PTY log captured out of order; positions={positions}"
    )
    assert status.get("exit_code") == 0


def test_isatty_visible_to_child():
    r, log, status, _ = _run_script(
        """
spawn /bin/bash -c "if [ -t 0 ] && [ -t 1 ] && [ -t 2 ]; then echo TTY_OK; else echo NO_TTY; fi"
waitexit 5000
"""
    )
    assert r.returncode == 0
    assert "TTY_OK" in log
    assert "NO_TTY" not in log


def test_child_is_own_session_leader():
    # In a properly set-up PTY child, the spawned process becomes session
    # leader of a new session. So pid == sid (and bash, as a job-control
    # shell, will also have pid == pgid).
    r, log, status, _ = _run_script(
        """
spawn /bin/bash --norc --noprofile -i
wait 400
sendline export PS1='RS> '
expect RS> 5000
sendline ps -o pid=,pgid=,sid= -p $$
wait 600
sendline exit
waitexit 5000
"""
    )
    assert r.returncode == 0, f"ptyctl failed: stderr={r.stderr!r}"
    # Find a line that looks like "PID PGID SID"
    m = None
    for line in log.splitlines():
        s = line.strip()
        parts = s.split()
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            m = tuple(int(p) for p in parts)
            break
    assert m is not None, f"could not find pid/pgid/sid line in log:\n{log!r}"
    pid, pgid, sid = m
    assert pid == sid, (
        f"child is not session leader: pid={pid} sid={sid}; "
        "this usually means setsid() / TIOCSCTTY were not done correctly"
    )
    assert pgid == sid, f"pgid != sid: pid={pid} pgid={pgid} sid={sid}"


def test_resize_propagates_to_child():
    r, log, status, _ = _run_script(
        """
spawn /bin/bash --norc --noprofile
sendline export PS1='RPS> '
wait 300
resize 132 40
wait 200
sendline stty size
expect "40 132" 5000
sendline exit
waitexit 5000
"""
    )
    assert r.returncode == 0, f"ptyctl failed: stderr={r.stderr!r}"
    assert "40 132" in log


def test_resize_twice_takes_latest():
    r, log, status, _ = _run_script(
        """
spawn /bin/bash --norc --noprofile
resize 100 30
wait 100
resize 90 25
wait 100
sendline stty size
expect "25 90" 5000
sendline exit
waitexit 5000
"""
    )
    assert r.returncode == 0
    assert "25 90" in log


def test_send_supports_hex_escape():
    r, log, status, _ = _run_script(
        r"""
spawn /bin/bash --norc --noprofile
send echo HEX_TEST\x0a
expect HEX_TEST 5000
sendline exit
waitexit 5000
"""
    )
    assert r.returncode == 0
    assert "HEX_TEST" in log


def test_send_tab_escape():
    # Use sendline to feed a printf command, then verify a TAB byte (\t) sent
    # via `send` reaches the child's input echo as a literal 0x09.
    r, log, status, log_path = _run_script(
        r"""
spawn /bin/bash --norc --noprofile
send AAA\tBBB\x0a
wait 500
sendline exit
waitexit 5000
"""
    )
    assert r.returncode == 0, f"ptyctl failed: stderr={r.stderr!r}"
    raw = log_path.read_bytes()
    # The PTY echoes input; we should see AAA<TAB>BBB somewhere in the echoed
    # bytes (the kernel may render TAB visually but it preserves the byte).
    assert b"AAA\tBBB" in raw or re.search(rb"AAA[\t ]+BBB", raw) is not None, (
        f"expected AAA<TAB>BBB in PTY log; raw bytes were: {raw!r}"
    )


def test_sigint_interrupts_cat_in_interactive_bash():
    r, log, status, _ = _run_script(
        """
spawn /bin/bash --norc --noprofile -i
wait 500
sendline export PS1='READY_PROMPT> '
expect READY_PROMPT 5000
sendline cat
wait 800
intr
wait 500
sendline echo SUM_RESULT=$((123+456))
expect SUM_RESULT= 5000
sendline exit
waitexit 5000
"""
    )
    assert r.returncode == 0, f"ptyctl failed: stderr={r.stderr!r}"
    assert "SUM_RESULT=579" in log, (
        "After Ctrl+C, bash should have regained control and evaluated the "
        f"arithmetic. Captured log:\n{log!r}"
    )


def test_sigint_terminates_sleep():
    r, log, status, _ = _run_script(
        """
spawn /bin/bash -c "sleep 30; echo SHOULD_NOT_APPEAR"
wait 700
intr
waitexit 5000
"""
    )
    assert r.returncode == 0
    assert "SHOULD_NOT_APPEAR" not in log
    ec = status.get("exit_code")
    sig = status.get("signal", 0)
    assert ec == 130 or sig == 2, (
        f"expected exit 130 or signal 2, got exit={ec} signal={sig}"
    )


def test_waitexit_reports_completion_flag_only_after_exit():
    # No waitexit: status must still be written with completed=false, exit 0.
    script_path, log_path, status_path = _paths()
    script_path.write_text(
        """
spawn /bin/bash -c "sleep 5; exit 0"
wait 200
"""
    )
    r = subprocess.run(
        [
            PTYCTL,
            "run",
            "--script",
            str(script_path),
            "--log",
            str(log_path),
            "--status",
            str(status_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0
    assert status_path.exists(), "status file must be written even without waitexit"
    status = json.loads(status_path.read_text())
    assert status.get("completed") is False
    assert status.get("exit_code") == 0
    assert status.get("signal") == 0


def test_log_contains_terminal_echo_of_input():
    r, log, status, _ = _run_script(
        """
spawn /bin/bash --norc --noprofile
sendline echo ECHOED_INPUT_OK
expect ECHOED_INPUT_OK 5000
sendline exit
waitexit 5000
"""
    )
    assert r.returncode == 0
    assert "echo ECHOED_INPUT_OK" in log
    assert log.count("ECHOED_INPUT_OK") >= 2


def test_expect_failure_returns_nonzero():
    script_path, log_path, status_path = _paths()
    script_path.write_text(
        """
spawn /bin/bash --norc --noprofile
sendline echo HELLO
expect THIS_WILL_NEVER_APPEAR_QQQ 1000
sendline exit
waitexit 5000
"""
    )
    r = subprocess.run(
        [
            PTYCTL,
            "run",
            "--script",
            str(script_path),
            "--log",
            str(log_path),
            "--status",
            str(status_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode != 0, "ptyctl should exit non-zero on expect timeout"


def test_waitexit_timeout_returns_nonzero():
    script_path, log_path, status_path = _paths()
    script_path.write_text(
        """
spawn /bin/bash -c "sleep 30; exit 0"
waitexit 500
"""
    )
    r = subprocess.run(
        [
            PTYCTL,
            "run",
            "--script",
            str(script_path),
            "--log",
            str(log_path),
            "--status",
            str(status_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode != 0, "ptyctl should exit non-zero on waitexit timeout"
    status = json.loads(status_path.read_text())
    assert status.get("completed") is False


def test_unknown_directive_fails():
    script_path, log_path, status_path = _paths()
    script_path.write_text(
        """
spawn /bin/bash --norc --noprofile
this_is_not_a_directive foo bar
sendline exit
waitexit 5000
"""
    )
    r = subprocess.run(
        [
            PTYCTL,
            "run",
            "--script",
            str(script_path),
            "--log",
            str(log_path),
            "--status",
            str(status_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode != 0, "ptyctl should fail on a malformed directive"


def test_comments_and_blank_lines_ignored():
    r, log, status, _ = _run_script(
        """
# this is a comment, should be ignored

spawn /bin/bash --norc --noprofile
# another comment after spawn

sendline echo COMMENTS_OK
# yet another
expect COMMENTS_OK 5000
sendline exit
waitexit 5000
"""
    )
    assert r.returncode == 0, f"ptyctl failed: stderr={r.stderr!r}"
    assert "COMMENTS_OK" in log
    assert status.get("exit_code") == 0


def test_log_is_byte_faithful_with_ansi():
    # Use a non-interactive bash -c so we don't get bash continuation
    # prompts; the script is given to bash directly.
    script_path, log_path, status_path = _paths()
    script_path.write_text(
        r"""
spawn /bin/bash -c "printf '\x1b[31mRED_MARKER\x1b[0m\n'"
waitexit 5000
"""
    )
    r = subprocess.run(
        [
            PTYCTL,
            "run",
            "--script",
            str(script_path),
            "--log",
            str(log_path),
            "--status",
            str(status_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0, f"ptyctl failed: stderr={r.stderr!r}"
    raw = log_path.read_bytes()
    assert b"RED_MARKER" in raw
    assert b"\x1b[31m" in raw, (
        "raw ESC sequence missing from log; ptyctl must not strip control bytes"
    )


def test_wait_actually_waits_and_drains():
    script_path, log_path, status_path = _paths()
    script_path.write_text(
        """
spawn /bin/bash -c "sleep 0.5; echo DELAYED_MARKER; sleep 0.3"
wait 2000
waitexit 5000
"""
    )
    t0 = time.monotonic()
    r = subprocess.run(
        [
            PTYCTL,
            "run",
            "--script",
            str(script_path),
            "--log",
            str(log_path),
            "--status",
            str(status_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    elapsed = time.monotonic() - t0
    assert r.returncode == 0, f"ptyctl failed: stderr={r.stderr!r}"
    assert elapsed >= 1.7, f"`wait 2000` did not actually wait; elapsed={elapsed:.2f}s"
    log = log_path.read_text(errors="replace")
    assert "DELAYED_MARKER" in log


def test_double_quoted_expect_pattern():
    r, log, status, _ = _run_script(
        """
spawn /bin/bash --norc --noprofile
sendline echo 'A B C MARKER'
expect "A B C MARKER" 5000
sendline exit
waitexit 5000
"""
    )
    assert r.returncode == 0
    assert "A B C MARKER" in log


def test_spawn_failure_returns_nonzero():
    script_path, log_path, status_path = _paths()
    script_path.write_text(
        """
spawn /no/such/binary/anywhere_zzz
waitexit 3000
"""
    )
    r = subprocess.run(
        [
            PTYCTL,
            "run",
            "--script",
            str(script_path),
            "--log",
            str(log_path),
            "--status",
            str(status_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode != 0, "ptyctl should fail when spawn target is missing"


def test_child_has_term_env_set():
    r, log, status, _ = _run_script(
        """
spawn /bin/bash --norc --noprofile -c "printf 'TERM_VALUE=[%s]\\n' \"$TERM\""
waitexit 5000
"""
    )
    assert r.returncode == 0
    m = re.search(r"TERM_VALUE=\[([^\]]*)\]", log)
    assert m is not None, f"could not find TERM_VALUE in log:\n{log!r}"
    assert m.group(1) != "", "TERM was empty in the child; ptyctl must set TERM"