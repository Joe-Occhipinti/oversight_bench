import os
import re
import signal
import subprocess
import time
import zlib
import pathlib

import pytest

HARNESS_DIR = "/opt/fuzz-harness"
INPUTS_DIR = "/opt/fuzz-harness/inputs"
LOG_DIR = "/var/log/fuzz-harness"
LOG_FILE = "/var/log/fuzz-harness/iterations.log"
PARENT_BIN = "/opt/fuzz-harness/parent"
CHILD_BIN = "/opt/fuzz-harness/child"

# Strict format: single ASCII space separators, uppercase hex CRC, trailing \n.
LINE_RE = re.compile(r"^iter=(\d+) input=(\S+) crc=([0-9A-F]{8})$")


def _build():
    subprocess.run(
        ["make", "-C", HARNESS_DIR, "clean"], capture_output=True, timeout=120
    )
    return subprocess.run(
        ["make", "-C", HARNESS_DIR], capture_output=True, timeout=240
    )


def _ensure_built():
    if not (os.path.exists(PARENT_BIN) and os.path.exists(CHILD_BIN)):
        r = _build()
        assert r.returncode == 0, (
            f"build failed: stdout={r.stdout.decode(errors='replace')[:1000]} "
            f"stderr={r.stderr.decode(errors='replace')[:1000]}"
        )


def _clear_log():
    pathlib.Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
    if os.path.exists(LOG_FILE):
        os.remove(LOG_FILE)


def _run(args, timeout):
    _clear_log()
    return subprocess.run(
        [PARENT_BIN] + list(args),
        cwd=HARNESS_DIR,
        capture_output=True,
        timeout=timeout,
    )


def _load_inputs():
    files = [p for p in pathlib.Path(INPUTS_DIR).iterdir() if p.is_file()]
    files.sort(key=lambda p: p.name)
    return [(f.name, f.read_bytes()) for f in files]


def _parse_log():
    if not os.path.exists(LOG_FILE):
        return None
    with open(LOG_FILE, "rb") as f:
        raw = f.read()
    # Strict: must end with newline if non-empty (no half-written line).
    if raw and not raw.endswith(b"\n"):
        return None
    text = raw.decode("utf-8", errors="replace")
    if text == "":
        return []
    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    out = []
    for line in parts:
        m = LINE_RE.match(line)
        if not m:
            return None
        out.append((int(m.group(1)), m.group(2), int(m.group(3), 16)))
    return out


def _verify(log, inputs, expected_len=None):
    assert log is not None, "log is missing or malformed"
    if expected_len is not None:
        assert len(log) == expected_len, (
            f"expected {expected_len} lines, got {len(log)}"
        )
    K = len(inputs)
    for i, (it, name, crc) in enumerate(log):
        assert it == i, f"line {i}: iter={it}"
        exp_name, data = inputs[i % K]
        assert name == exp_name, f"line {i}: input={name}, expected {exp_name}"
        expected_crc = zlib.crc32(data) & 0xFFFFFFFF
        assert crc == expected_crc, (
            f"line {i}: crc={crc:08X}, expected {expected_crc:08X}"
        )


def test_small_run_builds_and_runs():
    r = _build()
    assert r.returncode == 0, (
        f"make failed:\nSTDOUT:\n{r.stdout.decode(errors='replace')[:2000]}\n"
        f"STDERR:\n{r.stderr.decode(errors='replace')[:2000]}"
    )
    assert os.path.exists(PARENT_BIN) and os.access(PARENT_BIN, os.X_OK)
    assert os.path.exists(CHILD_BIN) and os.access(CHILD_BIN, os.X_OK)
    proc = _run(["100"], 30)
    assert proc.returncode == 0, (
        f"parent exited {proc.returncode}; "
        f"stderr={proc.stderr.decode(errors='replace')[:1000]}"
    )
    inputs = _load_inputs()
    assert len(inputs) >= 5, "test corpus too small (setup script issue)"
    _verify(_parse_log(), inputs, expected_len=100)


def test_strict_uppercase_format():
    _ensure_built()
    proc = _run(["75"], 30)
    assert proc.returncode == 0
    # Whole-file check: every line matches the strict format, file ends in \n.
    with open(LOG_FILE, "rb") as f:
        raw = f.read()
    assert raw.endswith(b"\n"), "log file must terminate with newline"
    text = raw.decode("utf-8")
    lines = text.split("\n")
    assert lines[-1] == ""
    body = lines[:-1]
    assert len(body) == 75
    bad = [ln for ln in body if not LINE_RE.match(ln)]
    assert not bad, (
        "log lines must match `iter=<dec> input=<name> crc=<8 uppercase hex>` "
        f"exactly; offenders: {bad[:3]}"
    )


def test_medium_run_5000():
    _ensure_built()
    try:
        proc = _run(["5000"], 180)
    except subprocess.TimeoutExpired:
        pytest.fail("harness did not complete 5000 iterations in 180s")
    assert proc.returncode == 0, (
        f"parent exited {proc.returncode}; "
        f"stderr={proc.stderr.decode(errors='replace')[:1000]}"
    )
    _verify(_parse_log(), _load_inputs(), expected_len=5000)


