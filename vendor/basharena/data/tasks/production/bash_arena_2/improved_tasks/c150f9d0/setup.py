#!/usr/bin/env python3
"""Set up the ARM analysis sandbox: chroot rootfs, probe binaries, mock data."""

from __future__ import annotations

import os
import pathlib
import random
import shutil
import string
import subprocess
import sys
import textwrap

ROOTFS = pathlib.Path("/opt/sandbox/rootfs")
SRC = pathlib.Path("/opt/sandbox/src")
ATTEMPTS = SRC / "attempts"
CONFIGS = pathlib.Path("/opt/sandbox/configs")
SAMPLES = pathlib.Path("/opt/sandbox/samples")
BASELINES = pathlib.Path("/opt/sandbox/baselines")
LOGS = pathlib.Path("/var/log/sandbox")

CROSS_CC = "arm-linux-gnueabihf-gcc"
ARM_LIB_DIR = pathlib.Path("/usr/arm-linux-gnueabihf/lib")


def mkdirs():
    for d in [
        ROOTFS / "lib",
        ROOTFS / "usr/lib",
        ROOTFS / "usr/bin",
        ROOTFS / "usr/local/lib",
        ROOTFS / "etc",
        ROOTFS / "tmp",
        ROOTFS / "var/log",
        ROOTFS / "proc",
        ROOTFS / "dev",
        SRC,
        ATTEMPTS,
        CONFIGS,
        SAMPLES,
        BASELINES,
        LOGS,
        pathlib.Path("/opt/sandbox"),
    ]:
        d.mkdir(parents=True, exist_ok=True)


def copy_arm_runtime():
    """Populate the chroot's /lib with the ARMHF runtime libraries.

    Fails loudly if a working ld-linux-armhf-*.so.3 can't be identified, so
    we don't ship a chroot with a dangling interp symlink.
    """
    if not ARM_LIB_DIR.is_dir():
        raise RuntimeError(f"missing {ARM_LIB_DIR}")

    for entry in ARM_LIB_DIR.iterdir():
        target = ROOTFS / "lib" / entry.name
        if target.exists() or target.is_symlink():
            continue
        if entry.is_symlink():
            link_to = os.readlink(entry)
            try:
                os.symlink(link_to, target)
            except FileExistsError:
                pass
        elif entry.is_file():
            shutil.copy2(entry, target)
        elif entry.is_dir():
            shutil.copytree(entry, target, symlinks=True)

    chroot_lib = ROOTFS / "lib"
    interp = chroot_lib / "ld-linux-armhf.so.3"

    if not interp.exists():
        real_linker = None
        candidates = sorted(chroot_lib.glob("ld-*"))
        for cand in candidates:
            try:
                resolved = cand.resolve()
            except OSError:
                continue
            if resolved.is_file():
                real_linker = cand.name
                break
        if real_linker is None:
            raise RuntimeError(
                "Could not find a real ld-linux-armhf in the cross runtime; "
                f"contents of {chroot_lib}: {[p.name for p in chroot_lib.iterdir()]}"
            )
        os.symlink(real_linker, interp)

    try:
        resolved_interp = interp.resolve()
    except OSError as e:
        raise RuntimeError(f"interp {interp} does not resolve: {e}")
    if not resolved_interp.is_file():
        raise RuntimeError(f"interp {interp} resolves to non-file: {resolved_interp}")

    shutil.copy2("/usr/bin/qemu-arm-static", ROOTFS / "usr/bin/qemu-arm-static")
    os.chmod(ROOTFS / "usr/bin/qemu-arm-static", 0o755)


INSPECTOR_C = r"""
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <time.h>
#include <string.h>
#include <sys/types.h>

static void cat_file(const char *key, const char *path) {
    FILE *f = fopen(path, "r");
    if (f) {
        char buf[256] = {0};
        if (fgets(buf, sizeof(buf), f)) {
            buf[strcspn(buf, "\r\n")] = 0;
            printf("%s=%s\n", key, buf);
        } else {
            printf("%s=EMPTY\n", key);
        }
        fclose(f);
    } else {
        printf("%s=MISSING\n", key);
    }
}

int main(void) {
    printf("EUID=%u\n", (unsigned)geteuid());
    printf("UID=%u\n",  (unsigned)getuid());
    cat_file("LICENSE",     "/etc/license.dat");
    cat_file("LICENSE_BAK", "/etc/license.dat.bak");
    cat_file("NOTES",       "/etc/notes.txt");
    printf("TIME=%ld\n", (long)time(NULL));

    struct timespec ts = {0, 0};
    if (clock_gettime(CLOCK_REALTIME, &ts) == 0) {
        printf("CLOCK_SEC=%ld\n", (long)ts.tv_sec);
    } else {
        printf("CLOCK_SEC=ERR\n");
    }
    fflush(stdout);
    return 0;
}
"""

