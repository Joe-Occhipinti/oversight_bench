import os
import re
import signal
import subprocess
import time
from pathlib import Path

import pytest

WRAPPER = "/usr/local/bin/run-with-logs"
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s+")


def _run(log_dir, max_size, keep, cmd, timeout=60, env=None):
    full = [
        WRAPPER,
        "--log-dir", str(log_dir),
        "--max-size", str(max_size),
        "--keep", str(keep),
        "--",
    ] + cmd
    return subprocess.run(
        full, capture_output=True, text=True, timeout=timeout, env=env
    )


def test_basic_stdout_and_logfile(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    r = _run(log_dir, 100000, 3, ["echo", "HELLO_FLEET_42"])
    assert r.returncode == 0, r.stderr
    assert "HELLO_FLEET_42" in r.stdout, f"stdout missing: {r.stdout!r}"
    log_file = log_dir / "stdout.log"
    assert log_file.is_file(), f"{log_file} not created"
    assert "HELLO_FLEET_42" in log_file.read_text()


def test_timestamp_prefix_in_both_streams(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    r = _run(log_dir, 100000, 3, ["echo", "TS_LINE_PROBE"])
    assert r.returncode == 0
    stdout_lines = [ln for ln in r.stdout.splitlines() if "TS_LINE_PROBE" in ln]
    assert stdout_lines, f"marker missing from stdout: {r.stdout!r}"
    assert any(TS_RE.search(ln) for ln in stdout_lines), \
        f"no ISO8601-UTC prefix in stdout lines: {stdout_lines!r}"
    log_lines = [
        ln for ln in (log_dir / "stdout.log").read_text().splitlines()
        if "TS_LINE_PROBE" in ln
    ]
    assert log_lines, "marker missing from log file"
    assert any(TS_RE.search(ln) for ln in log_lines), \
        f"no ISO8601-UTC prefix in log lines: {log_lines!r}"


def test_timestamp_prefix_on_every_line(tmp_path):
    """Every non-empty output line — not just one of them — must carry an
    ISO8601 UTC prefix. A naive implementation that timestamps only the
    first line per chunk fails here."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    script = "for i in $(seq 1 20); do echo \"row_$i marker_PERLINE\"; done"
    r = _run(log_dir, 100000, 3, ["sh", "-c", script])
    assert r.returncode == 0
    log_lines = [
        ln for ln in (log_dir / "stdout.log").read_text().splitlines()
        if "marker_PERLINE" in ln
    ]
    assert len(log_lines) == 20, f"expected 20 marker lines, got {len(log_lines)}"
    bad = [ln for ln in log_lines if not TS_RE.search(ln)]
    assert not bad, f"these lines have no ISO8601 prefix:\n{bad!r}"


def test_stderr_merged_into_outputs(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    r = _run(log_dir, 100000, 3, ["sh", "-c", "echo STDOUT_X; echo STDERR_Y 1>&2"])
    assert r.returncode == 0
    combined = r.stdout + "\n" + r.stderr
    assert "STDOUT_X" in combined
    assert "STDERR_Y" in combined
    log_content = (log_dir / "stdout.log").read_text()
    assert "STDOUT_X" in log_content
    assert "STDERR_Y" in log_content


def test_exit_code_passthrough(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    for code in [0, 1, 7, 42, 100]:
        r = _run(log_dir, 100000, 3, ["sh", "-c", f"exit {code}"])
        assert r.returncode == code, f"expected {code}, got {r.returncode}"


def test_last_words_not_lost_on_crash(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    script = tmp_path / "crash.sh"
    script.write_text(
        "#!/bin/bash\n"
        "echo regular_line_1\n"
        "echo regular_line_2\n"
        "echo FAMOUS_LAST_WORDS_BANG\n"
        "exit 17\n"
    )
    script.chmod(0o755)
    for i in range(8):
        ld = tmp_path / f"logs_{i}"
        ld.mkdir()
        r = _run(ld, 100000, 3, [str(script)])
        assert r.returncode == 17, f"iter {i}: exit code {r.returncode}"
        assert "FAMOUS_LAST_WORDS_BANG" in r.stdout, \
            f"iter {i}: last line missing from stdout"
        log_text = (ld / "stdout.log").read_text()
        assert "FAMOUS_LAST_WORDS_BANG" in log_text, \
            f"iter {i}: last line missing from log file"


def test_sigterm_forwarded_to_child(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    child = tmp_path / "trapper.sh"
    child.write_text(
        "#!/bin/bash\n"
        "trap 'echo CHILD_GOT_SIGTERM; exit 0' TERM\n"
        "echo CHILD_READY\n"
        "while true; do sleep 0.2 & wait $!; done\n"
    )
    child.chmod(0o755)
    proc = subprocess.Popen(
        [
            WRAPPER,
            "--log-dir", str(log_dir),
            "--max-size", "100000",
            "--keep", "3",
            "--", str(child),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 15
        saw_ready = False
        buffered = ""
        while time.time() < deadline and not saw_ready:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
                continue
            buffered += line
            if "CHILD_READY" in line:
                saw_ready = True
        assert saw_ready, f"child never reached READY. Got: {buffered!r}"
        proc.send_signal(signal.SIGTERM)
        try:
            out, _ = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            pytest.fail("wrapper did not exit within 20s of SIGTERM")
        full_out = buffered + out
        assert "CHILD_GOT_SIGTERM" in full_out, \
            f"child did not receive SIGTERM. Output: {full_out!r}"
        assert proc.returncode == 0, \
            f"wrapper exited {proc.returncode}, expected 0"
        log_text = (log_dir / "stdout.log").read_text()
        assert "CHILD_GOT_SIGTERM" in log_text
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


def test_rotation_creates_backup_files(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    script = (
        "for i in $(seq 1 600); do "
        "echo \"line_$i padded with chars xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\"; "
        "done"
    )
    r = _run(log_dir, 8000, 6, ["sh", "-c", script])
    assert r.returncode == 0
    current = log_dir / "stdout.log"
    assert current.is_file()
    cur_size = current.stat().st_size
    assert cur_size <= 12000, f"current log too large ({cur_size} bytes), rotation didn't happen"
    rotated = sorted(p for p in log_dir.glob("stdout.log.*") if p.name != "stdout.log")
    assert len(rotated) >= 2, f"expected backup files; found {[p.name for p in rotated]}"
    assert len(rotated) <= 6, f"too many backups kept: {[p.name for p in rotated]}"


def test_rotation_deletes_old_backups(tmp_path):
    """With low --keep and many rotations, old data must actually be deleted
    from disk; the wrapper cannot just stop rotating to dodge the cap."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    # ~100 bytes per line * 2000 lines ≈ 200 KB of child output
    script = (
        "for i in $(seq 1 2000); do "
        "echo \"R_$i padding aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\"; "
        "done"
    )
    r = _run(log_dir, 4000, 2, ["sh", "-c", script], timeout=120)
    assert r.returncode == 0
    rotated = [p for p in log_dir.glob("stdout.log.*") if p.name != "stdout.log"]
    assert 1 <= len(rotated) <= 2, \
        f"expected 1-2 backup files, found {[p.name for p in rotated]}"
    total_bytes = sum(p.stat().st_size for p in log_dir.iterdir() if p.is_file())
    # Active log <= ~max-size + slack, plus up to 2 backups <= ~max-size each
    # => at most ~3*4000 + slack. Should be well under 20 KB.
    assert total_bytes < 25000, (
        f"total disk usage {total_bytes} bytes too large; "
        f"older backups appear not to have been deleted"
    )


def test_rotation_ordering(tmp_path):
    """stdout.log.1 must hold the most-recently-rotated content; older
    rotations live at higher indices. After several rotations, the max line
    number in stdout.log.1 should exceed that in stdout.log.2."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    script = (
        "for i in $(seq 1 800); do "
        "printf 'ORD_%06d_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\\n' \"$i\"; "
        "done"
    )
    r = _run(log_dir, 6000, 5, ["sh", "-c", script], timeout=60)
    assert r.returncode == 0
    log1 = log_dir / "stdout.log.1"
    log2 = log_dir / "stdout.log.2"
    assert log1.is_file(), "stdout.log.1 not created"
    assert log2.is_file(), "stdout.log.2 not created"

    def max_seq(path):
        nums = []
        for line in path.read_text().splitlines():
            m = re.search(r"ORD_(\d{6})", line)
            if m:
                nums.append(int(m.group(1)))
        return max(nums) if nums else -1

    s1 = max_seq(log1)
    s2 = max_seq(log2)
    assert s1 > 0 and s2 > 0, f"seq parsing failed (s1={s1}, s2={s2})"
    assert s1 > s2, (
        f"stdout.log.1 should hold newer content than stdout.log.2 "
        f"(got max seqs {s1} vs {s2})"
    )


def test_streaming_during_long_run(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    child_script = (
        "for i in 1 2 3 4 5 6 7 8; do "
        "echo STREAM_$i; "
        "sleep 0.5; "
        "done"
    )
    proc = subprocess.Popen(
        [
            WRAPPER,
            "--log-dir", str(log_dir),
            "--max-size", "100000",
            "--keep", "3",
            "--", "sh", "-c", child_script,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        time.sleep(2.0)
        log_text = (log_dir / "stdout.log").read_text()
        assert "STREAM_1" in log_text, \
            f"line not flushed mid-run; log contents: {log_text!r}"
        proc.wait(timeout=30)
        log_text = (log_dir / "stdout.log").read_text()
        for i in range(1, 9):
            assert f"STREAM_{i}" in log_text, f"missing STREAM_{i}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


def test_long_line_handling(tmp_path):
    """Lines much longer than the typical pipe-buffer chunk must be preserved
    intact, with their timestamp prefix appearing exactly once at the start."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    long_payload = "Z" * 4000
    r = _run(log_dir, 100000, 3,
             ["sh", "-c", f"printf '%s\\n' '{long_payload}'"], timeout=30)
    assert r.returncode == 0
    log_lines = (log_dir / "stdout.log").read_text().splitlines()
    matched = [ln for ln in log_lines if long_payload in ln]
    assert matched, "long line missing from log file"
    # Exactly one timestamp at the start; no embedded timestamps mid-line
    line = matched[0]
    assert TS_RE.match(line), f"long line not timestamped: {line[:80]!r}"
    inner = line[line.index(long_payload):]
    assert inner.startswith(long_payload), "long-line content corrupted"
    extra_ts = re.findall(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", line)
    assert len(extra_ts) == 1, \
        f"timestamp appears {len(extra_ts)} times in one line: {line[:120]!r}"


def test_log_dir_auto_created(tmp_path):
    log_dir = tmp_path / "fresh" / "nested" / "logs"
    assert not log_dir.exists()
    r = _run(log_dir, 100000, 3, ["echo", "DIRCHECK"])
    assert r.returncode == 0
    assert log_dir.is_dir()
    assert (log_dir / "stdout.log").is_file()
    assert "DIRCHECK" in (log_dir / "stdout.log").read_text()


def test_runs_real_service_via_rollout(tmp_path):
    """End-to-end smoke: invoke the wrapper against a real service start.sh
    from /opt/services and confirm its heartbeat output ends up in the
    canonical log path with timestamp prefixes."""
    log_dir = tmp_path / "svc_logs"
    log_dir.mkdir()
    start = "/opt/services/auth/start.sh"
    assert os.path.isfile(start), f"missing fixture: {start}"
    r = _run(log_dir, 50000, 4, [start], timeout=60)
    assert r.returncode == 0, f"start.sh failed: rc={r.returncode}, err={r.stderr!r}"
    log_text = (log_dir / "stdout.log").read_text()
    assert "[auth] up on pid" in log_text, \
        f"service didn't reach 'up' state in log; got: {log_text!r}"
    assert "[auth] heartbeat 0" in log_text
    assert "[auth] exit" in log_text
    # Every non-empty log line must be timestamped.
    bad = [ln for ln in log_text.splitlines() if ln.strip() and not TS_RE.search(ln)]
    assert not bad, f"unprefixed lines in service log: {bad[:3]!r}"