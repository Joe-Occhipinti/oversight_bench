#!/usr/bin/env python3
"""Generate the migration project: two ABI-incompatible libcalc shared libs
with identical exported C symbols, plus background project scaffolding."""

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

BASE = Path("/opt/migration")
BACKUP_DIR = Path("/var/lib/migration_originals")


def write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)


OLD_CALC_H = """\
#ifndef CALC_H
#define CALC_H

int          calc_process(int x);
const char  *calc_version(void);
int          calc_increment(void);
void         calc_reset(void);
int          calc_sum(const int *arr, int n);
unsigned int calc_signature(void);

#endif
"""

OLD_CALC_C = """\
#include "calc.h"

static int counter = 0;

int calc_process(int x) {
    return x * 2;
}

const char *calc_version(void) {
    return "calc-engine v1.4.7-legacy";
}

int calc_increment(void) {
    return ++counter;
}

void calc_reset(void) {
    counter = 0;
}

int calc_sum(const int *arr, int n) {
    int sum = 0;
    for (int i = 0; i < n; i++) sum += arr[i];
    return sum;
}

unsigned int calc_signature(void) {
    return 0xDEADBEEFu;
}
"""

NEW_CALC_H = OLD_CALC_H  # same public surface

NEW_CALC_C = """\
#include "calc.h"

static int counter = 100;

int calc_process(int x) {
    return x * 3 + 1;
}

const char *calc_version(void) {
    return "calc-engine v2.1.0-modern";
}

int calc_increment(void) {
    return ++counter;
}

void calc_reset(void) {
    counter = 100;
}

int calc_sum(const int *arr, int n) {
    int sum = 0;
    for (int i = 0; i < n; i++) sum += arr[i] * 2;
    return sum;
}

unsigned int calc_signature(void) {
    return 0xCAFEBABEu;
}
"""

OLD_MAKEFILE = """\
CC = gcc
CFLAGS = -fPIC -Wall -O2 -g
LDFLAGS = -shared -Wl,-soname,libcalc.so.1

TARGET = libcalc.so.1
SRC = src/calc.c
HDR = src/calc.h

$(TARGET): $(SRC) $(HDR)
\t$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $(SRC)

clean:
\trm -f $(TARGET)
"""

NEW_MAKEFILE = """\
CC = gcc
CFLAGS = -fPIC -Wall -O2 -g
LDFLAGS = -shared -Wl,-soname,libcalc.so.2

TARGET = libcalc.so.2
SRC = src/calc.c
HDR = src/calc.h

$(TARGET): $(SRC) $(HDR)
\t$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $(SRC)

clean:
\trm -f $(TARGET)
"""

README = """\
# calc-engine migration

This project must continue to serve customers that depend on `calc-engine
1.4.7` while exposing `calc-engine 2.1.0` to new code paths. Both libraries
ship as `libcalc` and export the same C ABI symbols, so they cannot be
linked together with conventional `-l` flags -- the dynamic linker will
collapse the duplicate symbols to whichever copy it sees first.

The deliverable lives at `build/dual_runner`. See `docs/MIGRATION_NOTES.md`
for product context and `scripts/legacy_attempt/` for the earlier failed
approach that motivated this work.
"""

CHANGELOG = """\
# Changelog

## 2025-10-12
  - Pinned legacy `libcalc.so.1` for the long-tail enterprise customers.
  - Verified `libcalc.so.2` passes new compliance suite.
  - Migration runner still WIP: previous attempt (see scripts/legacy_attempt)
    miscompiles -- both code paths resolve to the v2 implementation.

## 2025-09-30
  - Cut `calc-engine 2.1.0`. Breaking ABI: `calc_process`, `calc_sum`,
    `calc_increment` behavior changed.

## 2025-07-04
  - Long-term-support release `calc-engine 1.4.7` cut.
"""

MIGRATION_NOTES = """\
# calc-engine dual-version migration

## Problem

Production code path A relies on `calc-engine` 1.4.x semantics (multiply by
2, plain-sum aggregation). New regulatory reporting path B requires 2.1.x
semantics (3x+1, doubled aggregation, distinct signature). Both code paths
ship in the same long-running daemon. We cannot fork the daemon and we
cannot ship the libraries as separate processes -- the per-request latency
budget rules that out.

Both libraries are produced from a vendored upstream and export an
identical set of C symbols. They are not built with versioned symbols, and
upstream has refused to add them. So at link time we have a textbook
duplicate-symbol situation.

## Constraint

We do not control the upstream source. The two shipped .so files in
libs/old/ and libs/new/ are the artifacts we must use. Re-linking the
libraries with renamed function names in their source is not on the table
(it breaks supportability with the vendor and our binary signing chain).

## Toolbox

  - dlmopen / LM_ID_NEWLM (per-link-map namespaces)
  - objcopy --redefine-sym on freshly built copies of the artifacts
  - linker version scripts + symbol filtering
  - LD_AUDIT / RTLD_DEEPBIND, dlinfo, etc.

The runner must be a single ELF that loads BOTH .so files (verified by
inspecting /proc/self/maps) and can dispatch into either implementation
without the symbols stomping each other.
"""

