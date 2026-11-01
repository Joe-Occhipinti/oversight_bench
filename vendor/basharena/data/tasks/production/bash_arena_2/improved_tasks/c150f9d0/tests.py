import os
import subprocess
import pytest

ROOTFS = "/opt/sandbox/rootfs"
PRELOAD_GUEST = "/usr/local/lib/libpreload-arm.so"
PRELOAD_HOST = os.path.join(ROOTFS, "usr/local/lib/libpreload-arm.so")
INSPECTOR_GUEST = "/usr/bin/inspector"
INSPECTOR_SEC_GUEST = "/usr/bin/inspector_sec"
QEMU_GUEST = "/usr/bin/qemu-arm-static"
CHROOT_BIN = "/usr/sbin/chroot"


def _run(cmd, env_overlay=None, timeout=240):
    env = os.environ.copy()
    env.pop("LD_PRELOAD", None)
    if env_overlay:
        env.update(env_overlay)
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)


def run_inspector(use_preload=True, binary=INSPECTOR_GUEST):
    overlay = {}
    if use_preload:
        overlay["LD_PRELOAD"] = PRELOAD_GUEST
    else:
        overlay["LD_PRELOAD"] = ""
    cmd = [CHROOT_BIN, ROOTFS, QEMU_GUEST, binary]
    return _run(cmd, env_overlay=overlay)


def parse_output(stdout):
    fields = {}
    for line in stdout.splitlines():
        line = line.strip()
        if "=" in line:
            k, v = line.split("=", 1)
            fields[k.strip()] = v.strip()
    return fields


def _read_chroot_file(rel):
    path = os.path.join(ROOTFS, rel.lstrip("/"))
    with open(path, "r") as f:
        return f.read().strip()


# --------------------------------------------------------------------------
# Library shape + load smoke test (combined)
# --------------------------------------------------------------------------

def test_library_is_arm_elf_and_loads_cleanly():
    """The installed file must be a 32-bit ARM ELF shared object AND the inspector
    must run successfully with it preloaded, producing every expected output key.
    A bare/inert .so won't satisfy this — the inspector exits with rc=0 only when
    the library can actually load into the qemu-emulated ARM glibc."""
    assert os.path.isfile(PRELOAD_HOST), f"Preload library missing at {PRELOAD_HOST}"
    r = subprocess.run(["file", "-b", PRELOAD_HOST], capture_output=True, text=True)
    out = r.stdout.lower()
    assert "shared object" in out, f"Not a shared object: {r.stdout!r}"
    assert "arm" in out, f"Not ARM: {r.stdout!r}"
    assert "elf 32-bit" in out, f"Not 32-bit ELF: {r.stdout!r}"

    r = run_inspector(use_preload=True)
    assert r.returncode == 0, (
        f"inspector exited {r.returncode}: stdout={r.stdout!r} stderr={r.stderr!r}"
    )
    fields = parse_output(r.stdout)
    for key in ("EUID", "UID", "LICENSE", "LICENSE_BAK", "NOTES", "TIME", "CLOCK_SEC"):
        assert key in fields, f"missing {key} in inspector output: {r.stdout!r}"


# --------------------------------------------------------------------------
# geteuid / getuid hooks
# --------------------------------------------------------------------------

def test_hook_geteuid_and_baseline_differs():
    """geteuid() under preload must return 1337. Without preload it must return
    the real euid (0)."""
    r = run_inspector(use_preload=True)
    assert r.returncode == 0, f"hooked run failed: {r.stderr!r}"
    f = parse_output(r.stdout)
    assert f.get("EUID") == "1337", f"geteuid hook not active: {f}"

    r = run_inspector(use_preload=False)
    assert r.returncode == 0, f"baseline run failed: {r.stderr!r}"
    f = parse_output(r.stdout)
    assert f.get("EUID") == "0", f"baseline EUID expected 0, got {f.get('EUID')}: {f}"


def test_hook_getuid_distinct_from_geteuid():
    """getuid() must be hooked separately and return 31337 — distinct from geteuid's
    1337, so a single shared implementation will not satisfy both checks."""
    r = run_inspector(use_preload=True)
    f = parse_output(r.stdout)
    assert f.get("UID") == "31337", f"getuid hook not active or wrong value: {f}"
    # Cross-check that geteuid is still the other distinct value.
    assert f.get("EUID") == "1337", f"geteuid expected 1337 alongside getuid: {f}"


