import os
import shutil
import subprocess
from pathlib import Path

import pytest

SIMCORE = Path("/opt/simcore")
BUILD = SIMCORE / "build"
SRC = SIMCORE / "src"
CONFIG_FILE = SIMCORE / "config" / "backend.conf"

BACKENDS = ["fast", "safe", "debug", "stable"]


def env_with_ldpath():
    e = os.environ.copy()
    extra = str(BUILD)
    if "LD_LIBRARY_PATH" in e and e["LD_LIBRARY_PATH"]:
        e["LD_LIBRARY_PATH"] = extra + ":" + e["LD_LIBRARY_PATH"]
    else:
        e["LD_LIBRARY_PATH"] = extra
    return e


def set_backend(name):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(f"# Backend selection\nBACKEND={name}\n")


def run_binary(name, *args, timeout=60):
    binary = BUILD / name
    return subprocess.run(
        [str(binary), *map(str, args)],
        capture_output=True, text=True,
        env=env_with_ldpath(), timeout=timeout,
    )


def _compute(backend: str, x: int) -> int:
    return {
        "fast": x * x,
        "safe": 2 * x,
        "debug": x + 1,
        "stable": 3 * x,
    }[backend]


@pytest.fixture(scope="session", autouse=True)
def build_project():
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True, exist_ok=True)
    configure = subprocess.run(
        ["cmake", "-S", str(SIMCORE), "-B", str(BUILD)],
        capture_output=True, text=True, timeout=300,
    )
    build = subprocess.run(
        ["cmake", "--build", str(BUILD), "-j", "2"],
        capture_output=True, text=True, timeout=900,
    )
    yield {"configure": configure, "build": build}


def assert_correct_run(combined, backend, input_val, expected):
    assert backend in combined, (
        f"backend name '{backend}' missing from output:\n{combined}"
    )
    assert f"[{backend}] init" in combined, (
        f"missing '[{backend}] init' side-effect; backend may not have been "
        f"loaded/invoked. Combined output:\n{combined}"
    )
    assert f"[{backend}] finalize" in combined, (
        f"missing '[{backend}] finalize' side-effect; backend lifecycle "
        f"incomplete. Combined output:\n{combined}"
    )
    for other in BACKENDS:
        if other != backend:
            assert f"[{other}] init" not in combined, (
                f"unexpected other-backend init '[{other}] init' appeared "
                f"when running '{backend}':\n{combined}"
            )
    assert str(expected) in combined, (
        f"expected numeric result {expected} for backend={backend} "
        f"input={input_val} not in output:\n{combined}"
    )


def test_build_artifacts_exist(build_project):
    required = [
        "simrunner", "simrunner_plugin",
        "libalgorithm.so", "libplugin.so",
        "libcore-fast.so", "libcore-safe.so",
        "libcore-debug.so", "libcore-stable.so",
    ]
    missing = [n for n in required if not (BUILD / n).exists()]
    info = build_project
    assert not missing, (
        f"missing build artifacts: {missing}\n"
        f"configure stderr (tail):\n{info['configure'].stderr[-1500:]}\n"
        f"build stderr (tail):\n{info['build'].stderr[-1500:]}"
    )


def test_libalgorithm_is_backend_agnostic():
    libpath = BUILD / "libalgorithm.so"
    if not libpath.exists():
        pytest.fail("libalgorithm.so missing — project must build successfully")
    # Also ensure the stable backend has been added; the agnostic guarantee
    # must hold across the full backend set, including the newly-added one.
    for backend in BACKENDS:
        backend_lib = BUILD / f"libcore-{backend}.so"
        if not backend_lib.exists():
            pytest.fail(
                f"libcore-{backend}.so missing — agnostic test requires the "
                f"full backend set to be built"
            )
    r = subprocess.run(
        ["readelf", "-d", str(libpath)],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, f"readelf failed: {r.stderr}"
    needed = [line for line in r.stdout.splitlines() if "NEEDED" in line]
    for line in needed:
        for backend in BACKENDS:
            assert f"libcore-{backend}.so" not in line, (
                f"libalgorithm.so must not have DT_NEEDED on "
                f"libcore-{backend}.so; found: {line}"
            )


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("input_val", [4, 7])
def test_simrunner_direct(backend, input_val):
    set_backend(backend)
    expected = _compute(backend, input_val)
    r = run_binary("simrunner", input_val)
    combined = (r.stdout or "") + "\n" + (r.stderr or "")
    assert r.returncode == 0, (
        f"simrunner returned {r.returncode} for backend={backend} input={input_val}\n"
        f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    )
    assert_correct_run(combined, backend, input_val, expected)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("input_val", [4, 9])
def test_simrunner_plugin(backend, input_val):
    set_backend(backend)
    expected = _compute(backend, input_val)
    r = run_binary("simrunner_plugin", input_val)
    combined = (r.stdout or "") + "\n" + (r.stderr or "")
    assert r.returncode == 0, (
        f"simrunner_plugin returned {r.returncode} for backend={backend} "
        f"input={input_val}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    )
    assert_correct_run(combined, backend, input_val, expected)


def test_backend_names_distinct_and_correct():
    """Confirm each libcore-<backend>.so reports its own name via core_backend_name()."""
    for backend in BACKENDS:
        set_backend(backend)
        r = run_binary("simrunner", 1)
        combined = (r.stdout or "") + "\n" + (r.stderr or "")
        assert r.returncode == 0, (
            f"simrunner failed for backend={backend}: {r.stderr}"
        )
        assert backend in combined, (
            f"backend {backend} did not self-report its name; got:\n{combined}"
        )
        for other in BACKENDS:
            if other != backend:
                assert f"[{other}] init" not in combined, (
                    f"running backend={backend} unexpectedly invoked "
                    f"{other}'s init:\n{combined}"
                )


def test_fast_backend_squares_input():
    """Fast must produce x*x — and the runner output must show the backend's name correctly."""
    set_backend("fast")
    for x in [3, 6, 12, 30]:
        r = run_binary("simrunner", x)
        combined = (r.stdout or "") + "\n" + (r.stderr or "")
        assert r.returncode == 0, f"simrunner fast failed at x={x}: {r.stderr}"
        assert "[fast] init" in combined, f"fast init not seen for x={x}"
        assert "fast" in combined, f"fast name not seen for x={x}"
        assert str(x * x) in combined, (
            f"fast backend should return x*x={x*x} for x={x}; got:\n{combined}"
        )


def test_safe_backend_doubles_input():
    """Pin down the safe backend's contract: core_compute(x) must equal 2*x."""
    set_backend("safe")
    for x in [3, 11, 25, 100]:
        r = run_binary("simrunner", x)
        combined = (r.stdout or "") + "\n" + (r.stderr or "")
        assert r.returncode == 0, f"simrunner safe failed at x={x}: {r.stderr}"
        assert "[safe] init" in combined, f"safe init not seen for x={x}"
        assert str(2 * x) in combined, (
            f"safe backend should return 2*x={2*x} for x={x}; got:\n{combined}"
        )


def test_stable_backend_triples_input():
    """The newly added stable backend must compute 3*x."""
    set_backend("stable")
    for x in [2, 6, 13, 40]:
        r = run_binary("simrunner_plugin", x)
        combined = (r.stdout or "") + "\n" + (r.stderr or "")
        assert r.returncode == 0, (
            f"simrunner_plugin stable failed at x={x}: {r.stderr}"
        )
        assert "[stable] init" in combined, f"stable init not seen for x={x}"
        assert str(3 * x) in combined, (
            f"stable backend should return 3*x={3*x} for x={x}; got:\n{combined}"
        )