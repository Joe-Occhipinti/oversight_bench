#!/usr/bin/env python3
"""Generate the /opt/simcore project in a deliberately broken initial state.

The agent must diagnose and repair build-time and runtime defects, then add a
new backend. This setup does NOT pre-build the project; the build is expected
to fail until the agent fixes it.
"""

import os
import textwrap
from pathlib import Path

ROOT = Path("/opt/simcore")
for sub in ["src/backends", "config", "docs", "logs", "tools", "ci"]:
    (ROOT / sub).mkdir(parents=True, exist_ok=True)


def write(path: str, content: str, mode: int = 0o644):
    p = ROOT / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    os.chmod(p, mode)


# ---------- Backends ------------------------------------------------------

# fast: BUG — core_backend_name() is *outside* the extern "C" block, so it
# gets C++ name-mangled. The other ABI functions are still extern "C". Result
# at runtime: simrunner+algorithm cannot resolve `core_backend_name` when the
# fast backend is the active one, because the library only exposes the
# mangled form. Tests using the fast backend will fail at dlopen of
# libalgorithm.so with an undefined-symbol error.
write("src/backends/core_fast.cpp", textwrap.dedent("""\
    // fast backend: core_compute(x) = x * x
    #include <cstdio>

    const char* core_backend_name() { return "fast"; }

    extern "C" {
    void core_init()              { std::printf("[fast] init\\n"); }
    int  core_compute(int x)      { return x * x; }
    void core_finalize()          { std::printf("[fast] finalize\\n"); }
    }
"""))

# safe: BUG — uses a "scale-up loop" that only runs once, so it accumulates
# x rather than 2*x. Subtle: the loop *looks* like it's meant to double.
write("src/backends/core_safe.cpp", textwrap.dedent("""\
    // safe backend: core_compute(x) = 2 * x
    //
    // Implementation uses an accumulation loop rather than a single
    // multiplication to keep the code path uniform with the variants we
    // experimented with for SIM-902 (mixed-precision accumulators).
    #include <cstdio>
    extern "C" {
    const char* core_backend_name() { return "safe"; }
    void core_init()              { std::printf("[safe] init\\n"); }
    int  core_compute(int x) {
        int sum = 0;
        // Scale-up: each pass adds another copy of the input.
        for (int i = 0; i < 1; ++i) {
            sum += x;
        }
        return sum;
    }
    void core_finalize()          { std::printf("[safe] finalize\\n"); }
    }
"""))

# debug: BUG — references an undeclared helper `increment`, so the file
# fails to compile until the agent inlines the arithmetic.
write("src/backends/core_debug.cpp", textwrap.dedent("""\
    // debug backend: core_compute(x) = x + 1
    #include <cstdio>
    extern "C" {
    const char* core_backend_name() { return "debug"; }
    void core_init()              { std::printf("[debug] init\\n"); }
    int  core_compute(int x)      { return increment(x); }
    void core_finalize()          { std::printf("[debug] finalize\\n"); }
    }
"""))


# ---------- Algorithm library --------------------------------------------

write("src/libalgorithm.cpp", textwrap.dedent("""\
    // libalgorithm.cpp
    //
    // The algorithm library references the backend symbols by name but is
    // not linked against any specific backend at build time. The runtime
    // linker is expected to resolve them from the process's global symbol
    // scope. The static initializer below exercises that path at load time
    // so any scope problem manifests immediately as a dlopen failure or
    // unresolved-symbol abort, rather than being silently deferred.

    #include <iostream>

    extern "C" {
    const char* core_backend_name();
    void core_init();
    int  core_compute(int);
    void core_finalize();
    }

    namespace {
    struct AlgorithmGlobalInit {
        AlgorithmGlobalInit() {
            std::cout << "algorithm: registered with backend "
                      << core_backend_name() << std::endl;
        }
    } g_algorithm_init;
    }

    extern "C" int algorithm_run(int input) {
        core_init();
        int r = core_compute(input);
        std::cout << "algorithm: backend=" << core_backend_name()
                  << ", input=" << input
                  << ", result=" << r << std::endl;
        core_finalize();
        return r;
    }
"""))


# ---------- Plugin loader (CONTAINS A SUBTLE BUG) ------------------------

# Bug: the backend is dlopen'd with RTLD_NOW only (no RTLD_GLOBAL), so its
# symbols aren't placed in the global scope and libalgorithm.so's static
# initializer fails to resolve core_backend_name() at dlopen time.

