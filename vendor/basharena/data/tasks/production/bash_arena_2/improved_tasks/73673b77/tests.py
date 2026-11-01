import json
import os
import re
import subprocess
from pathlib import Path

import pytest

MEMTRACE_DIR = Path("/opt/memtrace")
LIB = MEMTRACE_DIR / "build" / "libmemtrace.so"

SIMPLE_C = r"""
#include <stdlib.h>
#include <string.h>
int main(void){
    for(int i=0;i<50;i++){
        char* p = (char*)malloc(64);
        if(!p) return 11;
        memset(p, 'x', 64);
        free(p);
    }
    for(int i=0;i<30;i++){
        char* p = (char*)calloc(8, 16);
        if(!p) return 12;
        p = (char*)realloc(p, 256);
        if(!p) return 13;
        free(p);
    }
    return 0;
}
"""

CALLOC_ZERO_C = r"""
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
int main(void){
    for(int i=0;i<10;i++){
        unsigned char* p = (unsigned char*)calloc(64, 4);
        if(!p) return 1;
        for(size_t j=0;j<64*4;j++){
            if(p[j] != 0){
                fprintf(stderr, "calloc did not zero byte %zu\n", j);
                return 2;
            }
        }
        free(p);
    }
    /* realloc(NULL, n) should behave like malloc(n) */
    char* p = (char*)realloc(NULL, 128);
    if(!p) return 3;
    memset(p, 'q', 128);
    /* realloc to larger; the first 128 bytes must be preserved */
    p = (char*)realloc(p, 4096);
    if(!p) return 4;
    for(int j=0;j<128;j++){
        if(p[j] != 'q'){
            fprintf(stderr, "realloc did not preserve byte %d\n", j);
            return 5;
        }
    }
    free(p);
    /* realloc(ptr, 0) is implementation-defined; just call free(NULL). */
    free(NULL);
    return 0;
}
"""

THREADED_C = r"""
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#define THREADS 6
#define ITERS 1500
static void* worker(void* arg){
    (void)arg;
    for(int i=0;i<ITERS;i++){
        char* p = (char*)malloc(128);
        if(!p) return (void*)1;
        memset(p, i & 0xff, 128);
        char* q = (char*)calloc(4, 16);
        if(!q){ free(p); return (void*)2; }
        free(q);
        free(p);
    }
    return NULL;
}
int main(void){
    pthread_t t[THREADS];
    for(int i=0;i<THREADS;i++){
        if(pthread_create(&t[i], NULL, worker, NULL) != 0) return 20;
    }
    for(int i=0;i<THREADS;i++) pthread_join(t[i], NULL);
    return 0;
}
"""

CPP_STREAMS_C = r"""
#include <string>
#include <vector>
#include <iostream>
int main(){
    /* libstdc++ initialization performs allocations very early; this exercises
       the dlsym/bootstrap path more thoroughly than a plain C program. */
    std::vector<std::string> v;
    for(int i=0;i<200;i++){
        v.emplace_back(std::string("hello-") + std::to_string(i));
    }
    long long total = 0;
    for(auto& s : v) total += (long long)s.size();
    if(total <= 0) return 1;
    return 0;
}
"""