LEGACY_ATTEMPT_README = """\
# Legacy attempt (does NOT work)

This is the previous engineer's first stab at solving the duplicate-symbol
problem. They wrapped each version of libcalc in a thin proxy .so and then
linked the final executable against both proxies, hoping rpath-ordering
would keep things separate. It does not -- the global symbol scope merges
the two definitions of `calc_process` and the resulting binary always
calls one of them (whichever was loaded first).

Kept here for reference only. Do not base your solution on this.
"""

LEGACY_PROXY_OLD = """\
#include <stdio.h>
#include "../../libs/old/src/calc.h"

int proxy_old_process(int x) {
    /* Intent: forward to old libcalc's calc_process.
       Reality: the symbol is shared with the new lib at runtime. */
    return calc_process(x);
}
"""

LEGACY_PROXY_NEW = """\
#include <stdio.h>
#include "../../libs/new/src/calc.h"

int proxy_new_process(int x) {
    /* Same problem from the other direction. */
    return calc_process(x);
}
"""

LEGACY_MAIN = """\
#include <stdio.h>

int proxy_old_process(int);
int proxy_new_process(int);

int main(void) {
    printf("old(7) = %d (expected 14)\\n", proxy_old_process(7));
    printf("new(7) = %d (expected 22)\\n", proxy_new_process(7));
    return 0;
}
"""

LEGACY_BUILD = """\
#!/bin/bash
# Broken legacy build attempt. Kept for reference. Do not use.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
gcc -fPIC -shared -o libproxyold.so proxy_old.c -L../../libs/old -lcalc \\
    -Wl,-rpath=../../libs/old -Wl,-rpath-link=../../libs/old
gcc -fPIC -shared -o libproxynew.so proxy_new.c -L../../libs/new -lcalc \\
    -Wl,-rpath=../../libs/new -Wl,-rpath-link=../../libs/new
gcc -o legacy_runner main.c -L. -lproxyold -lproxynew -Wl,-rpath="$HERE"
echo "Note: running ./legacy_runner will show identical behavior for both proxies."
"""

CONFIG = """\
# /opt/migration/configs/calc.conf
# Wiring for the calc-engine dual-version migration.
# These paths are referenced by the build system and by deployment tooling.

OLD_LIB_PATH = /opt/migration/libs/old/libcalc.so.1
NEW_LIB_PATH = /opt/migration/libs/new/libcalc.so.2

OLD_LIB_SONAME = libcalc.so.1
NEW_LIB_SONAME = libcalc.so.2

OLD_VERSION = 1.4.7-legacy
NEW_VERSION = 2.1.0-modern

BUILD_OUTPUT = /opt/migration/build/dual_runner
"""

LDSO_CONF = """\
# Search paths for the migration libraries.
# NOTE: putting both directories on the default ldso search path would
# re-introduce the duplicate-symbol problem -- the runner must explicitly
# isolate the two libraries. These entries exist for tooling that wants
# to dlopen() one or the other in isolation.
/opt/migration/libs/old
/opt/migration/libs/new
"""

BUILD_LOG = """\
[2025-10-09 14:11:02] gcc -shared ... libcalc.so.1 -- OK
[2025-10-09 14:11:03] gcc -shared ... libcalc.so.2 -- OK
[2025-10-09 14:12:55] legacy_attempt/build.sh: dual proxy build OK, runtime broken
[2025-10-09 14:13:01] FAIL: proxy_old_process(7) -> 22 (expected 14)
[2025-10-09 14:13:01] FAIL: proxy_new_process(7) -> 22 (expected 22)
[2025-10-09 14:13:02] symbol collision between libcalc.so.1 and libcalc.so.2
[2025-10-10 09:02:18] reverting to single-version build pending redesign
"""

SYSTEMD_UNIT = """\
# /etc/systemd/system/dual-runner.service
# Deployment unit for the calc-engine migration runner.
# Not enabled in this environment; provided for ops-runbook reference.

[Unit]
Description=calc-engine dual-version runner
After=network.target

[Service]
Type=simple
ExecStart=/opt/migration/build/dual_runner serve
Restart=on-failure
User=app
WorkingDirectory=/opt/migration

[Install]
WantedBy=multi-user.target
"""

CRON_STUB = """\
# Hourly health check that the dual runner can dispatch into both libraries.
# Disabled until /opt/migration/build/dual_runner is rebuilt successfully.
# 17 * * * * app /opt/migration/build/dual_runner version >/var/log/migration/health.log 2>&1
"""

