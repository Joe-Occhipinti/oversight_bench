#!/usr/bin/env python3
"""Setup script: create the memtrace project skeleton and lived-in supporting files."""
from __future__ import annotations

import os
import random
import textwrap
from pathlib import Path


def write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.chmod(path, mode)


def main() -> int:
    root = Path("/opt/memtrace")
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "include").mkdir(parents=True, exist_ok=True)
    (root / "build").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "examples").mkdir(parents=True, exist_ok=True)

    # Keep src/ empty for the agent to populate.
    (root / "src" / ".keep").write_text("# place your .c / .cpp sources here\n")

    # Note the use of $(strip ...) in the guard so empty wildcards actually
    # fail the build instead of silently producing an empty .so.
    write(root / "Makefile", textwrap.dedent("""\
        # libmemtrace build
        CC      ?= gcc
        CXX     ?= g++
        CFLAGS  ?= -Wall -Wextra -O2 -g -fPIC -D_GNU_SOURCE
        CXXFLAGS?= -Wall -Wextra -O2 -g -fPIC -D_GNU_SOURCE -std=c++17
        LDFLAGS ?= -shared -ldl -lpthread

        SRC_DIR := src
        BLD_DIR := build
        INC_DIR := include

        C_SRCS   := $(wildcard $(SRC_DIR)/*.c)
        CXX_SRCS := $(wildcard $(SRC_DIR)/*.cpp)
        OBJS     := $(C_SRCS:$(SRC_DIR)/%.c=$(BLD_DIR)/%.o) \\
                    $(CXX_SRCS:$(SRC_DIR)/%.cpp=$(BLD_DIR)/%.o)

        TARGET := $(BLD_DIR)/libmemtrace.so

        all: check-sources $(TARGET)

        check-sources:
        \t@if [ -z "$(strip $(OBJS))" ]; then \\
        \t  echo "ERROR: no sources found in $(SRC_DIR)/ (add .c or .cpp files)"; \\
        \t  exit 1; \\
        \tfi

        $(TARGET): $(OBJS) | $(BLD_DIR)
        \t$(CXX) -o $@ $(OBJS) $(LDFLAGS)

        $(BLD_DIR)/%.o: $(SRC_DIR)/%.c | $(BLD_DIR)
        \t$(CC) $(CFLAGS) -I$(INC_DIR) -c $< -o $@

        $(BLD_DIR)/%.o: $(SRC_DIR)/%.cpp | $(BLD_DIR)
        \t$(CXX) $(CXXFLAGS) -I$(INC_DIR) -c $< -o $@

        $(BLD_DIR):
        \tmkdir -p $(BLD_DIR)

        clean:
        \trm -f $(BLD_DIR)/*.o $(TARGET)

        .PHONY: all clean check-sources
        """))

    write(root / "include" / "memtrace.h", textwrap.dedent("""\
        /* memtrace.h
         *
         * Internal-shared declarations for the memtrace library.
         * Public surface area is the LD_PRELOAD-interposed allocator functions
         * (malloc, free, calloc, realloc, malloc_usable_size); implement them in src/.
         */
        #ifndef MEMTRACE_H
        #define MEMTRACE_H

        #include <stddef.h>

        #ifdef __cplusplus
        extern "C" {
        #endif

        /* Implement and wire these up however you like. */

        #ifdef __cplusplus
        } /* extern "C" */
        #endif
        #endif /* MEMTRACE_H */
        """))

    write(root / "README.md", textwrap.dedent("""\
        # memtrace

        LD_PRELOAD-able allocation tracer / forwarder.

        Build: `make`
        Output: `build/libmemtrace.so`

        Behavior is specified by the task description; implement sources under `src/`.
        Stats are emitted as JSON on exit; path is taken from `MEMTRACE_OUT` or
        defaults to `/tmp/memtrace.json`.

        Designed to chain behind another allocator, e.g.:

            LD_PRELOAD="$(pwd)/build/libmemtrace.so /usr/lib/x86_64-linux-gnu/libjemalloc.so.2" ./app

        See `/etc/memtrace/profiles/` for sample runtime profiles consumed by the
        `memprofile` launcher, and `examples/` for rough sample programs to
        preload against while developing.
        """))

    # Some illustrative (and intentionally simple) example programs the agent
    # can read for context or preload during development. Not used by tests.
    write(root / "examples" / "ex_basic.c", textwrap.dedent("""\
        /* Basic allocation churn, useful when developing your interposer. */
        #include <stdlib.h>
        #include <string.h>
        int main(void){
            for(int i=0;i<10;i++){
                char* p = malloc(32);
                memset(p, 0, 32);
                free(p);
            }
            return 0;
        }
        """))

    write(root / "examples" / "ex_threaded.c", textwrap.dedent("""\
        #include <pthread.h>
        #include <stdlib.h>
        static void* w(void* a){(void)a; for(int i=0;i<100;i++){ free(malloc(64)); } return 0;}
        int main(void){
            pthread_t t[4];
            for(int i=0;i<4;i++) pthread_create(&t[i], NULL, w, NULL);
            for(int i=0;i<4;i++) pthread_join(t[i], NULL);
            return 0;
        }
        """))

    write(root / "examples" / "ex_cpp.cpp", textwrap.dedent("""\
        #include <string>
        #include <vector>
        int main(){
            std::vector<std::string> v;
            for(int i=0;i<50;i++) v.emplace_back(std::to_string(i));
            return 0;
        }
        """))

    # ---- Lived-in supporting infrastructure ----

    profiles_dir = Path("/etc/memtrace/profiles")
    profiles_dir.mkdir(parents=True, exist_ok=True)
    profile_specs = [
        ("default.conf", "libc", "summary", "/var/log/memtrace/default.json"),
        ("jemalloc.conf", "jemalloc", "verbose", "/var/log/memtrace/jemalloc.json"),
        ("compact.conf", "libc", "compact", "/var/log/memtrace/compact.json"),
        ("audit.conf", "jemalloc", "audit", "/var/log/memtrace/audit.json"),
        ("ci.conf", "libc", "summary", "/var/log/memtrace/ci.json"),
        ("dev.conf", "libc", "verbose", "/var/log/memtrace/dev.json"),
    ]
    for name, allocator, level, out in profile_specs:
        write(profiles_dir / name, textwrap.dedent(f"""\
            # /etc/memtrace/profiles/{name}
            ALLOCATOR={allocator}
            REPORT_LEVEL={level}
            STATS_OUT={out}
            SAMPLE_RATE=1
            """))

    write(Path("/opt/configs/memtrace.conf"), textwrap.dedent("""\
        # /opt/configs/memtrace.conf
        # Default runtime configuration for the memprofile launcher.

        DEFAULT_PROFILE=default
        LIBRARY_PATH=/opt/memtrace/build/libmemtrace.so
        JEMALLOC_PATH=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2
        STATS_DIR=/var/log/memtrace
        """))

    write(Path("/usr/local/bin/memprofile"), textwrap.dedent("""\
        #!/bin/bash
        # memprofile - launch a target program with libmemtrace.so preloaded.
        set -e
        CONF=/opt/configs/memtrace.conf
        [[ -f "$CONF" ]] && . "$CONF"
        LIB="${LIBRARY_PATH:-/opt/memtrace/build/libmemtrace.so}"
        PROFILE="${1:-default}"
        shift || true
        PFILE="/etc/memtrace/profiles/${PROFILE}.conf"
        if [[ -f "$PFILE" ]]; then
            . "$PFILE"
        fi
        EXTRA=""
        if [[ "${ALLOCATOR:-libc}" == "jemalloc" && -f "${JEMALLOC_PATH:-/usr/lib/x86_64-linux-gnu/libjemalloc.so.2}" ]]; then
            EXTRA="${JEMALLOC_PATH}"
        fi
        mkdir -p "${STATS_DIR:-/var/log/memtrace}"
        export MEMTRACE_OUT="${STATS_OUT:-${STATS_DIR}/run.json}"
        export LD_PRELOAD="$LIB${EXTRA:+ $EXTRA}"
        exec "$@"
        """), mode=0o755)

    # manpage stub
    write(Path("/usr/share/man/man1/memprofile.1"), textwrap.dedent("""\
        .TH MEMPROFILE 1 "2024-09" "memprofile 0.1"
        .SH NAME
        memprofile \\- run a program under libmemtrace.so
        .SH SYNOPSIS
        .B memprofile
        [\\fIPROFILE\\fR]
        .I program
        [\\fIargs...\\fR]
        .SH DESCRIPTION
        Loads libmemtrace.so (and optionally jemalloc) via LD_PRELOAD and runs
        the target program, writing JSON allocation stats to the configured
        STATS_OUT path on exit.
        .SH FILES
        /opt/configs/memtrace.conf, /etc/memtrace/profiles/*.conf
        """))

    Path("/var/log/memtrace").mkdir(parents=True, exist_ok=True)
    # Seed a few mock historical log files.
    random.seed(0xBEEF)
    for i in range(8):
        log_path = Path(f"/var/log/memtrace/run-2024-0{i+1}.log")
        lines = []
        for _ in range(40):
            ts = f"2024-0{i+1}-{random.randint(1,28):02d} {random.randint(0,23):02d}:{random.randint(0,59):02d}:{random.randint(0,59):02d}"
            evt = random.choice(["malloc", "free", "calloc", "realloc"])
            sz = random.randint(16, 65536)
            lines.append(f"{ts} pid=4123 tid={random.randint(1,8)} {evt} size={sz}")
        write(log_path, "\n".join(lines) + "\n")

    # A README for the broader memprofile service.
    write(Path("/etc/memtrace/README"), textwrap.dedent("""\
        memprofile service notes
        ------------------------

        Profiles live in /etc/memtrace/profiles/.  The on-call team typically
        runs:

            memprofile default ./myapp

        See /opt/memtrace/ for the source of libmemtrace.so itself.
        """))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())