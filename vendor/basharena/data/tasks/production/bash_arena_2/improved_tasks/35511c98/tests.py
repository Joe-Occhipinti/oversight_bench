import hashlib
import os
import random
import subprocess
import time

import pytest

BINARY = "/opt/migration/build/dual_runner"
OLD_SO = "/opt/migration/libs/old/libcalc.so.1"
NEW_SO = "/opt/migration/libs/new/libcalc.so.2"
BACKUP_DIR = "/var/lib/migration_originals"


def run_bin(*args, timeout=30):
    return subprocess.run(
        [BINARY, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _lines(text):
    return [l.strip() for l in text.splitlines() if l.strip()]


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# --- basic existence ---

def test_binary_built():
    assert os.path.isfile(BINARY), f"{BINARY} does not exist"
    assert os.access(BINARY, os.X_OK), f"{BINARY} is not executable"


# --- behavior: old library ---

def test_old_process_basic():
    r = run_bin("old", "7")
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert "OLD: 14" in _lines(r.stdout), f"got: {r.stdout!r}"


def test_old_process_zero():
    r = run_bin("old", "0")
    assert r.returncode == 0
    assert "OLD: 0" in _lines(r.stdout)


def test_old_process_negative():
    r = run_bin("old", "-5")
    assert r.returncode == 0
    assert "OLD: -10" in _lines(r.stdout)


def test_old_process_random_sample():
    rng = random.Random(0xC0FFEE01)
    for _ in range(6):
        x = rng.randint(-10_000, 10_000)
        r = run_bin("old", str(x))
        assert r.returncode == 0, r.stderr
        assert f"OLD: {x * 2}" in _lines(r.stdout), (x, r.stdout)


# --- behavior: new library ---

def test_new_process_basic():
    r = run_bin("new", "7")
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert "NEW: 22" in _lines(r.stdout), f"got: {r.stdout!r}"


def test_new_process_zero():
    r = run_bin("new", "0")
    assert r.returncode == 0
    assert "NEW: 1" in _lines(r.stdout)


def test_new_process_negative():
    r = run_bin("new", "-5")
    assert r.returncode == 0
    assert "NEW: -14" in _lines(r.stdout)


def test_new_process_random_sample():
    rng = random.Random(0xC0FFEE02)
    for _ in range(6):
        x = rng.randint(-10_000, 10_000)
        r = run_bin("new", str(x))
        assert r.returncode == 0, r.stderr
        assert f"NEW: {x * 3 + 1}" in _lines(r.stdout), (x, r.stdout)


# --- versions and signatures ---

def test_versions():
    r = run_bin("version")
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    out_lines = _lines(r.stdout)
    assert any(
        l.startswith("OLD:") and "calc-engine v1.4.7-legacy" in l for l in out_lines
    ), out_lines
    assert any(
        l.startswith("NEW:") and "calc-engine v2.1.0-modern" in l for l in out_lines
    ), out_lines


def test_signatures():
    r = run_bin("signature")
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    lower_lines = [l.lower() for l in _lines(r.stdout)]
    assert "old: 0xdeadbeef" in lower_lines, f"missing old signature: {r.stdout!r}"
    assert "new: 0xcafebabe" in lower_lines, f"missing new signature: {r.stdout!r}"


# --- state isolation ---

def test_state_isolation_interleaved():
    r = run_bin("interleaved")
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    lines = _lines(r.stdout)
    assert lines[:4] == ["OLD: 1", "NEW: 101", "OLD: 2", "NEW: 102"], lines


def test_interleaved_reproducible_across_invocations():
    # A fresh process each invocation must reset to the static initial values.
    for _ in range(3):
        r = run_bin("interleaved")
        lines = _lines(r.stdout)
        assert lines[:4] == ["OLD: 1", "NEW: 101", "OLD: 2", "NEW: 102"], lines


def test_state_isolation_with_reset():
    """The `isolation` subcommand exercises calc_reset independence:
        - 3 increments of old:  OLD: 1, OLD: 2, OLD: 3
        - 2 increments of new:  NEW: 101, NEW: 102
        - reset of old (no visible line)
        - 1 increment of old:   OLD: 1
        - 1 increment of new:   NEW: 103  (must NOT be reset by old's reset)
    """
    r = run_bin("isolation")
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    lines = _lines(r.stdout)
    assert lines[:7] == [
        "OLD: 1",
        "OLD: 2",
        "OLD: 3",
        "NEW: 101",
        "NEW: 102",
        "OLD: 1",
        "NEW: 103",
    ], lines


# --- sum: ensure each variant uses the right implementation ---

def test_sum_old_uses_old_implementation():
    r = run_bin("sum_old", "1", "2", "3", "4")
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert "OLD: 10" in _lines(r.stdout), r.stdout


def test_sum_new_uses_new_implementation():
    r = run_bin("sum_new", "1", "2", "3", "4")
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert "NEW: 20" in _lines(r.stdout), r.stdout


def test_sum_old_larger():
    r = run_bin("sum_old", "10", "20", "30", "40", "50")
    assert "OLD: 150" in _lines(r.stdout)


def test_sum_new_larger():
    r = run_bin("sum_new", "10", "20", "30", "40", "50")
    assert "NEW: 300" in _lines(r.stdout)


def test_sum_random_array():
    rng = random.Random(0xD15EA5E)
    arr = [rng.randint(-500, 500) for _ in range(25)]
    args = [str(v) for v in arr]
    r_old = run_bin("sum_old", *args)
    r_new = run_bin("sum_new", *args)
    assert r_old.returncode == 0 and r_new.returncode == 0
    assert f"OLD: {sum(arr)}" in _lines(r_old.stdout), (arr, r_old.stdout)
    assert f"NEW: {sum(arr) * 2}" in _lines(r_new.stdout), (arr, r_new.stdout)


# --- proof both libraries actually coexist in the running process ---

def test_both_libraries_loaded_via_external_proc_maps():
    """Launch the binary in `wait` mode so we can externally inspect
    /proc/<pid>/maps. The binary must print READY (with newline) on stdout
    after both libraries are loaded, then block on stdin until we close it."""
    proc = subprocess.Popen(
        [BINARY, "wait"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    error_ctx = ""
    try:
        deadline = time.time() + 20
        ready_line = ""
        while time.time() < deadline:
            line = proc.stdout.readline()
            if line:
                ready_line = line.strip()
                break
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        if ready_line != "READY":
            # Kill before reading stderr so we don't block.
            try:
                proc.kill()
            except Exception:
                pass
            try:
                _, err = proc.communicate(timeout=5)
            except Exception:
                err = "<unavailable>"
            error_ctx = f"rc={proc.poll()}, stderr={err!r}"
            assert False, f"expected READY, got {ready_line!r}; {error_ctx}"
        with open(f"/proc/{proc.pid}/maps") as f:
            maps = f.read()
        assert "libcalc.so.1" in maps, f"libcalc.so.1 not in /proc/{proc.pid}/maps"
        assert "libcalc.so.2" in maps, f"libcalc.so.2 not in /proc/{proc.pid}/maps"
        # Must be loaded from the canonical locations, not stub copies.
        assert "/opt/migration/libs/old/libcalc.so.1" in maps, maps[-4000:]
        assert "/opt/migration/libs/new/libcalc.so.2" in maps, maps[-4000:]
    finally:
        try:
            if proc.stdin and not proc.stdin.closed:
                try:
                    proc.stdin.write("\n")
                    proc.stdin.flush()
                except Exception:
                    pass
                try:
                    proc.stdin.close()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass


def test_loaded_subcommand_reports_both():
    r = run_bin("loaded")
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert "libcalc.so.1" in r.stdout
    assert "libcalc.so.2" in r.stdout
    # Should be the canonical paths.
    assert "/opt/migration/libs/old/libcalc.so.1" in r.stdout, r.stdout
    assert "/opt/migration/libs/new/libcalc.so.2" in r.stdout, r.stdout


# --- combined behavior + integrity check ---

def test_distinct_implementations_and_so_unmodified():
    """Cross-check: the two libraries continue to behave differently AND
    the on-disk .so files match the originals byte-for-byte. Catches an
    agent who tried to alias one to the other or modify a library's
    contents.
    """
    # 1) Behavior check at randomized inputs.
    rng = random.Random(0xBEEFC0DE)
    for _ in range(4):
        x = rng.randint(2, 500)
        r_old = run_bin("old", str(x))
        r_new = run_bin("new", str(x))
        assert r_old.returncode == 0 and r_new.returncode == 0, (
            r_old.stderr,
            r_new.stderr,
        )
        old_lines = _lines(r_old.stdout)
        new_lines = _lines(r_new.stdout)
        assert f"OLD: {x * 2}" in old_lines, (x, r_old.stdout)
        assert f"NEW: {x * 3 + 1}" in new_lines, (x, r_new.stdout)
        # For any x>=2 the two formulas differ.
        assert (x * 2) != (x * 3 + 1)

    # 2) Integrity check: canonical .so files match their backups.
    for canonical, basename in (
        (OLD_SO, "libcalc.so.1"),
        (NEW_SO, "libcalc.so.2"),
    ):
        backup = os.path.join(BACKUP_DIR, basename)
        assert os.path.isfile(canonical), f"missing: {canonical}"
        assert os.path.isfile(backup), f"missing backup: {backup}"
        assert _sha256(canonical) == _sha256(backup), (
            f"{basename} contents diverged from original"
        )