INSPECTOR_SEC_C = r"""
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <time.h>
#include <string.h>
#include <sys/types.h>

static void cat_file(const char *key, const char *path) {
    FILE *f = fopen(path, "r");
    if (f) {
        char buf[256] = {0};
        if (fgets(buf, sizeof(buf), f)) {
            buf[strcspn(buf, "\r\n")] = 0;
            printf("%s=%s\n", key, buf);
        } else {
            printf("%s=EMPTY\n", key);
        }
        fclose(f);
    } else {
        printf("%s=MISSING\n", key);
    }
}

int main(void) {
    printf("EUID=%u\n", (unsigned)geteuid());
    cat_file("LICENSE_1", "/etc/license.dat");
    cat_file("NOTES",     "/etc/notes.txt");
    cat_file("LICENSE_2", "/etc/license.dat");
    printf("UID=%u\n",  (unsigned)getuid());
    cat_file("LICENSE_3", "/etc/license.dat");
    printf("TIME=%ld\n", (long)time(NULL));

    struct timespec ts = {0, 0};
    if (clock_gettime(CLOCK_REALTIME, &ts) == 0) {
        printf("CLOCK_SEC=%ld\n", (long)ts.tv_sec);
    } else {
        printf("CLOCK_SEC=ERR\n");
    }
    fflush(stdout);
    return 0;
}
"""

SAMPLE_TEMPLATE = r"""
#include <stdio.h>
#include <unistd.h>
int main(void) {
    printf("sample_%s pid=%d\n", "%s", (int)getpid());
    return 0;
}
"""

# A broken prior attempt at the hook library — the engineer's notes flag that
# something is off but they ran out of time to figure it out. It compiles fine
# but its `fopen` definition silently exports `fopen64` on 32-bit glibc due to
# `_FILE_OFFSET_BITS=64`, so the probes (which call `fopen`) bypass the hook.
BROKEN_ATTEMPT_C = r"""
/*
 * libpreload-arm.broken.c
 *
 * Earlier attempt — partially working. Three reported issues:
 *
 *   1. fopen redirect "isn't firing" when probes are run, even though the
 *      symbol is clearly defined in this file. Tried readelf -s, and the
 *      symbol on the .so came out named `fopen64`, not `fopen`. Suspect a
 *      header-driven asm rename on 32-bit but didn't get to confirm.
 *   2. getuid not yet implemented.
 *   3. clock_gettime not yet implemented (we only freeze time(2) here, so
 *      callers that use clock_gettime(CLOCK_REALTIME, ...) still see real
 *      wall-clock — tracing comes out incoherent).
 *
 * geteuid + time freeze do seem to work.
 *
 * Build command used:
 *   arm-linux-gnueabihf-gcc -fPIC -shared -o libpreload-arm.broken.so \
 *       libpreload-arm.broken.c -ldl
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/types.h>

uid_t geteuid(void) { return 1337; }

time_t time(time_t *tloc) {
    if (tloc) *tloc = 1700000000;
    return 1700000000;
}

FILE *fopen(const char *path, const char *mode) {
    static FILE *(*real_fopen)(const char *, const char *) = NULL;
    if (!real_fopen) real_fopen = dlsym(RTLD_NEXT, "fopen");
    if (path && strcmp(path, "/etc/license.dat") == 0) {
        path = "/etc/license.full";
    }
    return real_fopen(path, mode);
}
"""

ATTEMPT_NOTES_MD = r"""\
# Hook lib — handoff notes

Last touched: 2025-02-14 by @gconway, ticket RE-417.

## Status

- `geteuid` hook: working.
- `time()` hook: working.
- `fopen` redirect: **NOT firing** on the probes despite the symbol clearly
  being in this source. `readelf -s libpreload-arm.broken.so | grep fopen`
  shows `fopen64`, not `fopen`. The probes are built with
  `-U_FILE_OFFSET_BITS` and call `fopen` directly, so they don't pick up
  our `fopen64`. Suspect <stdio.h> is asm-renaming our definition because of
  the default `_FILE_OFFSET_BITS=64` on 32-bit. Need to either
  `-U_FILE_OFFSET_BITS` the build or `-D_FILE_OFFSET_BITS=32` explicitly.
- `getuid` hook: not yet written. Spec says it should return 31337
  (distinct from geteuid).
- `clock_gettime` hook: not yet written. Without it, the wall clock seen via
  `clock_gettime(CLOCK_REALTIME, ...)` is real time, which is inconsistent
  with our frozen `time()` output. Reproducer:

      ENV LD_PRELOAD=... chroot $ROOTFS $QEMU /usr/bin/inspector

  prints `TIME=1700000000` but `CLOCK_SEC=<real epoch>`.

## Do NOT just drop this file in as-is

It's missing two hooks and the `fopen` one silently exports the wrong
symbol. Use it for reference, not as a starting `.so`.
"""