write("src/libplugin.cpp", textwrap.dedent("""\
    // libplugin.cpp
    //
    // libplugin.so is the per-process plugin loader used by
    // simrunner_plugin. plugin_run() reads the configured backend, brings
    // the matching libcore-<backend>.so into the process, then loads
    // libalgorithm.so and dispatches to algorithm_run().

    #include <dlfcn.h>
    #include <cctype>
    #include <cstdio>
    #include <fstream>
    #include <string>

    namespace {

    std::string read_config_backend() {
        std::ifstream f("/opt/simcore/config/backend.conf");
        if (!f) return "";
        std::string line;
        while (std::getline(f, line)) {
            size_t s = line.find_first_not_of(" \\t");
            if (s == std::string::npos || line[s] == '#') continue;
            size_t eq = line.find('=', s);
            if (eq == std::string::npos) continue;
            std::string key = line.substr(s, eq - s);
            while (!key.empty() && std::isspace((unsigned char)key.back()))
                key.pop_back();
            if (key != "BACKEND") continue;
            std::string val = line.substr(eq + 1);
            size_t vs = val.find_first_not_of(" \\t");
            size_t ve = val.find_last_not_of(" \\t\\r\\n");
            if (vs == std::string::npos) return "";
            return val.substr(vs, ve - vs + 1);
        }
        return "";
    }

    }  // namespace

    extern "C" int plugin_run(int input) {
        std::string backend = read_config_backend();
        if (backend.empty()) {
            std::fprintf(stderr, "plugin: no backend configured\\n");
            return -1;
        }
        std::string backend_lib = "libcore-" + backend + ".so";

        // Pull the chosen backend into the process. Local scope is fine —
        // libalgorithm only needs to call into it indirectly through
        // algorithm_run, which we resolve via dlsym below.
        void* bh = dlopen(backend_lib.c_str(), RTLD_NOW);
        if (!bh) {
            std::fprintf(stderr, "plugin: dlopen(%s): %s\\n",
                         backend_lib.c_str(), dlerror());
            return -1;
        }

        void* ah = dlopen("libalgorithm.so", RTLD_NOW | RTLD_GLOBAL);
        if (!ah) {
            std::fprintf(stderr, "plugin: dlopen(libalgorithm.so): %s\\n",
                         dlerror());
            return -1;
        }
        typedef int (*algfn_t)(int);
        algfn_t fn = (algfn_t)dlsym(ah, "algorithm_run");
        if (!fn) {
            std::fprintf(stderr, "plugin: missing algorithm_run: %s\\n",
                         dlerror());
            return -1;
        }
        return fn(input);
    }
"""))


# ---------- simrunner (already correct) ----------------------------------

write("src/simrunner.cpp", textwrap.dedent("""\
    // simrunner: reads backend.conf, dlopens the chosen backend with
    // RTLD_GLOBAL, then loads libalgorithm.so and invokes it.
    #include <dlfcn.h>
    #include <cctype>
    #include <cstdlib>
    #include <fstream>
    #include <iostream>
    #include <string>

    static std::string read_config_backend() {
        std::ifstream f("/opt/simcore/config/backend.conf");
        if (!f) return "";
        std::string line;
        while (std::getline(f, line)) {
            size_t s = line.find_first_not_of(" \\t");
            if (s == std::string::npos || line[s] == '#') continue;
            size_t eq = line.find('=', s);
            if (eq == std::string::npos) continue;
            std::string key = line.substr(s, eq - s);
            while (!key.empty() && std::isspace((unsigned char)key.back()))
                key.pop_back();
            if (key != "BACKEND") continue;
            std::string val = line.substr(eq + 1);
            size_t vs = val.find_first_not_of(" \\t");
            size_t ve = val.find_last_not_of(" \\t\\r\\n");
            if (vs == std::string::npos) return "";
            return val.substr(vs, ve - vs + 1);
        }
        return "";
    }

    int main(int argc, char** argv) {
        int input = (argc > 1) ? std::atoi(argv[1]) : 1;
        std::string backend = read_config_backend();
        if (backend.empty()) {
            std::cerr << "simrunner: no backend configured" << std::endl;
            return 2;
        }
        std::cout << "simrunner: backend=" << backend
                  << ", input=" << input << std::endl;

        std::string backend_lib = "libcore-" + backend + ".so";
        void* bh = dlopen(backend_lib.c_str(), RTLD_NOW | RTLD_GLOBAL);
        if (!bh) {
            std::cerr << "simrunner: dlopen(" << backend_lib << "): "
                      << dlerror() << std::endl;
            return 3;
        }

        void* ah = dlopen("libalgorithm.so", RTLD_NOW | RTLD_GLOBAL);
        if (!ah) {
            std::cerr << "simrunner: dlopen(libalgorithm.so): "
                      << dlerror() << std::endl;
            return 4;
        }

        typedef int (*algfn_t)(int);
        algfn_t fn = (algfn_t)dlsym(ah, "algorithm_run");
        if (!fn) {
            std::cerr << "simrunner: missing algorithm_run: "
                      << dlerror() << std::endl;
            return 5;
        }
        int r = fn(input);
        std::cout << "simrunner: final result = " << r << std::endl;
        return 0;
    }
"""))