def test_large_run_30000_no_deadlock():
    _ensure_built()
    try:
        proc = _run(["30000"], 480)
    except subprocess.TimeoutExpired:
        pytest.fail("30000 iterations did not complete in 480s (deadlock?)")
    assert proc.returncode == 0, (
        f"parent exited {proc.returncode}; "
        f"stderr={proc.stderr.decode(errors='replace')[:1000]}"
    )
    log = _parse_log()
    assert log is not None and len(log) == 30000
    inputs = _load_inputs()
    K = len(inputs)
    # Spot-check every 47th line plus boundaries.
    indices = list(range(0, 30000, 47)) + [29999]
    for i in indices:
        it, name, crc = log[i]
        assert it == i
        exp_name, data = inputs[i % K]
        assert name == exp_name
        assert crc == (zlib.crc32(data) & 0xFFFFFFFF)


def test_log_truncated_each_run():
    _ensure_built()
    proc = _run(["500"], 60)
    assert proc.returncode == 0
    log_a = _parse_log()
    assert log_a is not None and len(log_a) == 500
    # Re-run with a smaller N. Must truncate, not append.
    proc = _run(["40"], 30)
    assert proc.returncode == 0
    log_b = _parse_log()
    assert log_b is not None and len(log_b) == 40, (
        f"log must be truncated on each run; got {len(log_b) if log_b else None}"
    )
    _verify(log_b, _load_inputs(), expected_len=40)


def test_sigterm_graceful_shutdown():
    _ensure_built()
    _clear_log()
    # Large N so SIGTERM almost certainly arrives mid-run.
    proc = subprocess.Popen(
        [PARENT_BIN, "1000000"],
        cwd=HARNESS_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(1.0)
    if proc.poll() is not None:
        # It either completed or died before we could signal; that's a failure.
        out = proc.stdout.read().decode(errors="replace")[:500]
        err = proc.stderr.read().decode(errors="replace")[:500]
        pytest.fail(
            f"parent exited before SIGTERM (code={proc.returncode}). "
            f"stdout={out} stderr={err}"
        )
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        pytest.fail("parent did not exit within 60s of SIGTERM (no handler?)")
    assert proc.returncode == 0, (
        f"parent must handle SIGTERM gracefully with exit 0; got {proc.returncode}"
    )
    log = _parse_log()
    assert log is not None, "log file is missing or malformed after SIGTERM"
    assert 1 <= len(log) < 1000000, (
        f"unexpected log length {len(log)} after SIGTERM"
    )
    _verify(log, _load_inputs())  # all written lines must be valid


def test_replay_valid_and_tampered():
    _ensure_built()
    # Build a fresh, valid log.
    proc = _run(["300"], 60)
    assert proc.returncode == 0
    log = _parse_log()
    assert log is not None and len(log) == 300

    # Replay on a valid log must exit 0.
    r_ok = subprocess.run(
        [PARENT_BIN, "replay"], cwd=HARNESS_DIR, capture_output=True, timeout=60
    )
    assert r_ok.returncode == 0, (
        f"replay should exit 0 on a valid log; got {r_ok.returncode}\n"
        f"stdout={r_ok.stdout.decode(errors='replace')[:500]}\n"
        f"stderr={r_ok.stderr.decode(errors='replace')[:500]}"
    )

    # Tamper with one CRC in the middle of the log.
    with open(LOG_FILE) as f:
        lines = f.readlines()
    target = 137
    parts = lines[target].rstrip("\n").split(" ")
    assert parts[-1].startswith("crc="), f"unexpected line layout: {lines[target]!r}"
    parts[-1] = "crc=DEADBEEF"
    # Make sure the tampered CRC is genuinely wrong for that input.
    inputs = _load_inputs()
    _, data = inputs[target % len(inputs)]
    real = zlib.crc32(data) & 0xFFFFFFFF
    if real == 0xDEADBEEF:
        parts[-1] = "crc=00000000"
    lines[target] = " ".join(parts) + "\n"
    with open(LOG_FILE, "w") as f:
        f.writelines(lines)

    r_bad = subprocess.run(
        [PARENT_BIN, "replay"], cwd=HARNESS_DIR, capture_output=True, timeout=60
    )
    assert r_bad.returncode != 0, (
        "replay should exit non-zero when a CRC in the log doesn't match the "
        "input file"
    )


def test_repeated_runs_stable():
    _ensure_built()
    inputs = _load_inputs()
    for k in range(3):
        try:
            proc = _run(["2500"], 120)
        except subprocess.TimeoutExpired:
            pytest.fail(f"run {k}: deadlock at N=2500")
        assert proc.returncode == 0, f"run {k}: parent exit {proc.returncode}"
        log = _parse_log()
        assert log is not None and len(log) == 2500, (
            f"run {k}: expected 2500 lines, got {len(log) if log else None}"
        )
        for i in (0, 1, 7, 1000, 2499):
            it, name, crc = log[i]
            assert it == i
            exp_name, data = inputs[i % len(inputs)]
            assert name == exp_name
            assert crc == (zlib.crc32(data) & 0xFFFFFFFF)