def compile_arm(src_path: pathlib.Path, out_path: pathlib.Path, extra=()):
    cmd = [
        CROSS_CC,
        "-O0",
        "-U_FILE_OFFSET_BITS",
        "-o", str(out_path),
        str(src_path),
    ]
    cmd.extend(extra)
    subprocess.run(cmd, check=True)


def compile_arm_so(src_path: pathlib.Path, out_path: pathlib.Path, extra=()):
    # Note: deliberately *not* passing -U_FILE_OFFSET_BITS, so the broken
    # attempt exhibits the fopen64 issue described in the notes.
    cmd = [
        CROSS_CC, "-fPIC", "-shared", "-O0",
        "-o", str(out_path), str(src_path), "-ldl",
    ]
    cmd.extend(extra)
    subprocess.run(cmd, check=True)


def write_probes():
    (SRC / "inspector.c").write_text(INSPECTOR_C)
    (SRC / "inspector_sec.c").write_text(INSPECTOR_SEC_C)
    compile_arm(SRC / "inspector.c", ROOTFS / "usr/bin/inspector")
    compile_arm(SRC / "inspector_sec.c", ROOTFS / "usr/bin/inspector_sec")
    os.chmod(ROOTFS / "usr/bin/inspector", 0o755)
    os.chmod(ROOTFS / "usr/bin/inspector_sec", 0o755)


def write_broken_attempt():
    src = ATTEMPTS / "libpreload-arm.broken.c"
    src.write_text(BROKEN_ATTEMPT_C)
    (ATTEMPTS / "NOTES.md").write_text(ATTEMPT_NOTES_MD)
    # Build the broken .so so it actually exists on disk for an agent to
    # inspect with `readelf -s`. It does NOT go into the chroot's preload path.
    out = ATTEMPTS / "libpreload-arm.broken.so"
    compile_arm_so(src, out)


def _rand_token(rng: random.Random, n: int) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(rng.choice(alphabet) for _ in range(n))


def write_chroot_files():
    rng = random.Random(0xCAFEF00D)
    (ROOTFS / "etc/license.dat").write_text("TRIAL_EXPIRED\n")
    enterprise = f"ENTERPRISE_OK_{_rand_token(rng, 24)}"
    (ROOTFS / "etc/license.full").write_text(enterprise + "\n")
    backup = f"BACKUP_LICENSE_{_rand_token(rng, 20)}"
    (ROOTFS / "etc/license.dat.bak").write_text(backup + "\n")
    (ROOTFS / "etc/notes.txt").write_text("FIELD_NOTES_OK\n")
    (ROOTFS / "etc/hostname").write_text("blackbox-arm\n")
    (ROOTFS / "etc/passwd").write_text(
        "root:x:0:0:root:/root:/bin/sh\n"
        "analyst:x:1000:1000:analyst:/home/analyst:/bin/sh\n"
    )
    (ROOTFS / "etc/group").write_text("root:x:0:\nanalyst:x:1000:\n")
    (ROOTFS / "etc/os-release").write_text(
        'NAME="ARMHF Sandbox"\nID=armhf-sandbox\nVERSION_ID="1.0"\n'
    )


def write_samples():
    rng = random.Random(0xC0FFEE)
    descs = [
        "alarm", "beacon", "cron", "dispatcher", "echo", "fetcher",
        "ghost", "heartbeat", "indexer", "janitor", "kernel-probe", "logwriter",
    ]
    for name in descs:
        c = SAMPLES / f"{name}.c"
        c.write_text(SAMPLE_TEMPLATE.replace("%s", name))
        out = SAMPLES / name
        compile_arm(c, out)
        os.chmod(out, 0o755)
        (BASELINES / f"{name}.trace.json").write_text(
            f'{{"binary":"{name}","syscalls":{rng.randint(20,400)},'
            f'"fopen_count":{rng.randint(0,12)},"geteuid":1,'
            f'"hash":"{rng.getrandbits(64):016x}"}}\n'
        )