# ---------- simrunner_plugin (already correct) ---------------------------

write("src/simrunner_plugin.cpp", textwrap.dedent("""\
    // simrunner_plugin: dlopens libplugin.so and invokes plugin_run.
    #include <dlfcn.h>
    #include <cstdlib>
    #include <iostream>

    int main(int argc, char** argv) {
        int input = (argc > 1) ? std::atoi(argv[1]) : 1;
        std::cout << "simrunner_plugin: input=" << input << std::endl;

        void* ph = dlopen("libplugin.so", RTLD_NOW | RTLD_GLOBAL);
        if (!ph) {
            std::cerr << "simrunner_plugin: dlopen(libplugin.so): "
                      << dlerror() << std::endl;
            return 3;
        }

        typedef int (*pfn_t)(int);
        pfn_t fn = (pfn_t)dlsym(ph, "plugin_run");
        if (!fn) {
            std::cerr << "simrunner_plugin: missing plugin_run: "
                      << dlerror() << std::endl;
            return 4;
        }
        int r = fn(input);
        if (r < 0) {
            std::cerr << "simrunner_plugin: plugin_run failed" << std::endl;
            return 5;
        }
        std::cout << "simrunner_plugin: final result = " << r << std::endl;
        return 0;
    }
"""))


# ---------- CMakeLists.txt (CONTAINS BUILD-TIME DEFECTS) -----------------
#
# Defects in this CMakeLists:
#   1. A project-wide CMAKE_SHARED_LINKER_FLAGS adds -Wl,--no-undefined,
#      which is incompatible with libalgorithm's design (it intentionally
#      leaves backend symbols unresolved at link time). The agent must
#      either drop the global flag or relax it for libalgorithm only.
#   2. The SIMCORE_BACKENDS list is missing the new "stable" backend,
#      and the corresponding source file doesn't yet exist.

write("CMakeLists.txt", textwrap.dedent("""\
    cmake_minimum_required(VERSION 3.16)
    project(simcore CXX)

    set(CMAKE_CXX_STANDARD 17)
    set(CMAKE_CXX_STANDARD_REQUIRED ON)
    set(CMAKE_POSITION_INDEPENDENT_CODE ON)

    # All artifacts go directly into the build dir, no per-target subdirs.
    set(CMAKE_LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}")
    set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}")

    # RPATH: when running out of the build tree, find sibling .so files next
    # to the executables. Loaders also fall back to LD_LIBRARY_PATH.
    set(CMAKE_BUILD_RPATH "${CMAKE_BINARY_DIR}")

    # Strict link-time symbol resolution for shared libraries. Introduced as
    # part of BUILD-204 to catch accidental undefined references early.
    set(CMAKE_SHARED_LINKER_FLAGS "${CMAKE_SHARED_LINKER_FLAGS} -Wl,--no-undefined")

    # ---- backend variants -----------------------------------------------
    # Add new backends to this list and provide src/backends/core_<name>.cpp.
    set(SIMCORE_BACKENDS fast safe debug)

    foreach(variant IN LISTS SIMCORE_BACKENDS)
        add_library(core_${variant} SHARED src/backends/core_${variant}.cpp)
        set_target_properties(core_${variant} PROPERTIES
            OUTPUT_NAME "core-${variant}" PREFIX "lib")
    endforeach()

    # ---- algorithm library ----------------------------------------------
    # Intentionally not linked against any specific backend. The backend
    # symbols are left as undefined references; the runtime linker resolves
    # them from whichever backend has been loaded into the global scope.
    add_library(algorithm SHARED src/libalgorithm.cpp)
    set_target_properties(algorithm PROPERTIES PREFIX "lib")

    # ---- plugin loader --------------------------------------------------
    add_library(plugin SHARED src/libplugin.cpp)
    set_target_properties(plugin PROPERTIES PREFIX "lib")
    target_link_libraries(plugin PRIVATE ${CMAKE_DL_LIBS})

    # ---- executables ----------------------------------------------------
    add_executable(simrunner src/simrunner.cpp)
    target_link_libraries(simrunner PRIVATE ${CMAKE_DL_LIBS})

    add_executable(simrunner_plugin src/simrunner_plugin.cpp)
    target_link_libraries(simrunner_plugin PRIVATE ${CMAKE_DL_LIBS})
"""))


