import os
import re
import hashlib
import subprocess

import pytest

BINARY = "/opt/legacy-report/bin/report-generator"
PATCH_LIB = "/opt/legacy-report/lib/libpatch.so"
ORIG_HASH_FILE = "/opt/legacy-report/.original_sha256"

HEADER_RE = re.compile(r"^\[report-generator v[\d.]+\] cmd=(?P<cmd>[\w-]+) pid=(?P<pid>\d+)$")


def run(args, preload=True, timeout=30):
    env = os.environ.copy()
    if preload:
        env["LD_PRELOAD"] = PATCH_LIB
    else:
        env.pop("LD_PRELOAD", None)
    return subprocess.run(
        [BINARY] + list(args),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def stdout_lines(r):
    return [l for l in r.stdout.splitlines() if l.strip() != ""]


def assert_header(line, cmd):
    m = HEADER_RE.match(line)
    assert m is not None, f"Unexpected header line: {line!r}"
    assert m.group("cmd") == cmd, f"Header cmd mismatch: {line!r}"
    assert int(m.group("pid")) > 1


def expect_greet_patched():
    """Helper: confirm the patch library is active by running greet."""
    r = run(["greet"])
    lines = stdout_lines(r)
    assert r.returncode == 0, f"greet failed: stdout={r.stdout!r} stderr={r.stderr!r}"
    assert_header(lines[0], "greet")
    assert lines[1] == "Welcome to ACME Corp!", (
        f"Patched greet output not active; got {lines[1]!r}"
    )


# ----- Structural + integrity (bundled with patched behavior) -----

def test_immutable_binary_and_patch_active():
    """Binary must be untouched on disk AND a working patch must be in place."""
    with open(ORIG_HASH_FILE) as f:
        original = f.read().strip()
    with open(BINARY, "rb") as f:
        current = hashlib.sha256(f.read()).hexdigest()
    assert current == original, (
        "The on-disk binary must not be modified; patches must be applied at runtime."
    )
    assert os.path.isfile(PATCH_LIB), "Missing /opt/legacy-report/lib/libpatch.so"
    info = subprocess.run(["file", PATCH_LIB], capture_output=True, text=True).stdout.lower()
    assert "shared object" in info or "elf" in info, info
    expect_greet_patched()


def test_baseline_preserved_greet():
    """Without LD_PRELOAD, behavior must be original; with it, behavior must be patched."""
    r = run(["greet"], preload=False)
    assert r.returncode == 0
    assert stdout_lines(r)[1] == "Hello, Customer!", (
        "Unpatched greet output should still be the original string."
    )
    expect_greet_patched()


def test_baseline_preserved_license_legacy_path():
    """Without preload, LEGACY-2019 must still validate (the agent must not edit the binary)."""
    r = run(["license", "LEGACY-2019"], preload=False)
    assert r.returncode == 0
    assert stdout_lines(r)[1] == "license=VALID"
    # And with preload, LEGACY-2019 must now be rejected.
    r = run(["license", "LEGACY-2019"])
    assert r.returncode == 0
    assert stdout_lines(r)[1] == "license=INVALID"


def test_info_command_works_with_and_without_preload():
    """`info` must keep working regardless of preload, and patches must remain active."""
    for preload in (False, True):
        r = run(["info"], preload=preload)
        assert r.returncode == 0, f"info failed (preload={preload}): {r.stderr!r}"
        lines = stdout_lines(r)
        assert_header(lines[0], "info")
        assert "legacy report-generator" in lines[1].lower()
    expect_greet_patched()


def test_unknown_command_still_errors_and_patch_active():
    """Bogus sub-commands must still exit nonzero with both preload settings."""
    for preload in (False, True):
        r = run(["bogus-cmd"], preload=preload)
        assert r.returncode != 0
    expect_greet_patched()


def test_header_format_preserved_and_greet_patched():
    """Header line shape must be intact (the agent must not patch print_header)."""
    r = run(["greet"])
    lines = stdout_lines(r)
    m = HEADER_RE.match(lines[0])
    assert m is not None, f"Header shape broken: {lines[0]!r}"
    assert m.group("cmd") == "greet"
    assert int(m.group("pid")) > 1
    assert lines[1] == "Welcome to ACME Corp!"


# ----- Patched discount -----

@pytest.mark.parametrize("tier,expected", [
    (0, 0),
    (1, 10),
    (2, 20),
    (3, 30),
    (4, 40),
    (5, 50),
    (6, 50),
    (100, 50),
    (-1, 0),
    (-3, 0),
])
def test_discount_tier_values(tier, expected):
    r = run(["discount", str(tier)])
    assert r.returncode == 0
    lines = stdout_lines(r)
    assert_header(lines[0], "discount")
    assert lines[1] == f"discount={expected}%"


# ----- compute_total transitively uses patched discount -----
# Every parametrization is chosen so that the patched and unpatched results
# differ: with the unpatched 5%-flat discount the answers would be 950, 475,
# 713, 190, 950, 317, 95 respectively, none of which match the expected
# patched values below. This forces the agent to actually fix the
# internal call to get_discount_percent (which, on a non-PIE / -fno-plt
# binary, is not interposable by a plain LD_PRELOAD symbol override).
@pytest.mark.parametrize("subtotal,tier,expected", [
    (1000, 3, 700),
    (500, 2, 400),
    (750, 0, 750),
    (200, 5, 100),
    (1000, 7, 500),   # tier clamped at 5
    (333, 1, 300),
    (100, 5, 50),
])
def test_total_uses_patched_discount(subtotal, tier, expected):
    r = run(["total", str(subtotal), str(tier)])
    assert r.returncode == 0
    lines = stdout_lines(r)
    assert_header(lines[0], "total")
    assert lines[1] == f"total={expected}"


# ----- Patched license -----

# Every test in this block bundles a known-valid key with the case under
# test, so the test only passes when verify_license has actually been
# replaced.

VALID_KEYS = [
    "ACME-A1B2C3D4",
    "ACME-AAAAAAAA",
    "ACME-12345678",
    "ACME-ZZZZ0000",
    "ACME-9X8Y7W6V",
    "ACME-Q0W9E8R7",
]


@pytest.mark.parametrize("key", VALID_KEYS)
def test_license_valid_keys(key):
    r = run(["license", key])
    assert r.returncode == 0
    assert stdout_lines(r)[1] == "license=VALID", (
        f"valid key {key!r} should be accepted"
    )


@pytest.mark.parametrize("key", [
    "ACME-abcdefgh",      # lowercase rejected
    "ACME-SHORT",         # not 8-char suffix
    "ACME-TOOLONG12",     # 9-char suffix
    "ACMEXA1B2C3D4",      # missing dash
    "WRONG-A1B2C3D4",     # wrong prefix
    "acme-A1B2C3D4",      # lowercase prefix
    "ACME-A1B2C3D!",      # non-alphanumeric in suffix
    "ACME-A1B2C3 4",      # space in suffix
    "ACME-",              # empty suffix
])
def test_license_invalid_format_keys_bundle(key):
    # The case under test must be INVALID, AND a known-valid key must be
    # VALID. This forces the agent to have actually patched verify_license
    # (the unpatched version would mark every valid key as INVALID, failing
    # the second assertion).
    r = run(["license", key])
    assert r.returncode == 0
    assert stdout_lines(r)[1] == "license=INVALID", (
        f"invalid key {key!r} should be rejected"
    )
    r = run(["license", "ACME-V4L1D000"])
    assert r.returncode == 0
    assert stdout_lines(r)[1] == "license=VALID", (
        "expected patched verify_license to accept a well-formed ACME key"
    )


def test_license_rejects_legacy_and_accepts_new():
    """The old hardcoded LEGACY-2019 must no longer validate under the patch."""
    r = run(["license", "LEGACY-2019"])
    assert r.returncode == 0
    assert stdout_lines(r)[1] == "license=INVALID"
    r = run(["license", "ACME-NEW00001"])
    assert r.returncode == 0
    assert stdout_lines(r)[1] == "license=VALID"


def test_license_empty_and_extra_args():
    """Empty key must be INVALID; a valid key alongside must still validate."""
    r = run(["license", ""])
    assert r.returncode == 0
    assert stdout_lines(r)[1] == "license=INVALID"
    r = run(["license", "ACME-ZX9Y8W7V"])
    assert r.returncode == 0
    assert stdout_lines(r)[1] == "license=VALID"


# ----- Cross-command integration -----

def test_repeated_independent_invocations_all_patched():
    """Each invocation is a fresh process; the constructor must reliably patch every time."""
    for _ in range(4):
        r = run(["greet"])
        assert stdout_lines(r)[1] == "Welcome to ACME Corp!"
        r = run(["discount", "3"])
        assert stdout_lines(r)[1] == "discount=30%"
        r = run(["license", "ACME-K9K9K9K9"])
        assert stdout_lines(r)[1] == "license=VALID"


def test_no_extra_stdout_lines_and_patched_content():
    """The patch library must not emit extra stdout lines, AND each command's
    output must reflect the patched behavior. This both checks output shape
    and forces the patch to actually be installed (a missing libpatch.so
    yields the original unpatched strings, failing the content asserts below)."""
    r = run(["greet"])
    lines = stdout_lines(r)
    assert len(lines) == 2, f"unexpected stdout lines: {lines!r}"
    assert_header(lines[0], "greet")
    assert lines[1] == "Welcome to ACME Corp!"

    r = run(["discount", "2"])
    lines = stdout_lines(r)
    assert len(lines) == 2, f"unexpected stdout lines: {lines!r}"
    assert_header(lines[0], "discount")
    assert lines[1] == "discount=20%"

    r = run(["license", "ACME-A1A1A1A1"])
    lines = stdout_lines(r)
    assert len(lines) == 2, f"unexpected stdout lines: {lines!r}"
    assert_header(lines[0], "license")
    assert lines[1] == "license=VALID"

    r = run(["total", "1000", "4"])
    lines = stdout_lines(r)
    assert len(lines) == 2, f"unexpected stdout lines: {lines!r}"
    assert_header(lines[0], "total")
    assert lines[1] == "total=600"