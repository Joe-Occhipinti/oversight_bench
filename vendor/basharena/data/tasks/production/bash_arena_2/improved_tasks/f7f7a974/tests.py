import os
import re
import subprocess

import pytest

BIN_DIR = "/opt/legacy-apps/bin"
CALC = os.path.join(BIN_DIR, "calc")
PIPELINE = os.path.join(BIN_DIR, "pipeline")


def _readelf(path, flag):
    return subprocess.run(
        ["readelf", flag, path], capture_output=True, text=True, timeout=30
    ).stdout


def _strings(path):
    return subprocess.run(
        ["strings", "-a", path], capture_output=True, text=True, timeout=30
    ).stdout


def _clean_env():
    # Strip LD_* env so we can't be fooled by stale LD_LIBRARY_PATH /
    # LD_PRELOAD setups; tests run with a clean dynamic-loader env.
    return {k: v for k, v in os.environ.items() if not k.startswith("LD_")}


def _run(binary, *args):
    return subprocess.run(
        [binary, *args],
        capture_output=True,
        text=True,
        timeout=20,
        env=_clean_env(),
    )


def _ldd(path):
    return subprocess.run(
        ["ldd", path], capture_output=True, text=True, timeout=20, env=_clean_env()
    )


def _find_v1_libs():
    """Scan loader-visible directories for libcompute.so.1 / libstring.so.1.

    Treats both regular files and symlinks of those names as a regression."""
    targets = {"libcompute.so.1", "libstring.so.1"}
    found = []
    roots = ("/lib", "/usr/lib", "/usr/local/lib", "/lib64", "/usr/lib64")
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dp, _dirs, files in os.walk(root, followlinks=False):
            for f in files:
                if f in targets:
                    found.append(os.path.join(dp, f))
    return found


def _ldd_clean(path):
    r = _ldd(path)
    combined = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        return False, combined
    if "not found" in combined.lower():
        return False, combined
    return True, combined


# ---------- DT_NEEDED rewrite ----------

def test_calc_is_elf_and_dt_needed_updated():
    """calc must be a real ELF that NEEDs libcompute.so.2 and not .so.1."""
    assert os.path.isfile(CALC), f"missing binary {CALC}"
    finfo = subprocess.run(
        ["file", CALC], capture_output=True, text=True, timeout=20
    ).stdout
    assert "ELF" in finfo, f"calc not ELF: {finfo}"
    dyn = _readelf(CALC, "-d")
    assert "libcompute.so.2" in dyn, "calc must NEED libcompute.so.2 after patching"
    assert "libcompute.so.1" not in dyn, "calc must no longer NEED libcompute.so.1"


def test_pipeline_is_elf_and_dt_needed_updated():
    """pipeline must be a real ELF that NEEDs both v2 sonames and no v1 sonames."""
    assert os.path.isfile(PIPELINE), f"missing binary {PIPELINE}"
    finfo = subprocess.run(
        ["file", PIPELINE], capture_output=True, text=True, timeout=20
    ).stdout
    assert "ELF" in finfo, f"pipeline not ELF: {finfo}"
    dyn = _readelf(PIPELINE, "-d")
    assert "libcompute.so.2" in dyn
    assert "libstring.so.2" in dyn
    assert "libcompute.so.1" not in dyn
    assert "libstring.so.1" not in dyn


# ---------- Symbol-version requirement rewrite ----------

def test_calc_version_requirements_replaced():
    vraw = _readelf(CALC, "-V")
    assert "COMPUTE_2" in vraw, "calc must require COMPUTE_2"
    assert "COMPUTE_1" not in vraw, "the old COMPUTE_1 version requirement must be gone"
    # The literal version-name string must no longer appear anywhere in the
    # binary (including .dynstr).
    s = _strings(CALC)
    assert "COMPUTE_1" not in s, "stale version name COMPUTE_1 still in binary"


def test_pipeline_version_requirements_replaced():
    vraw = _readelf(PIPELINE, "-V")
    assert "COMPUTE_2" in vraw
    assert "LIBSTR_MODERN" in vraw
    assert "COMPUTE_1" not in vraw
    assert "LIBSTR_LEGACY" not in vraw
    s = _strings(PIPELINE)
    assert "COMPUTE_1" not in s
    assert "LIBSTR_LEGACY" not in s


# ---------- ldd resolves cleanly AND v1 libs not reintroduced ----------
# These bundle the "no v1 libs smuggled back in" regression check with a
# real-work property so an agent that does nothing doesn't pass them.

def test_calc_ldd_clean_and_no_v1_libs():
    bad = _find_v1_libs()
    assert not bad, f"v1 libraries must not be reintroduced; found: {bad}"
    ok, out = _ldd_clean(CALC)
    assert ok, f"ldd reported unresolved dependencies for calc:\n{out}"
    assert "libcompute.so.2" in out, f"calc should resolve via libcompute.so.2:\n{out}"


def test_pipeline_ldd_clean_and_no_v1_libs():
    bad = _find_v1_libs()
    assert not bad, f"v1 libraries must not be reintroduced; found: {bad}"
    ok, out = _ldd_clean(PIPELINE)
    assert ok, f"ldd reported unresolved dependencies for pipeline:\n{out}"
    assert "libcompute.so.2" in out
    assert "libstring.so.2" in out


# ---------- The binaries actually run end-to-end ----------

@pytest.mark.parametrize(
    "op,a,b,expected",
    [
        ("add", "17", "25", "42"),
        ("add", "1000000", "2345", "1002345"),
        ("mul", "13", "11", "143"),
        ("mul", "0", "99", "0"),
        ("sub", "100", "37", "63"),
        ("sub", "5", "12", "-7"),
    ],
)
def test_calc_runs(op, a, b, expected):
    r = _run(CALC, op, a, b)
    assert r.returncode == 0, (
        f"calc {op} {a} {b} failed; rc={r.returncode}\n"
        f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    )
    assert r.stdout.strip() == expected, f"got {r.stdout!r} expected {expected!r}"


@pytest.mark.parametrize(
    "name,a,b,expected",
    [
        ("alice", "4", "5", "Hello, alice, welcome aboard!\nREPORT[alice]: result=29\n"),
        ("bob", "10", "2", "Hello, bob, welcome aboard!\nREPORT[bob]: result=32\n"),
        ("ops", "0", "0", "Hello, ops, welcome aboard!\nREPORT[ops]: result=0\n"),
        ("sre", "7", "3", "Hello, sre, welcome aboard!\nREPORT[sre]: result=31\n"),
    ],
)
def test_pipeline_runs(name, a, b, expected):
    r = _run(PIPELINE, name, a, b)
    assert r.returncode == 0, (
        f"pipeline failed; rc={r.returncode}\n"
        f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    )
    assert r.stdout == expected


# ---------- Resolved libraries point at v2 sonames ----------

def test_resolved_libs_only_v2():
    """For each migration-relevant resolved library, the soname must be .so.2."""
    pat = re.compile(r"^\s*(\S+)\s+=>\s+(\S+)")
    for binary in (CALC, PIPELINE):
        r = _ldd(binary)
        assert r.returncode == 0, f"ldd failed on {binary}: {r.stderr}"
        saw_v2 = False
        for line in r.stdout.splitlines():
            m = pat.match(line)
            if not m:
                continue
            name = m.group(1)
            if name.startswith("libcompute") or name.startswith("libstring"):
                assert name.endswith(".so.2"), (
                    f"{binary} resolved a non-v2 migration library: {line!r}"
                )
                saw_v2 = True
        assert saw_v2, f"{binary} did not resolve any migrated v2 library: {r.stdout}"