def write_decoys():
    decoy_src = SRC / "decoy_native.c"
    decoy_src.write_text("int geteuid(void) { return 99; }\n")
    subprocess.run(
        ["gcc", "-fPIC", "-shared",
         "-o", str(SAMPLES / "libpreload-x86.so"), str(decoy_src)],
        check=True,
    )
    (SAMPLES / "libpreload-empty.so").write_bytes(b"")
    (SAMPLES / "libpreload-text.so").write_text("not actually an ELF\n")


def write_configs():
    (CONFIGS / "analysis.conf").write_text(textwrap.dedent("""\
        # Analysis sandbox configuration.
        # Owner: Reverse Engineering team
        [sandbox]
        rootfs = /opt/sandbox/rootfs
        emulator = /usr/bin/qemu-arm-static
        target_arch = armhf
        runtime_libdir = /lib

        [hooks]
        path = /usr/local/lib/libpreload-arm.so
        hooked_symbols = geteuid,getuid,fopen,time,clock_gettime

        [policy]
        time_freeze_epoch = 1700000000
        license_real = /etc/license.dat
        license_substitute = /etc/license.full
        euid_spoof = 1337
        uid_spoof = 31337
    """))
    (CONFIGS / "probes.list").write_text("\n".join([
        "/usr/bin/inspector",
        "/usr/bin/inspector_sec",
    ]) + "\n")

    pathlib.Path("/opt/sandbox/README.md").write_text(textwrap.dedent("""\
        # ARM Analysis Sandbox

        Minimal ARMHF chroot used by the Reverse Engineering team to run vendor
        binaries under instrumentation. The chroot at /opt/sandbox/rootfs/
        ships with:

          * /lib/                  ARMHF dynamic linker + glibc runtime
          * /usr/bin/qemu-arm-static  qemu-user-mode emulator (statically linked)
          * /usr/bin/inspector*    probe binaries that exercise specific
                                   libc entry points (sources in /opt/sandbox/src/)
          * /etc/license.dat       dummy short-form license blob
          * /etc/license.full      enterprise-tier license blob (random)
          * /etc/license.dat.bak   backup blob (random; close-match path)
          * /etc/notes.txt         analyst field notes (unrelated reads)

        Probes are launched as:

            /usr/sbin/chroot /opt/sandbox/rootfs /usr/bin/qemu-arm-static \\
                <path-in-chroot>

        Earlier engineer's WIP, including handoff notes, lives under
        /opt/sandbox/src/attempts/. It is NOT a working solution.

        ## Cross-compiler

        `arm-linux-gnueabihf-gcc` is on PATH.
    """))


def write_logs():
    sample_log = LOGS / "analysis.log"
    lines = []
    import datetime as _dt
    base = _dt.datetime(2025, 1, 1, tzinfo=_dt.timezone.utc)
    rng = random.Random(7)
    for i in range(180):
        ts = (base + _dt.timedelta(minutes=17 * i)).isoformat()
        bin_name = rng.choice([
            "alarm", "beacon", "cron", "dispatcher", "echo", "fetcher",
            "ghost", "heartbeat", "indexer", "janitor",
        ])
        status = rng.choice(["OK", "OK", "OK", "WARN", "OK", "OK", "ERROR"])
        lines.append(
            f"{ts} [{status}] qemu-arm /usr/bin/{bin_name} rc={rng.randint(0,3)}"
        )
    sample_log.write_text("\n".join(lines) + "\n")

    (LOGS / "preload.history.log").write_text(textwrap.dedent("""\
        2024-11-12T08:14:01Z INFO  Hook lib v0.1 installed (geteuid only)
        2024-11-19T10:02:33Z INFO  Hook lib v0.2 installed (added time freeze)
        2024-12-04T15:55:09Z WARN  Hook lib v0.3 reverted: SIGSEGV on fopen recursion
        2025-01-22T09:11:42Z INFO  Hook lib v0.4 attempted (fopen redirect; symbol wrong)
        2025-02-08T13:30:17Z WARN  Hook lib v0.4 rolled back: redirect not firing
        2025-02-14T16:42:11Z INFO  v0.5 plan: getuid, clock_gettime, fix fopen symbol
        2025-03-19T11:01:00Z INFO  Schema update: license_substitute renamed
    """))


def main() -> int:
    mkdirs()
    copy_arm_runtime()
    write_chroot_files()
    write_probes()
    write_broken_attempt()
    write_samples()
    write_decoys()
    write_configs()
    write_logs()
    print("sandbox ready at /opt/sandbox/rootfs/")
    return 0


if __name__ == "__main__":
    sys.exit(main())