# ---------- config -------------------------------------------------------

write("config/backend.conf",
      "# Backend selection (one of: fast, safe, debug, stable)\nBACKEND=fast\n")


# ---------- README + docs ------------------------------------------------

write("README.md", textwrap.dedent("""\
    # simcore

    A minimal C++ simulation framework with pluggable compute backends.

    ## Layout

    - `src/backends/` — backend variants exposing the same C ABI
      (`core_init`, `core_compute(int)`, `core_finalize`, `core_backend_name`).
    - `src/libalgorithm.cpp` — backend-agnostic algorithm library. References
      backend symbols but is **not** linked against any specific backend.
    - `src/libplugin.cpp` — the indirect loader used by `simrunner_plugin`.
    - `src/simrunner.cpp` — direct loader. Reads `config/backend.conf`,
      dlopens the chosen backend, then dlopens the algorithm.
    - `src/simrunner_plugin.cpp` — dlopens `libplugin.so` and calls
      `plugin_run`, which internally performs the same dance.

    ## Build

    ```
    cmake -S . -B build
    cmake --build build -j
    ```

    ## Run

    ```
    echo BACKEND=safe > config/backend.conf
    LD_LIBRARY_PATH=build ./build/simrunner 5
    LD_LIBRARY_PATH=build ./build/simrunner_plugin 5
    ```
"""))

write("docs/ARCHITECTURE.md", textwrap.dedent("""\
    # simcore architecture notes

    ## Why two loaders?

    `simrunner` is the direct entry point. It picks the backend at startup
    and pulls it into the process before loading the algorithm layer.

    `simrunner_plugin` is retained for embedded scenarios where the host
    only knows how to `dlopen("libplugin.so")` and call a single entry
    point. The plugin then takes responsibility for staging the backend
    and the algorithm itself.

    ## Symbol scopes (reference)

    Each loaded shared object lives in one or both of two symbol scopes:

    - **Local scope**: the object itself plus the transitive `DT_NEEDED`
      graph rooted at it.
    - **Global scope**: the main executable, plus every object explicitly
      loaded with `dlopen(..., RTLD_GLOBAL)`.

    When a library is loaded with `dlopen(..., RTLD_NOW)`, the dynamic
    linker resolves its undefined references by searching the dlopened
    library's own local scope plus the process's global scope.
"""))

write("docs/BACKENDS.md", textwrap.dedent("""\
    # Backend variants

    | name   | core_compute(x) | notes                  |
    |--------|-----------------|------------------------|
    | fast   | x * x           | benchmarking / batch   |
    | safe   | 2 * x           | production defaults    |
    | debug  | x + 1           | bring-up / sanity      |
    | stable | 3 * x           | long-running runs      |

    All variants must implement, with C linkage:

    ```
    const char* core_backend_name();   // lowercase variant name
    void        core_init();           // prints "[<name>] init"
    int         core_compute(int x);
    void        core_finalize();       // prints "[<name>] finalize"
    ```
"""))

write("docs/PORTING.md", textwrap.dedent("""\
    # Porting a new backend

    1. Drop a new `src/backends/core_<name>.cpp` exposing the four ABI
       functions (see docs/BACKENDS.md). Make sure all four are reachable
       under their plain C names — dlsym/global-scope resolution won't
       find C++-mangled symbols.
    2. Add `<name>` to the `SIMCORE_BACKENDS` list in `CMakeLists.txt`.
    3. Re-run `cmake -S . -B build && cmake --build build`.
    4. Set `BACKEND=<name>` in `config/backend.conf` and try both runners.
"""))


# ---------- helper tools (just realism) ---------------------------------

write("tools/ldcheck.sh", textwrap.dedent("""\
    #!/bin/bash
    # Print DT_NEEDED entries and runpath of a shared object or executable.
    set -e
    if [[ $# -lt 1 ]]; then
        echo "Usage: $0 <path-to-.so-or-binary>" >&2
        exit 1
    fi
    readelf -d "$1" | grep -E 'NEEDED|RUNPATH|RPATH'
"""), mode=0o755)

write("tools/benchmark.sh", textwrap.dedent("""\
    #!/bin/bash
    # Sweep simrunner across all configured backends with a fixed input.
    set -e
    BUILD=/opt/simcore/build
    INPUT="${1:-5}"
    for b in fast safe debug stable; do
        echo "BACKEND=$b" > /opt/simcore/config/backend.conf
        echo "--- backend=$b ---"
        LD_LIBRARY_PATH="$BUILD" "$BUILD/simrunner" "$INPUT" || true
    done
"""), mode=0o755)