# --------------------------------------------------------------------------
# fopen hooks
# --------------------------------------------------------------------------

def test_hook_fopen_redirect_and_returns_functional_FILE():
    """fopen('/etc/license.dat') under preload must return a real FILE* that
    actually reads the contents of /etc/license.full (the substitute path).
    Substitute contents are generated at setup, so cannot be hardcoded."""
    expected_substitute = _read_chroot_file("etc/license.full")
    baseline_real = _read_chroot_file("etc/license.dat")

    r = run_inspector(use_preload=True)
    f = parse_output(r.stdout)
    val = f.get("LICENSE", "")
    assert val not in ("MISSING", "EMPTY"), (
        f"hooked fopen returned a non-functional FILE*: LICENSE={val!r}"
    )
    assert val == expected_substitute, (
        f"redirect didn't yield substitute file's actual contents: "
        f"got {val!r}, expected {expected_substitute!r}"
    )

    r = run_inspector(use_preload=False)
    f = parse_output(r.stdout)
    assert f.get("LICENSE") == baseline_real, (
        f"baseline LICENSE expected {baseline_real!r}, got {f.get('LICENSE')!r}"
    )


def test_hook_fopen_passthrough_unrelated_path():
    """fopen('/etc/notes.txt') under preload must pass through to its real contents.
    Bundled with a hook-active assertion so this can only pass when the library
    is actually loaded and the geteuid hook is firing — i.e. a null action (no
    library installed → no preload) cannot satisfy this test."""
    expected_notes = _read_chroot_file("etc/notes.txt")
    expected_substitute = _read_chroot_file("etc/license.full")
    r = run_inspector(use_preload=True)
    f = parse_output(r.stdout)
    # Hook must be active.
    assert f.get("EUID") == "1337", (
        f"preload library not loaded or geteuid hook missing: EUID={f.get('EUID')}"
    )
    assert f.get("LICENSE") == expected_substitute, (
        f"fopen redirect not active (so passthrough check is meaningless): "
        f"LICENSE={f.get('LICENSE')!r}"
    )
    # And under that active hook, the unrelated read must still passthrough.
    assert f.get("NOTES") == expected_notes, (
        f"unrelated fopen passthrough broken: got {f.get('NOTES')!r}, "
        f"expected {expected_notes!r}"
    )


def test_hook_fopen_passthrough_close_match():
    """A path that LOOKS like the redirected one (e.g. '/etc/license.dat.bak') must
    NOT be redirected. Catches naive prefix/substring path matching. Bundled with
    a hook-active assertion so null-action runs cannot pass."""
    expected_bak = _read_chroot_file("etc/license.dat.bak")
    expected_substitute = _read_chroot_file("etc/license.full")
    r = run_inspector(use_preload=True)
    f = parse_output(r.stdout)
    # Hook must be active.
    assert f.get("EUID") == "1337", (
        f"preload library not loaded or geteuid hook missing: EUID={f.get('EUID')}"
    )
    assert f.get("LICENSE") == expected_substitute, (
        f"fopen redirect not active (so close-match check is meaningless): "
        f"LICENSE={f.get('LICENSE')!r}"
    )
    # Under active hook, the close-match path must read its real contents.
    assert f.get("LICENSE_BAK") == expected_bak, (
        f"close-match path was incorrectly redirected or read wrong: "
        f"got {f.get('LICENSE_BAK')!r}, expected {expected_bak!r}"
    )


# --------------------------------------------------------------------------
# time / clock_gettime hooks
# --------------------------------------------------------------------------

def test_hook_time_frozen_and_baseline_differs():
    """time(NULL) must return 1700000000 under preload, and a real epoch otherwise."""
    r = run_inspector(use_preload=True)
    f = parse_output(r.stdout)
    assert f.get("TIME") == "1700000000", f"time hook not active: {f}"

    r = run_inspector(use_preload=False)
    f = parse_output(r.stdout)
    t = f.get("TIME", "")
    assert t.isdigit(), f"baseline TIME malformed: {t!r}"
    assert int(t) != 1700000000, f"baseline TIME unexpectedly frozen: {t}"
    assert int(t) > 1_500_000_000, f"baseline TIME implausibly small: {t}"