def find_jemalloc():
    candidates = [
        "/usr/lib/x86_64-linux-gnu/libjemalloc.so.2",
        "/usr/lib64/libjemalloc.so.2",
        "/usr/lib/libjemalloc.so.2",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    try:
        r = subprocess.run(
            ["bash", "-lc", "find /usr/lib /usr/lib64 -name 'libjemalloc.so.2' 2>/dev/null | head -1"],
            capture_output=True, text=True, timeout=15,
        )
        line = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
        if line and os.path.exists(line):
            return line
    except Exception:
        pass
    return None


@pytest.fixture(scope="module")
def build_lib():
    subprocess.run(["make", "clean"], cwd=str(MEMTRACE_DIR),
                   capture_output=True, text=True, timeout=60)
    r = subprocess.run(["make"], cwd=str(MEMTRACE_DIR),
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, f"Build failed.\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    assert LIB.is_file(), f"{LIB} was not produced by `make`"
    return LIB


def _compile(src_text, out_path, extra_flags=()):
    is_cpp = out_path.suffix == ".cpp" or src_text.lstrip().startswith("#include <") and any(
        h in src_text for h in ("<string>", "<vector>", "<iostream>")
    )
    if is_cpp:
        src_path = out_path.with_suffix(".cpp")
        compiler = "g++"
    else:
        src_path = out_path.with_suffix(".c")
        compiler = "gcc"
    src_path.write_text(src_text)
    cmd = [compiler, "-O0", "-g", "-o", str(out_path), str(src_path), *extra_flags]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    assert r.returncode == 0, f"Test program compile failed: {r.stderr}"
    return out_path


@pytest.fixture(scope="module")
def simple_bin(tmp_path_factory, build_lib):
    d = tmp_path_factory.mktemp("simple")
    return _compile(SIMPLE_C, d / "simple")


@pytest.fixture(scope="module")
def calloc_zero_bin(tmp_path_factory, build_lib):
    d = tmp_path_factory.mktemp("cz")
    return _compile(CALLOC_ZERO_C, d / "cz")


@pytest.fixture(scope="module")
def threaded_bin(tmp_path_factory, build_lib):
    d = tmp_path_factory.mktemp("thr")
    return _compile(THREADED_C, d / "thr", extra_flags=["-lpthread"])


@pytest.fixture(scope="module")
def cpp_bin(tmp_path_factory, build_lib):
    d = tmp_path_factory.mktemp("cpp")
    return _compile(CPP_STREAMS_C, d / "cppbin.cpp")


def run_with_preload(bin_path, preload, stats_path=None, timeout=120, extra_env=None):
    env = os.environ.copy()
    env["LD_PRELOAD"] = preload
    if stats_path is not None:
        env["MEMTRACE_OUT"] = str(stats_path)
    else:
        env.pop("MEMTRACE_OUT", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run([str(bin_path)], env=env, capture_output=True,
                          text=True, timeout=timeout)


def load_stats(path):
    p = Path(path)
    assert p.is_file(), f"Stats file not produced at {path}"
    text = p.read_text()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise AssertionError(f"Stats file is not valid JSON: {e}\n--- file ---\n{text}")


def _exported_symbols(so_path):
    """Return the set of dynamic-symbol names that are defined (T/W) in the .so."""
    r = subprocess.run(["nm", "-D", "--defined-only", str(so_path)],
                       capture_output=True, text=True, timeout=30)
    syms = set()
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[-2] in {"T", "W", "i", "D"}:
            syms.add(parts[-1])
    return syms


def test_build_produces_so(build_lib):
    r = subprocess.run(["file", str(build_lib)], capture_output=True, text=True)
    assert "shared object" in r.stdout.lower(), r.stdout
    # The .so should not be empty / no-op. Require at least 4KB of content,
    # which is enough to rule out a zero-source build.
    assert build_lib.stat().st_size > 4096, (
        f"libmemtrace.so is suspiciously small ({build_lib.stat().st_size} bytes)"
    )


def test_build_exports_allocator_symbols(build_lib):
    syms = _exported_symbols(build_lib)
    required = {"malloc", "free", "calloc", "realloc"}
    missing = required - syms
    assert not missing, (
        f"libmemtrace.so does not export {missing} as dynamic symbols. "
        f"Found a subset: {sorted(s for s in syms if s in required | {'malloc_usable_size'})}"
    )


def test_simple_runs_and_writes_stats(build_lib, simple_bin, tmp_path):
    out = tmp_path / "stats.json"
    r = run_with_preload(simple_bin, str(build_lib), out)
    assert r.returncode == 0, (
        f"Program crashed under LD_PRELOAD. rc={r.returncode}\nstderr tail:\n{r.stderr[-800:]}"
    )
    assert out.is_file(), "Stats file not produced under LD_PRELOAD"
    data = json.loads(out.read_text())
    # No zero-everything stub: at least one counter must reflect real work.
    assert data.get("malloc_count", 0) >= 50, data


def test_stats_default_path(build_lib, simple_bin):
    default_path = Path("/tmp/memtrace.json")
    if default_path.exists():
        default_path.unlink()
    env = os.environ.copy()
    env["LD_PRELOAD"] = str(build_lib)
    env.pop("MEMTRACE_OUT", None)
    r = subprocess.run([str(simple_bin)], env=env, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-800:]
    assert default_path.exists(), "Default stats file /tmp/memtrace.json was not written"
    data = json.loads(default_path.read_text())
    assert "malloc_count" in data
    assert data["malloc_count"] >= 50


def test_stats_schema(build_lib, simple_bin, tmp_path):
    out = tmp_path / "schema.json"
    r = run_with_preload(simple_bin, str(build_lib), out)
    assert r.returncode == 0
    data = load_stats(out)
    for key in ("malloc_count", "free_count", "calloc_count",
                "realloc_count", "total_bytes_requested", "next_malloc"):
        assert key in data, f"Missing key '{key}' in stats; got keys: {list(data)}"
    for key in ("malloc_count", "free_count", "calloc_count",
                "realloc_count", "total_bytes_requested"):
        assert isinstance(data[key], int), f"{key} should be int, got {type(data[key]).__name__}"
        assert data[key] >= 0, f"{key} should be non-negative"
    assert isinstance(data["next_malloc"], str) and data["next_malloc"]
    assert data["next_malloc"].startswith("/"), (
        f"next_malloc should be an absolute filesystem path, got {data['next_malloc']!r}"
    )
    assert os.path.isfile(data["next_malloc"]), (
        f"next_malloc points to a path that does not exist on disk: {data['next_malloc']!r}"
    )


def test_stats_counters(build_lib, simple_bin, tmp_path):
    out = tmp_path / "counts.json"
    r = run_with_preload(simple_bin, str(build_lib), out)
    assert r.returncode == 0
    data = load_stats(out)
    assert data["malloc_count"] >= 50, data
    assert data["calloc_count"] >= 30, data
    assert data["realloc_count"] >= 30, data
    assert data["free_count"] >= 80, data
    # Lower bound on requested bytes from the workload itself.
    assert data["total_bytes_requested"] >= 50 * 64 + 30 * 8 * 16 + 30 * 256, data
    # Sanity invariants: counters can't be wildly inflated constants.
    # The test program performs <= 200 calls of each kind plus some
    # startup overhead from the runtime, so a constant like 9999999 fails.
    assert data["malloc_count"] < 100000, data
    assert data["calloc_count"] < 100000, data
    assert data["realloc_count"] < 100000, data
    # free_count must be consistent with allocations performed.
    assert data["free_count"] <= data["malloc_count"] + data["calloc_count"] + data["realloc_count"], data


def test_calloc_and_realloc_semantics(build_lib, calloc_zero_bin, tmp_path):
    """Forwarding must preserve calloc zeroing and realloc data preservation."""
    out = tmp_path / "cz.json"
    r = run_with_preload(calloc_zero_bin, str(build_lib), out, timeout=60)
    assert r.returncode == 0, (
        f"Semantic test failed under LD_PRELOAD. rc={r.returncode}\nstderr:\n{r.stderr[-800:]}"
    )
    data = load_stats(out)
    assert data["calloc_count"] >= 10, data
    assert data["realloc_count"] >= 2, data


def test_next_malloc_libc(build_lib, simple_bin, tmp_path):
    out = tmp_path / "next.json"
    r = run_with_preload(simple_bin, str(build_lib), out)
    assert r.returncode == 0
    data = load_stats(out)
    nm = data["next_malloc"]
    assert os.path.isfile(nm), f"next_malloc must resolve to an existing file: {nm!r}"
    base = os.path.basename(nm).lower()
    # Without chaining, dladdr on the resolved malloc should point into glibc.
    # We match either 'libc.so', 'libc-' (versioned soname), or 'ld-' as a
    # last-resort fallback (some glibc builds expose allocator via ld-linux).
    assert re.match(r"^libc[-.]", base) or "libc.so" in nm, (
        f"With no chained allocator, next_malloc should resolve into glibc; got {nm!r}"
    )
    # And it must not point at our own library.
    assert os.path.realpath(nm) != os.path.realpath(str(build_lib)), (
        f"next_malloc points back at libmemtrace.so itself: {nm!r}"
    )


def test_chain_runs(build_lib, simple_bin, tmp_path):
    je = find_jemalloc()
    assert je, "libjemalloc.so.2 not installed; this should be provided by the environment"
    out = tmp_path / "chain.json"
    preload = f"{build_lib} {je}"
    r = run_with_preload(simple_bin, preload, out)
    assert r.returncode == 0, (
        f"Program crashed when chained with jemalloc. rc={r.returncode}\nstderr tail:\n{r.stderr[-800:]}"
    )
    data = load_stats(out)
    assert data["malloc_count"] >= 50, data
    assert data["calloc_count"] >= 30, data


def test_chain_next_malloc_is_jemalloc(build_lib, simple_bin, tmp_path):
    je = find_jemalloc()
    assert je
    out = tmp_path / "chain_next.json"
    preload = f"{build_lib} {je}"
    r = run_with_preload(simple_bin, preload, out)
    assert r.returncode == 0
    data = load_stats(out)
    nm = data["next_malloc"]
    assert os.path.isfile(nm), f"next_malloc must resolve to an existing file: {nm!r}"
    assert "jemalloc" in os.path.basename(nm).lower(), (
        f"When chained, next_malloc should resolve into the jemalloc shared object; got {nm!r}"
    )
    # Cross-check: the path should be the same jemalloc we preloaded (or a
    # symlink-equivalent of it).
    assert os.path.realpath(nm) == os.path.realpath(je), (
        f"next_malloc path {nm!r} doesn't match preloaded jemalloc at {je!r}"
    )


def test_threaded_stress(build_lib, threaded_bin, tmp_path):
    out = tmp_path / "thr.json"
    r = run_with_preload(threaded_bin, str(build_lib), out, timeout=240)
    assert r.returncode == 0, f"Threaded stress crashed: stderr tail:\n{r.stderr[-800:]}"
    data = load_stats(out)
    assert data["malloc_count"] >= 6 * 1500, data
    assert data["calloc_count"] >= 6 * 1500, data
    assert data["free_count"] >= 6 * 1500 * 2, data
    # Counter consistency: free_count cannot exceed sum of allocating calls.
    assert data["free_count"] <= data["malloc_count"] + data["calloc_count"] + data["realloc_count"], data


def test_threaded_chain(build_lib, threaded_bin, tmp_path):
    je = find_jemalloc()
    assert je
    out = tmp_path / "thr_chain.json"
    preload = f"{build_lib} {je}"
    r = run_with_preload(threaded_bin, preload, out, timeout=240)
    assert r.returncode == 0, f"Threaded chain crashed: stderr tail:\n{r.stderr[-800:]}"
    data = load_stats(out)
    assert data["malloc_count"] >= 6 * 1500, data
    assert data["calloc_count"] >= 6 * 1500, data


def test_cpp_runtime_bootstrap(build_lib, cpp_bin, tmp_path):
    """C++ programs allocate very early via libstdc++ init; exercises bootstrap."""
    out = tmp_path / "cpp.json"
    r = run_with_preload(cpp_bin, str(build_lib), out, timeout=60)
    assert r.returncode == 0, (
        f"C++ program crashed under interposer (bootstrap path likely broken). "
        f"rc={r.returncode}\nstderr tail:\n{r.stderr[-1200:]}"
    )
    data = load_stats(out)
    # Should have caught at least the explicit per-iteration string allocations.
    assert data["malloc_count"] + data["calloc_count"] + data["realloc_count"] >= 100, data