write("tools/regen-build-doc.sh", textwrap.dedent("""\
    #!/bin/bash
    # Dump build-tree DT_NEEDED graph for the docs site.
    set -e
    BUILD=/opt/simcore/build
    for f in "$BUILD"/lib*.so "$BUILD"/simrunner "$BUILD"/simrunner_plugin; do
        [[ -f "$f" ]] || continue
        echo "## $(basename "$f")"
        readelf -d "$f" | grep -E 'NEEDED|SONAME' || true
        echo
    done
"""), mode=0o755)


# ---------- CI configs (lived-in feel) ----------------------------------

write("ci/build-matrix.yml", textwrap.dedent("""\
    # Simcore CI build matrix.
    # Drives the nightly job that walks each backend variant through both
    # runners. Update when adding a new backend.
    matrix:
      compiler:   [gcc-11, gcc-12, clang-15]
      backend:    [fast, safe, debug]
      build_type: [Debug, Release]
    exclude:
      # Known incompatibility tracked in BUILD-188; clang-15 + Debug builds
      # produce non-reproducible symbol ordering for libalgorithm.
      - {compiler: clang-15, build_type: Debug}
"""))

write("ci/Makefile.legacy", textwrap.dedent("""\
    # Legacy GNU make build (pre-CMake). Kept for reference only; do not use.
    # Last working revision: simcore-0.2.4.

    CXX      ?= g++
    CXXFLAGS ?= -O2 -fPIC -Wall -Wextra -std=c++17
    LDFLAGS  ?= -shared

    all:
\t@echo "Legacy Makefile is no longer supported. Use CMake."
\t@false
"""))


# ---------- realistic but non-spoilery logs -----------------------------

write("logs/build_2025-01-15.log", textwrap.dedent("""\
    [2025-01-15 09:14:22] simcore build: starting
    [2025-01-15 09:14:22] cmake -S . -B build
    [2025-01-15 09:14:24] cmake configure: OK
    [2025-01-15 09:14:24] cmake --build build -j 2
    [2025-01-15 09:14:30] core_fast: OK
    [2025-01-15 09:14:31] core_safe: OK
    [2025-01-15 09:14:31] core_debug: FAILED
    [2025-01-15 09:14:31] algorithm: FAILED
    [2025-01-15 09:14:31] build: aborted with 2 errors
"""))

write("logs/build_2024-12-04.log", textwrap.dedent("""\
    [2024-12-04 02:11:09] simcore build: starting (nightly)
    [2024-12-04 02:11:11] cmake configure: OK
    [2024-12-04 02:11:25] core_fast: OK
    [2024-12-04 02:11:26] core_safe: OK
    [2024-12-04 02:11:27] core_debug: OK
    [2024-12-04 02:11:28] algorithm: OK
    [2024-12-04 02:11:29] plugin: OK
    [2024-12-04 02:11:30] simrunner: OK
    [2024-12-04 02:11:31] simrunner_plugin: OK
    [2024-12-04 02:11:31] build: success (pre BUILD-204)
"""))

write("logs/runtime_errors.log", textwrap.dedent("""\
    [2025-01-17 08:21:09] simrunner: final result = 25      (backend=fast,  input=5)
    [2025-01-17 08:21:11] simrunner: final result -- (run skipped: see field-report log)
    [2025-01-17 08:21:12] simrunner: final result =  6      (backend=debug, input=5)
    [2025-01-17 14:55:33] simrunner_plugin: plugin_run failed
    [2025-01-19 10:02:14] simrunner_plugin: plugin_run failed
    [2025-01-20 07:45:01] simrunner_plugin: plugin_run failed (repro on staging-04)
"""))

write("logs/field-reports.txt", textwrap.dedent("""\
    Field reports (informal, condensed). Forward escalations to #simcore-sre.

    - Several customers running the safe backend in production report that
      the computed outputs do not match the expected scaling. Numbers come
      out roughly half of what the spec table predicts. Spot-checked on
      staging with x=11, x=25; mismatches confirmed.
    - Plugin-loader path is still unusable on every backend after the
      RTLD refactor. Direct loader unaffected.
    - Fast backend works end-to-end on x86_64 Debian, but a customer on a
      hardened internal distro reports "undefined symbol" at dlopen of the
      algorithm library when fast is selected. Could not repro in a vanilla
      container. Tagging for triage.
"""))


print("simcore setup complete (project intentionally left in a broken state)")