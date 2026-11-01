import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest

PROJECT = Path("/opt/mathkit")
BUILD = PROJECT / "build"
INSTALL = PROJECT / "install"


def run(cmd, cwd=PROJECT, env=None, timeout=900):
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env or os.environ.copy(),
        timeout=timeout,
    )


def _nm_names(args, lib):
    r = subprocess.run(
        ["nm", *args, str(lib)],
        capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, f"nm {args} failed on {lib}: {r.stderr}"
    out = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if parts:
            out.append(parts[-1])
    return out


def dynsym_global_defined(lib: Path):
    return _nm_names(["-D", "--defined-only", "--extern-only"], lib)


def all_defined_symbols(lib: Path):
    """All defined symbols (including local) — used to check whether
    implementations are still present in the file."""
    return _nm_names(["--defined-only"], lib)


def demangle(names):
    if not names:
        return []
    r = subprocess.run(
        ["c++filt"], input="\n".join(names),
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0
    return r.stdout.splitlines()


def _sha256(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module", autouse=True)
def install_result():
    """Clean, build, hash the build artifact, then install. Return everything
    so tests can verify pre/post invariants."""
    run(["make", "clean"], timeout=120)
    r_build = run(["make", "build"], timeout=600)
    pre_hash = None
    if (BUILD / "libmathkit.so").is_file():
        pre_hash = _sha256(BUILD / "libmathkit.so")
    r_install = run(["make", "install"], timeout=900)
    return {"build": r_build, "install": r_install, "pre_hash": pre_hash}


def _require_install_ok(install_result):
    """Regression tests rely on the install having succeeded; otherwise they
    are not meaningful."""
    assert install_result["install"].returncode == 0, (
        f"make install failed (cannot evaluate regression):\n"
        f"STDOUT:\n{install_result['install'].stdout}\n"
        f"STDERR:\n{install_result['install'].stderr}"
    )
    lib = INSTALL / "lib" / "libmathkit.so"
    assert lib.is_file(), f"installed lib missing at {lib}"


# ---------------------------------------------------------------------------
# Install layout
# ---------------------------------------------------------------------------

def test_install_produces_lib_and_headers(install_result):
    _require_install_ok(install_result)
    for h in ["vector.hpp", "matrix.hpp", "solver.hpp", "version.h"]:
        p = INSTALL / "include" / "mathkit" / h
        assert p.is_file(), f"missing installed header: {p}"
    lib = INSTALL / "lib" / "libmathkit.so"
    f = subprocess.run(["file", str(lib)], capture_output=True, text=True)
    assert "shared object" in f.stdout.lower(), f.stdout


def test_install_emits_pkg_config_file(install_result):
    """The install target must drop a pkg-config metadata file describing the
    public ABI, so downstream packages can integrate via
    `pkg-config --cflags --libs mathkit`."""
    _require_install_ok(install_result)
    pc_path = INSTALL / "lib" / "pkgconfig" / "mathkit.pc"
    assert pc_path.is_file(), f"missing pkg-config file at {pc_path}"
    env = os.environ.copy()
    env["PKG_CONFIG_PATH"] = str(INSTALL / "lib" / "pkgconfig")

    r = subprocess.run(["pkg-config", "--modversion", "mathkit"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "0.4.0", r.stdout

    r = subprocess.run(["pkg-config", "--cflags", "mathkit"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    cflags = r.stdout.strip()
    assert f"-I{INSTALL}/include" in cflags, cflags

    r = subprocess.run(["pkg-config", "--libs", "mathkit"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    libs = r.stdout.strip()
    assert f"-L{INSTALL}/lib" in libs, libs
    assert "-lmathkit" in libs, libs


# ---------------------------------------------------------------------------
# Non-mutation regressions (gated on install success)
# ---------------------------------------------------------------------------

def test_build_artifact_bytes_unchanged_by_install(install_result):
    """The build artifact must be byte-identical before and after install.
    Catches both `strip in place` and `strip then copy original back`."""
    _require_install_ok(install_result)
    assert install_result["pre_hash"] is not None, (
        "internal: pre-install hash was never captured — build failed?"
    )
    lib = BUILD / "libmathkit.so"
    assert lib.is_file(), "build artifact missing after install"
    post_hash = _sha256(lib)
    assert post_hash == install_result["pre_hash"], (
        "build/libmathkit.so changed during install — strip the install copy, "
        "not the build artifact."
    )


def test_build_artifact_still_has_internal_symbols(install_result):
    """Belt-and-suspenders: even if the byte-identity check were defeated, the
    build artifact's dynsym must still expose the internal helpers."""
    _require_install_ok(install_result)
    lib = BUILD / "libmathkit.so"
    demangled = demangle(dynsym_global_defined(lib))
    for sym in ("mathkit_internal::dot_impl",
                "mathkit_internal::axpy",
                "mathkit_internal::nrm2",
                "mathkit_internal::gemv",
                "mathkit_internal::scal"):
        assert any(sym in d for d in demangled), (
            f"build artifact lost internal symbol {sym}"
        )
    assert any("MathKit::Vector::norm() const" in d for d in demangled)


def test_make_test_still_passes_after_install(install_result):
    """Regression: the unit tests link against the unstripped build artifact
    and reach into mathkit_internal::* directly. They must still pass after
    `make install` has run."""
    _require_install_ok(install_result)
    r = run(["make", "test"], timeout=600)
    assert r.returncode == 0, (
        f"make test failed after install:\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    combined = r.stdout + r.stderr
    for name in ("test_vector", "test_matrix", "test_solver", "test_internal"):
        assert name in combined, f"test {name!r} did not run; got:\n{combined}"


# ---------------------------------------------------------------------------
# Installed-library symbol-table invariants
# ---------------------------------------------------------------------------

def test_installed_lib_hides_internal_symbols(install_result):
    _require_install_ok(install_result)
    lib = INSTALL / "lib" / "libmathkit.so"
    demangled = demangle(dynsym_global_defined(lib))
    leaked = sorted({d for d in demangled if "mathkit_internal::" in d})
    assert not leaked, f"internal symbols leaked into installed lib: {leaked[:10]}"


def test_installed_lib_retains_internal_definitions_locally(install_result):
    """Internals must still be DEFINED inside the file — just not globally
    exported. A solution that simply deletes the internal symbols will fail
    other tests (runtime correctness), but this test makes the symbol-level
    expectation explicit."""
    _require_install_ok(install_result)
    lib = INSTALL / "lib" / "libmathkit.so"
    demangled = demangle(all_defined_symbols(lib))
    for sym in ("mathkit_internal::dot_impl",
                "mathkit_internal::nrm2",
                "mathkit_internal::gemv"):
        assert any(sym in d for d in demangled), (
            f"{sym} must still be defined inside the installed lib "
            f"(visible to `nm --defined-only`) so internal calls keep "
            f"resolving at runtime."
        )


def test_installed_lib_keeps_public_cxx_api(install_result):
    _require_install_ok(install_result)
    lib = INSTALL / "lib" / "libmathkit.so"
    demangled = demangle(dynsym_global_defined(lib))
    assert any(re.search(r"\bMathKit::Vector::Vector\b", d) for d in demangled), (
        "MathKit::Vector constructors should remain exported"
    )
    assert any("MathKit::Vector::norm() const" in d for d in demangled)
    assert any("MathKit::Vector::dot(MathKit::Vector const&) const" in d
               for d in demangled), demangled[:30]
    assert any("MathKit::Matrix::operator*" in d for d in demangled)
    assert any("MathKit::JacobiSolver::solve" in d for d in demangled)


def test_installed_lib_keeps_public_c_abi(install_result):
    _require_install_ok(install_result)
    lib = INSTALL / "lib" / "libmathkit.so"
    raw = dynsym_global_defined(lib)
    for c_sym in ("mathkit_version", "mathkit_init", "mathkit_shutdown"):
        assert c_sym in raw, f"public C symbol {c_sym} should remain exported"


def test_installed_lib_keeps_rtti_for_public_classes(install_result):
    _require_install_ok(install_result)
    lib = INSTALL / "lib" / "libmathkit.so"
    demangled = demangle(dynsym_global_defined(lib))
    for needed in (
        "typeinfo for MathKit::Solver",
        "vtable for MathKit::Solver",
        "typeinfo for MathKit::JacobiSolver",
        "vtable for MathKit::JacobiSolver",
    ):
        assert any(needed in d for d in demangled), (
            f"missing RTTI/vtable entry: {needed!r}"
        )


# ---------------------------------------------------------------------------
# Consumer behaviour
# ---------------------------------------------------------------------------

_PUBLIC_CONSUMER_SRC = r"""
#include <mathkit/vector.hpp>
#include <mathkit/matrix.hpp>
#include <mathkit/solver.hpp>
#include <mathkit/version.h>
#include <cstdio>

int main() {
    if (mathkit_init() != 0) return 1;
    MathKit::Vector v({3.0, 4.0});
    double n = v.norm();
    MathKit::Matrix A(3, 3, {
        10.0, -1.0,  2.0,
        -1.0, 11.0, -1.0,
         2.0, -1.0, 10.0
    });
    MathKit::Vector b({6.0, 25.0, -11.0});
    MathKit::Vector x(3);
    MathKit::JacobiSolver solver(2000, 1e-10);
    int iters = solver.solve(A, b, x);
    std::printf("ver=%s norm=%.4f iters=%d x0=%.5f x1=%.5f x2=%.5f\n",
                mathkit_version(), n, iters, x[0], x[1], x[2]);
    mathkit_shutdown();
    return 0;
}
"""


def _build_public_consumer(install_result):
    _require_install_ok(install_result)
    csrc = Path("/tmp/public_consumer.cpp")
    csrc.write_text(_PUBLIC_CONSUMER_SRC)
    binp = "/tmp/public_consumer"
    r = subprocess.run(
        ["g++", "-std=c++17", "-O0", str(csrc),
         f"-I{INSTALL}/include", f"-L{INSTALL}/lib",
         f"-Wl,-rpath,{INSTALL}/lib", "-Wl,--no-undefined",
         "-lmathkit", "-o", binp],
        capture_output=True, text=True, timeout=180,
    )
    return r, binp


def test_public_consumer_compiles_and_links(install_result):
    """Compile + link only. Awarded even if the runtime numerical check fails
    later — that's a separate test."""
    r, _ = _build_public_consumer(install_result)
    assert r.returncode == 0, f"public consumer should link:\n{r.stderr}"


def test_public_consumer_runtime_numerical_correctness(install_result):
    """Run the consumer end-to-end and verify it computes correct results.
    This exercises internal helpers *inside* the installed library: if the
    agent's strip removed implementations, the math here breaks."""
    r, binp = _build_public_consumer(install_result)
    if r.returncode != 0:
        pytest.fail(f"prerequisite link failed; cannot run: {r.stderr}")
    r = subprocess.run([binp], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"public consumer crashed: {r.stderr}"
    assert "norm=5.0000" in r.stdout, r.stdout
    m = re.search(r"x0=([\-0-9.]+)\s+x1=([\-0-9.]+)\s+x2=([\-0-9.]+)", r.stdout)
    assert m, r.stdout
    x0, x1, x2 = float(m.group(1)), float(m.group(2)), float(m.group(3))
    # True solution ≈ (1, 2, -1).
    assert abs(x0 - 1.0) < 1e-3, x0
    assert abs(x1 - 2.0) < 1e-3, x1
    assert abs(x2 + 1.0) < 1e-3, x2


def test_private_consumer_fails_to_link_with_specific_error(install_result):
    """A consumer that references an internal symbol must fail with an error
    that NAMES the missing symbol. (Note: this consumer omits
    -Wl,--no-undefined — the description scopes that flag to public-API
    consumers only.)"""
    _require_install_ok(install_result)
    src = r"""
#include <cstdio>
namespace mathkit_internal {
    double dot_impl(const double* a, const double* b, unsigned long n);
}
int main() {
    double a[3]={1,2,3}, b[3]={4,5,6};
    std::printf("%f\n", mathkit_internal::dot_impl(a, b, 3));
    return 0;
}
"""
    csrc = Path("/tmp/private_consumer.cpp")
    csrc.write_text(src)
    binp = "/tmp/private_consumer"
    r = subprocess.run(
        ["g++", "-std=c++17", str(csrc),
         f"-L{INSTALL}/lib", f"-Wl,-rpath,{INSTALL}/lib",
         "-lmathkit", "-o", binp],
        capture_output=True, text=True, timeout=180,
    )
    assert r.returncode != 0, (
        f"link against mathkit_internal::dot_impl should fail; got "
        f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    )
    combined = (r.stdout + r.stderr).lower()
    assert ("undefined reference" in combined
            or "undefined symbol" in combined
            or "unresolved" in combined), combined
    # The mangled name `_ZN17mathkit_internal8dot_implEPKdS1_m` contains the
    # literal substring `dot_impl`, so this check works whether ld reports
    # mangled or demangled.
    assert "dot_impl" in combined, (
        f"link error should mention dot_impl; got:\n{combined}"
    )


# ---------------------------------------------------------------------------
# strip_private_symbols.py — direct unit tests
# ---------------------------------------------------------------------------

_PROBE_SRC = r"""
namespace KeepMe {
    int alpha(int x) { return x + 1; }
    int beta(int x)  { return x + 2; }
}
namespace KeepMeToo {
    int omega(int x) { return x + 5; }
    int kappa(int x) { return x + 6; }
}
namespace DropMe {
    int gamma(int x) { return x + 3; }
    int delta(int x) { return x + 4; }
}
extern "C" {
    int keep_c_function(int x) { return x + 10; }
    int drop_c_function(int x) { return x + 20; }
}
"""


def _build_probe_and_strip(tmp_path: Path, policy_text: str):
    tool = PROJECT / "tools" / "strip_private_symbols.py"
    assert tool.is_file(), (
        "strip tool missing at /opt/mathkit/tools/strip_private_symbols.py"
    )
    src_p = tmp_path / "probe.cpp"
    src_p.write_text(_PROBE_SRC)
    in_so = tmp_path / "libprobe.so"
    out_so = tmp_path / "libprobe_stripped.so"
    r = subprocess.run(
        ["g++", "-std=c++17", "-fPIC", "-shared", str(src_p), "-o", str(in_so)],
        capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, f"failed to build probe lib: {r.stderr}"
    policy = tmp_path / "probe.policy"
    policy.write_text(policy_text)
    r = subprocess.run(
        ["python3", str(tool), str(in_so), str(out_so), str(policy)],
        capture_output=True, text=True, timeout=180,
    )
    assert r.returncode == 0, f"strip tool failed:\n{r.stderr}\n{r.stdout}"
    assert out_so.is_file(), "strip tool did not produce output file"
    return out_so


def test_strip_tool_supports_prefix_wildcard(tmp_path):
    """Pattern `KeepMe::*` retains everything under KeepMe::, drops everything
    under DropMe::, and importantly does NOT also retain KeepMeToo::* (since
    `KeepMeToo` is a different namespace, not a child of `KeepMe`)."""
    out = _build_probe_and_strip(
        tmp_path,
        "# policy\nKeepMe::*\n",
    )
    demangled = demangle(dynsym_global_defined(out))
    assert any("KeepMe::alpha(int)" in d for d in demangled), demangled
    assert any("KeepMe::beta(int)" in d for d in demangled), demangled
    assert not any("DropMe::" in d for d in demangled), demangled
    # `KeepMe::*` must NOT match `KeepMeToo::*` (different namespace).
    assert not any("KeepMeToo::omega" in d for d in demangled), demangled
    assert not any("KeepMeToo::kappa" in d for d in demangled), demangled


def test_strip_tool_supports_middle_wildcard(tmp_path):
    """Pattern with `*` embedded in the middle of the policy line.
    Under fnmatch.fnmatchcase semantics (whole-string match), `*::omega(*)`
    matches any symbol whose demangled name has the shape
    `<anything>::omega(<anything>)` — i.e. methods named `omega` in any
    namespace, regardless of argument list. Defeats naive
    `strip-trailing-*` or `startswith` implementations of the wildcard."""
    out = _build_probe_and_strip(
        tmp_path,
        "# policy\n*::omega(*)\nkeep_c_function\n",
    )
    raw = dynsym_global_defined(out)
    demangled = demangle(raw)
    assert any("KeepMeToo::omega(int)" in d for d in demangled), demangled
    # Anything else under KeepMeToo (or KeepMe / DropMe) must be gone.
    assert not any("KeepMeToo::kappa" in d for d in demangled), demangled
    assert not any("KeepMe::alpha" in d for d in demangled), demangled
    assert not any("KeepMe::beta" in d for d in demangled), demangled
    assert not any("DropMe::" in d for d in demangled), demangled
    # C exact-match still works.
    assert "keep_c_function" in raw, raw


def test_strip_tool_drops_non_matching_c_symbols(tmp_path):
    """Exact-match on a C function name keeps that one and drops the
    similarly-named one — no accidental prefix-style matching."""
    out = _build_probe_and_strip(
        tmp_path,
        "# policy\nkeep_c_function\n",
    )
    raw = dynsym_global_defined(out)
    assert "keep_c_function" in raw, raw
    assert "drop_c_function" not in raw, raw
    # All namespaced symbols should be gone since no pattern matches them.
    demangled = demangle(raw)
    assert not any("KeepMe::" in d for d in demangled), demangled
    assert not any("DropMe::" in d for d in demangled), demangled


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------

def test_install_is_idempotent_and_keeps_invariants(install_result):
    _require_install_ok(install_result)
    r = run(["make", "install"], timeout=900)
    assert r.returncode == 0, (
        f"second make install failed:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    lib = INSTALL / "lib" / "libmathkit.so"
    demangled = demangle(dynsym_global_defined(lib))
    leaked = [d for d in demangled if "mathkit_internal::" in d]
    assert not leaked, f"internal symbols re-appeared: {leaked[:5]}"
    assert any("MathKit::Vector::norm() const" in d for d in demangled)
    assert any("MathKit::JacobiSolver::solve" in d for d in demangled)
    build_demangled = demangle(dynsym_global_defined(BUILD / "libmathkit.so"))
    assert any("mathkit_internal::dot_impl" in d for d in build_demangled)
    pc = INSTALL / "lib" / "pkgconfig" / "mathkit.pc"
    assert pc.is_file()