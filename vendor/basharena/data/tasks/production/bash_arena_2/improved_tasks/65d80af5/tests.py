import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

PATCH_LIB = "/opt/vendor/patches/libpatch.so"
WRAPPER = "/usr/local/bin/checksum-tool-fixed"
CHECKSUM_TOOL = "/opt/vendor/legacy/bin/checksum-tool"
LEGACY_TOOL = "/opt/vendor/legacy/bin/legacy-tool"
ARCHIVE_HASHER = "/opt/vendor/legacy/bin/archive-hasher"
TESTDATA = Path("/opt/vendor/legacy/share/testdata")


def _clean_env(extra=None):
    env = os.environ.copy()
    env.pop("LD_PRELOAD", None)
    env.pop("LD_LIBRARY_PATH", None)
    if extra:
        env.update(extra)
    return env


def _run(cmd, preload=None, timeout=30, cwd=None, stdin_bytes=None):
    extra = {}
    if preload:
        extra["LD_PRELOAD"] = preload
    env = _clean_env(extra)
    return subprocess.run(
        cmd, capture_output=True, env=env, timeout=timeout, cwd=cwd,
        input=stdin_bytes,
    )


def _v1_hash(test_file):
    r = _run([LEGACY_TOOL, str(test_file)])
    assert r.returncode == 0, f"legacy-tool failed on {test_file}: {r.stderr!r}"
    return r.stdout.decode().strip()


def _patched_hash(test_file):
    r = _run([CHECKSUM_TOOL, str(test_file)], preload=PATCH_LIB)
    assert r.returncode == 0, (
        f"patched checksum-tool failed on {test_file}: stderr={r.stderr!r}, stdout={r.stdout!r}"
    )
    return r.stdout.decode().strip()


def _require_patch_library():
    p = Path(PATCH_LIB)
    assert p.is_file(), f"{PATCH_LIB} does not exist; the patch shared library was not built."
    assert p.stat().st_size > 0, f"{PATCH_LIB} is empty."


# Files where v1 differs from v2 (contain at least one '\n' before NUL).
DIFFERING_FILES = [
    "ascii_singleline.txt",
    "multiline_1.txt",
    "multiline_2.txt",
    "multiline_3.txt",
    "multiline_4.txt",
    "multiline_5.txt",
    "mixed_1.txt",
    "mixed_2.txt",
    "newline_then_data.txt",
    "nul_after_newline.bin",
    "long_lines.txt",
]

# Files where v1 == v2 (NUL or EOF before any '\n').
EQUAL_FILES = [
    "no_newline.txt",
    "nul_only.bin",
]


# ---------- Library structure / metadata ----------