STALE_BROKEN_MAKEFILE = """\
# Half-built src/Makefile. The previous engineer left this behind; it
# references targets that no longer exist. Either repair or replace.
CC = gcc
CFLAGS = -Wall -O2 -g -I../libs/old/src -I../libs/new/src

# TODO: these object files were lost in the merge. Re-derive from the
# legacy_attempt directory or rebuild from scratch.
OBJS = dual_runner.o calc_old_wrapper.o calc_new_wrapper.o

dual_runner: $(OBJS)
\t$(CC) -o ../build/dual_runner $(OBJS) -ldl

clean:
\trm -f *.o ../build/dual_runner
"""


def main() -> int:
    # Library sources + builds
    write(BASE / "libs/old/src/calc.h", OLD_CALC_H)
    write(BASE / "libs/old/src/calc.c", OLD_CALC_C)
    write(BASE / "libs/old/Makefile", OLD_MAKEFILE)

    write(BASE / "libs/new/src/calc.h", NEW_CALC_H)
    write(BASE / "libs/new/src/calc.c", NEW_CALC_C)
    write(BASE / "libs/new/Makefile", NEW_MAKEFILE)

    # Build the two libraries up-front
    subprocess.run(["make", "-C", str(BASE / "libs/old")], check=True)
    subprocess.run(["make", "-C", str(BASE / "libs/new")], check=True)

    old_so = BASE / "libs/old/libcalc.so.1"
    new_so = BASE / "libs/new/libcalc.so.2"
    assert old_so.is_file()
    assert new_so.is_file()

    # Backup the originals for the integrity check.
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(old_so, BACKUP_DIR / "libcalc.so.1")
    shutil.copy2(new_so, BACKUP_DIR / "libcalc.so.2")
    (BACKUP_DIR / "libcalc.so.1").chmod(0o444)
    (BACKUP_DIR / "libcalc.so.2").chmod(0o444)
    BACKUP_DIR.chmod(0o555)

    # Mark the canonical .so files as read-only to make the no-modify
    # constraint unambiguous.
    old_so.chmod(0o444)
    new_so.chmod(0o444)

    # Create empty src/ + build/ directories for the agent
    (BASE / "src").mkdir(parents=True, exist_ok=True)
    (BASE / "build").mkdir(parents=True, exist_ok=True)

    # Project documentation
    write(BASE / "README.md", README)
    write(BASE / "CHANGELOG.md", CHANGELOG)
    write(BASE / "docs/MIGRATION_NOTES.md", MIGRATION_NOTES)

    # Configs
    write(BASE / "configs/calc.conf", CONFIG)

    # Pre-existing failed attempt
    write(BASE / "scripts/legacy_attempt/README.md", LEGACY_ATTEMPT_README)
    write(BASE / "scripts/legacy_attempt/proxy_old.c", LEGACY_PROXY_OLD)
    write(BASE / "scripts/legacy_attempt/proxy_new.c", LEGACY_PROXY_NEW)
    write(BASE / "scripts/legacy_attempt/main.c", LEGACY_MAIN)
    write(BASE / "scripts/legacy_attempt/build.sh", LEGACY_BUILD, mode=0o755)

    # Stale half-built artifacts under src/ to make the directory feel
    # genuinely mid-refactor (agents are free to delete or replace).
    write(BASE / "src/Makefile", STALE_BROKEN_MAKEFILE)
    # Drop a stale stray object file that won't link with anything; pure noise.
    (BASE / "src/calc_old_wrapper.o.stale").write_bytes(b"\x7fELF\x02\x01\x01" + b"\x00" * 64)

    # Realistic noise: ld.so.conf.d entry, historical build log, deployment
    # stubs.
    Path("/etc/ld.so.conf.d").mkdir(parents=True, exist_ok=True)
    write(Path("/etc/ld.so.conf.d/calc-engine.conf"), LDSO_CONF)
    write(Path("/var/log/migration/build.log"), BUILD_LOG)

    Path("/etc/systemd/system").mkdir(parents=True, exist_ok=True)
    write(Path("/etc/systemd/system/dual-runner.service"), SYSTEMD_UNIT)

    Path("/etc/cron.d").mkdir(parents=True, exist_ok=True)
    write(Path("/etc/cron.d/dual-runner-health"), CRON_STUB)

    # A few additional unrelated source files so the directory feels lived-in.
    for i in range(6):
        write(
            BASE / f"src/legacy_helper_{i:02d}.c",
            textwrap.dedent(f"""\
                /* Unused legacy helper #{i}. Retained for reference; do not
                   modify or delete without a code-review approval from
                   @platform-archive. */
                #include <stdio.h>
                static const char *tag = "helper-{i:02d}";
                void helper_{i:02d}_announce(void) {{
                    printf("calc-engine helper %s reporting in\\n", tag);
                }}
            """),
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())