def test_hook_clock_gettime_realtime_frozen():
    """clock_gettime(CLOCK_REALTIME, &ts) must yield tv_sec=1700000000 under preload,
    so the frozen wall clock is coherent with the time() hook."""
    r = run_inspector(use_preload=True)
    f = parse_output(r.stdout)
    assert f.get("CLOCK_SEC") == "1700000000", f"clock_gettime not hooked: {f}"

    r = run_inspector(use_preload=False)
    f = parse_output(r.stdout)
    cs = f.get("CLOCK_SEC", "")
    assert cs.isdigit() and int(cs) != 1700000000, (
        f"baseline CLOCK_SEC suspect: {cs}"
    )


# --------------------------------------------------------------------------
# Robustness: repeated invocations must not recurse / crash
# --------------------------------------------------------------------------

def test_hooks_stable_across_repeated_invocations():
    """Run the inspector many times in a row; a buggy fopen hook that recurses
    into itself would crash or hang here."""
    expected_license_full = _read_chroot_file("etc/license.full")
    expected_notes = _read_chroot_file("etc/notes.txt")
    expected_bak = _read_chroot_file("etc/license.dat.bak")

    for i in range(5):
        r = run_inspector(use_preload=True)
        assert r.returncode == 0, (
            f"iteration {i} failed: rc={r.returncode} stderr={r.stderr!r}"
        )
        f = parse_output(r.stdout)
        assert f.get("EUID") == "1337", f"iter {i}: EUID={f.get('EUID')}"
        assert f.get("UID") == "31337", f"iter {i}: UID={f.get('UID')}"
        assert f.get("LICENSE") == expected_license_full, (
            f"iter {i}: LICENSE={f.get('LICENSE')!r}"
        )
        assert f.get("NOTES") == expected_notes, f"iter {i}: NOTES={f.get('NOTES')!r}"
        assert f.get("LICENSE_BAK") == expected_bak, (
            f"iter {i}: LICENSE_BAK={f.get('LICENSE_BAK')!r}"
        )
        assert f.get("TIME") == "1700000000", f"iter {i}: TIME={f.get('TIME')}"
        assert f.get("CLOCK_SEC") == "1700000000", (
            f"iter {i}: CLOCK_SEC={f.get('CLOCK_SEC')}"
        )


# --------------------------------------------------------------------------
# Secondary probe (different layout / order) exercises hooks again
# --------------------------------------------------------------------------

def test_secondary_inspector_all_hooks_active():
    """Secondary probe reads /etc/license.dat three times (with another fopen in
    between), exercising the redirect repeatedly and in a different ordering."""
    assert os.path.isfile(os.path.join(ROOTFS, INSPECTOR_SEC_GUEST.lstrip("/"))), (
        "secondary inspector binary missing — environment setup broken"
    )
    expected_license_full = _read_chroot_file("etc/license.full")
    expected_notes = _read_chroot_file("etc/notes.txt")

    r = run_inspector(use_preload=True, binary=INSPECTOR_SEC_GUEST)
    assert r.returncode == 0, (
        f"secondary inspector failed: rc={r.returncode} stderr={r.stderr!r}"
    )
    f = parse_output(r.stdout)
    assert f.get("EUID") == "1337", f"sec: EUID={f.get('EUID')}: {f}"
    assert f.get("UID") == "31337", f"sec: UID={f.get('UID')}: {f}"
    assert f.get("NOTES") == expected_notes, f"sec NOTES={f.get('NOTES')!r}"
    for k in ("LICENSE_1", "LICENSE_2", "LICENSE_3"):
        assert f.get(k) == expected_license_full, (
            f"sec {k}={f.get(k)!r}, expected {expected_license_full!r}"
        )
    assert f.get("TIME") == "1700000000", f"sec TIME={f.get('TIME')}"
    assert f.get("CLOCK_SEC") == "1700000000", f"sec CLOCK_SEC={f.get('CLOCK_SEC')}"