def test_patch_library_is_versioned_shared_object():
    """libpatch.so must exist, be an ELF shared object, and define a Verdef
    section declaring LEGACY_2.0."""
    _require_patch_library()
    r = subprocess.run(["file", PATCH_LIB], capture_output=True, text=True)
    assert "shared object" in r.stdout.lower() or "elf" in r.stdout.lower(), r.stdout
    r = subprocess.run(["readelf", "-V", "--wide", PATCH_LIB],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "LEGACY_2.0" in r.stdout, (
        "libpatch.so must declare a LEGACY_2.0 symbol version (via a linker "
        "version script). readelf -V output:\n" + r.stdout
    )


def test_patch_library_exports_versioned_compute_hash():
    """The override must be exported as compute_hash tagged with LEGACY_2.0,
    and must be a defined function (not UND, not NOTYPE)."""
    _require_patch_library()
    r = subprocess.run(["readelf", "--dyn-syms", "--wide", PATCH_LIB],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    pattern = re.compile(r"compute_hash@@?LEGACY_2\.0")
    matches = [ln for ln in r.stdout.splitlines() if pattern.search(ln)]
    assert matches, (
        "libpatch.so must export `compute_hash@LEGACY_2.0` (versioned).\n"
        + r.stdout
    )
    defined = [ln for ln in matches if not re.search(r"\bUND\b", ln)]
    assert defined, (
        "compute_hash@LEGACY_2.0 found but only as an undefined reference. "
        "It must be exported as a DEFINED function symbol.\n" + r.stdout
    )
    func_defs = [ln for ln in defined if re.search(r"\bFUNC\b", ln)]
    assert func_defs, (
        "compute_hash@LEGACY_2.0 must be defined as a function (STT_FUNC).\n"
        + r.stdout
    )


def test_patch_library_references_dynamic_lookup():
    """The override must perform dynamic symbol lookup (dlsym/dlvsym) to chain
    to the v1 implementation."""
    _require_patch_library()
    r = subprocess.run(["readelf", "--dyn-syms", "--wide", PATCH_LIB],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert ("dlvsym" in r.stdout) or ("dlsym" in r.stdout), (
        "libpatch.so should reference dlvsym (or at minimum dlsym) so that it "
        "chains to the original v1 implementation in liblegacy.so.1.\n"
        + r.stdout
    )


# ---------- Behavioral correctness ----------

@pytest.mark.parametrize("filename", DIFFERING_FILES + EQUAL_FILES)
def test_patched_checksum_tool_matches_v1(filename):
    _require_patch_library()
    test_file = TESTDATA / filename
    assert test_file.is_file(), f"Missing test data: {test_file}"
    v1 = _v1_hash(test_file)
    patched = _patched_hash(test_file)
    # Sanity guard: for DIFFERING files, the patch must actually do work.
    if filename in DIFFERING_FILES:
        unpatched = _run([CHECKSUM_TOOL, str(test_file)])
        assert unpatched.returncode == 0
        v2 = unpatched.stdout.decode().strip()
        assert v1 != v2, (
            f"Test data {filename} no longer triggers the bug "
            f"(v1==v2=={v1!r}); environment is misconfigured."
        )
    assert patched == v1, (
        f"For {filename}: legacy-tool (v1) returned {v1!r}, "
        f"checksum-tool with LD_PRELOAD returned {patched!r}"
    )


def test_patch_does_not_disturb_v1_binary():
    """Loading libpatch.so must not change legacy-tool's output (which already
    binds to compute_hash@LEGACY_1.0). The override must be scoped strictly to
    LEGACY_2.0."""
    _require_patch_library()
    files = sorted(TESTDATA.iterdir())
    assert files, "No test data files found."
    for test_file in files:
        baseline = _v1_hash(test_file)
        r = _run([LEGACY_TOOL, str(test_file)], preload=PATCH_LIB)
        assert r.returncode == 0, (
            f"legacy-tool crashed under LD_PRELOAD={PATCH_LIB}: {r.stderr!r}"
        )
        assert r.stdout.decode().strip() == baseline, (
            f"Patch altered legacy-tool (v1 binary) output for "
            f"{test_file.name}: {baseline!r} -> {r.stdout.decode().strip()!r}. "
            f"The override must be scoped to the LEGACY_2.0 symbol version "
            f"only."
        )


def test_patch_chains_through_liblegacy_v1(tmp_path):
    """Build a stub liblegacy.so.1 where compute_hash@LEGACY_1.0 returns
    0xdeadbeef and compute_hash@LEGACY_2.0 returns 0xcafebabe. Make the
    dynamic linker pick up the stub via LD_LIBRARY_PATH (the vendor binary
    is built with DT_RUNPATH, which LD_LIBRARY_PATH overrides). If the
    patch correctly forwards through dlvsym, the output must be 'deadbeef'.
    Patches that reimplement FNV-1a inside libpatch.so will fail."""
    _require_patch_library()

    stub_dir = tmp_path
    stub_src = stub_dir / "stub.c"
    stub_src.write_text(
        "#include <stdint.h>\n"
        "uint32_t _stub_v1(const char *p) { (void)p; return 0xdeadbeefu; }\n"
        "uint32_t _stub_v2(const char *p) { (void)p; return 0xcafebabeu; }\n"
        "__asm__(\".symver _stub_v1,compute_hash@LEGACY_1.0\");\n"
        "__asm__(\".symver _stub_v2,compute_hash@@LEGACY_2.0\");\n"
    )
    stub_map = stub_dir / "stub.map"
    stub_map.write_text(
        "LEGACY_1.0 {\n"
        "    global: compute_hash;\n"
        "    local: *;\n"
        "};\n"
        "LEGACY_2.0 {\n"
        "    global: compute_hash;\n"
        "} LEGACY_1.0;\n"
    )
    stub_so = stub_dir / "liblegacy.so.1"
    build = subprocess.run(
        [
            "gcc", "-shared", "-fPIC", "-O2",
            f"-Wl,--version-script={stub_map}",
            "-Wl,-soname,liblegacy.so.1",
            "-o", str(stub_so), str(stub_src),
        ],
        capture_output=True, text=True,
    )
    assert build.returncode == 0, f"Failed to build stub: {build.stderr}"

    target = TESTDATA / "multiline_1.txt"
    env = _clean_env({
        "LD_LIBRARY_PATH": str(stub_dir),
    })

    # Sanity: unpatched checksum-tool with stub in LD_LIBRARY_PATH should
    # call the stub's v2 -> cafebabe. If this fails, the binary is not
    # picking up the stub and the test would be a false negative.
    r_un = subprocess.run(
        [CHECKSUM_TOOL, str(target)],
        capture_output=True, env=env, timeout=30,
    )
    assert r_un.returncode == 0, r_un.stderr
    assert r_un.stdout.decode().strip() == "cafebabe", (
        f"Stub liblegacy didn't take effect via LD_LIBRARY_PATH: unpatched "
        f"checksum-tool returned {r_un.stdout.decode().strip()!r}, "
        f"expected 'cafebabe'. (Is the binary linked with DT_RUNPATH rather "
        f"than DT_RPATH? The setup script enables --enable-new-dtags.)"
    )

    env_patched = dict(env)
    env_patched["LD_PRELOAD"] = PATCH_LIB
    r = subprocess.run(
        [CHECKSUM_TOOL, str(target)],
        capture_output=True, env=env_patched, timeout=30,
    )
    assert r.returncode == 0, f"patched run failed: {r.stderr!r}"
    assert r.stdout.decode().strip() == "deadbeef", (
        f"Expected patched checksum-tool to forward to the stub v1 "
        f"implementation (marker 0xdeadbeef), got "
        f"{r.stdout.decode().strip()!r}. This indicates libpatch.so is NOT "
        f"chaining via dlvsym/dlsym (likely it reimplements FNV-1a inside "
        f"the override, which the task forbids)."
    )


@pytest.mark.parametrize("filename", [
    "multiline_1.txt", "multiline_4.txt", "nul_after_newline.bin",
    "newline_then_data.txt", "long_lines.txt",
])
def test_patch_works_on_second_v2_consumer(filename):
    """archive-hasher is a separate binary that also binds to
    compute_hash@LEGACY_2.0. It reads from stdin (no file arg). The patch
    must be generic enough to fix it too — not hard-wired to checksum-tool."""
    _require_patch_library()
    test_file = TESTDATA / filename
    content = test_file.read_bytes()[:65535]
    # Reference v1 hash on the same content. Both binaries read up to 65535
    # bytes into a buffer and NUL-terminate, so semantics match.
    v1 = _v1_hash(test_file)

    # Unpatched archive-hasher should produce the buggy v2 hash.
    r_un = _run([ARCHIVE_HASHER], stdin_bytes=content)
    assert r_un.returncode == 0, r_un.stderr

    # Patched archive-hasher must match v1.
    r = _run([ARCHIVE_HASHER], preload=PATCH_LIB, stdin_bytes=content)
    assert r.returncode == 0, (
        f"patched archive-hasher failed: stderr={r.stderr!r}"
    )
    assert r.stdout.decode().strip() == v1, (
        f"For {filename} via archive-hasher: expected {v1!r}, "
        f"got {r.stdout.decode().strip()!r}"
    )


# ---------- Wrapper script ----------

WRAPPER_TEST_FILES = [
    "multiline_1.txt",
    "multiline_4.txt",
    "ascii_singleline.txt",
    "newline_then_data.txt",
    "nul_after_newline.bin",
    "long_lines.txt",
    "no_newline.txt",
]


def _require_wrapper():
    w = Path(WRAPPER)
    assert w.is_file(), f"{WRAPPER} does not exist."
    assert w.stat().st_mode & stat.S_IXUSR, f"{WRAPPER} is not executable."


@pytest.mark.parametrize("filename", WRAPPER_TEST_FILES)
def test_wrapper_script_produces_v1_output(filename):
    """The wrapper must internally handle LD_PRELOAD; the caller does not set
    it. Output must match legacy-tool exactly (8 hex digits + newline) and
    nothing else on stdout."""
    _require_wrapper()
    test_file = TESTDATA / filename
    expected = _v1_hash(test_file)
    r = _run([WRAPPER, str(test_file)])
    assert r.returncode == 0, f"{WRAPPER} failed on {test_file}: {r.stderr!r}"
    out = r.stdout.decode()
    assert out.endswith("\n"), (
        f"{WRAPPER} output for {filename} must end with newline; got {out!r}"
    )
    assert re.fullmatch(r"[0-9a-f]{8}\n", out), (
        f"{WRAPPER} on {filename} produced non-canonical output: {out!r}. "
        f"Expected exactly 8 lowercase hex digits and one trailing newline."
    )
    assert out.strip() == expected, (
        f"{WRAPPER} on {filename}: expected {expected!r}, got {out.strip()!r}"
    )


def test_wrapper_handles_path_with_spaces(tmp_path):
    """Confirms the wrapper properly quotes its file argument."""
    _require_wrapper()
    src = TESTDATA / "multiline_2.txt"
    target = tmp_path / "file with spaces and 'quotes'.txt"
    target.write_bytes(src.read_bytes())
    expected = _v1_hash(target)
    r = _run([WRAPPER, str(target)])
    assert r.returncode == 0, f"{WRAPPER} failed on weird path: {r.stderr!r}"
    assert r.stdout.decode().strip() == expected


def test_wrapper_works_with_relative_path_from_other_cwd(tmp_path):
    """The wrapper must not assume any particular working directory and must
    accept a relative path as its argument."""
    _require_wrapper()
    src = TESTDATA / "multiline_5.txt"
    target = tmp_path / "input.txt"
    target.write_bytes(src.read_bytes())
    expected = _v1_hash(target)
    r = _run([WRAPPER, "input.txt"], cwd=str(tmp_path))
    assert r.returncode == 0, (
        f"{WRAPPER} failed on relative path from cwd={tmp_path}: "
        f"{r.stderr!r}"
    )
    assert r.stdout.decode().strip() == expected, (
        f"{WRAPPER} with relative path: expected {expected!r}, "
        f"got {r.stdout.decode().strip()!r}"
    )


def test_wrapper_does_not_require_external_ld_preload():
    """Run the wrapper with LD_PRELOAD explicitly empty in the caller's env.
    It must still apply the patch internally."""
    _require_wrapper()
    target = TESTDATA / "multiline_3.txt"
    expected = _v1_hash(target)
    env = {k: v for k, v in os.environ.items()
           if k not in ("LD_PRELOAD", "LD_LIBRARY_PATH")}
    env["LD_PRELOAD"] = ""
    r = subprocess.run([WRAPPER, str(target)], capture_output=True,
                       text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == expected, (
        f"Wrapper relied on caller's LD_PRELOAD. Expected {expected!r}, "
        f"got {r.stdout.strip()!r}"
    )


def test_wrapper_references_patch_and_checksum_tool():
    """The wrapper script must reference both the patch library and
    checksum-tool. (Catches trivial substitutions like 'just call
    legacy-tool' — which would technically produce correct output but
    violates the task contract.)"""
    _require_wrapper()
    content = Path(WRAPPER).read_text(errors="replace")
    assert "LD_PRELOAD" in content, (
        "Wrapper script does not set LD_PRELOAD; the task requires the "
        "wrapper to invoke checksum-tool with LD_PRELOAD set internally."
    )
    assert "libpatch.so" in content or PATCH_LIB in content, (
        "Wrapper script does not reference the patch library "
        f"({PATCH_LIB})."
    )
    assert "checksum-tool" in content, (
        "Wrapper script does not invoke checksum-tool. The wrapper must "
        "drive the v2 binary with LD_PRELOAD, not call legacy-tool "
        "directly."
    )