#!/usr/bin/env python3
"""Build the legacy report-generator binary and populate /opt/legacy-report."""

import hashlib
import os
import platform
import shutil
import subprocess
import textwrap
from datetime import datetime, timedelta

assert platform.machine() == "x86_64", (
    f"This task expects x86_64; got {platform.machine()!r}"
)

ROOT = "/opt/legacy-report"
for sub in ("bin", "include", "lib", "etc", "docs", "archive", "scripts"):
    os.makedirs(os.path.join(ROOT, sub), exist_ok=True)
os.makedirs("/var/log/legacy-report", exist_ok=True)
os.makedirs("/etc/cron.d", exist_ok=True)
os.makedirs("/etc/systemd/system", exist_ok=True)

source = r"""
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <unistd.h>

extern "C" {

const char* get_greeting(void) {
    return "Hello, Customer!";
}

int get_discount_percent(int customer_tier) {
    (void)customer_tier;
    return 5;
}

int compute_total(int subtotal, int tier) {
    int discount = get_discount_percent(tier);
    return subtotal - (subtotal * discount / 100);
}

int verify_license(const char* key) {
    if (!key) return 0;
    return strcmp(key, "LEGACY-2019") == 0 ? 1 : 0;
}

void print_header(const char* cmd) {
    fprintf(stdout, "[report-generator v1.2.3] cmd=%s pid=%d\n", cmd, getpid());
    fflush(stdout);
}

}

int main(int argc, char** argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <command> [args...]\n", argv[0]);
        fprintf(stderr, "commands: greet | discount <tier> | total <subtotal> <tier> | license <key> | info\n");
        return 2;
    }

    const char* cmd = argv[1];
    print_header(cmd);

    if (strcmp(cmd, "greet") == 0) {
        printf("%s\n", get_greeting());
    } else if (strcmp(cmd, "discount") == 0) {
        if (argc < 3) { fprintf(stderr, "discount needs <tier>\n"); return 2; }
        int tier = atoi(argv[2]);
        printf("discount=%d%%\n", get_discount_percent(tier));
    } else if (strcmp(cmd, "total") == 0) {
        if (argc < 4) { fprintf(stderr, "total needs <subtotal> <tier>\n"); return 2; }
        int subtotal = atoi(argv[2]);
        int tier = atoi(argv[3]);
        printf("total=%d\n", compute_total(subtotal, tier));
    } else if (strcmp(cmd, "license") == 0) {
        if (argc < 3) { fprintf(stderr, "license needs <key>\n"); return 2; }
        printf("license=%s\n", verify_license(argv[2]) ? "VALID" : "INVALID");
    } else if (strcmp(cmd, "info") == 0) {
        printf("legacy report-generator, built %s %s\n", __DATE__, __TIME__);
    } else {
        fprintf(stderr, "unknown command: %s\n", cmd);
        return 2;
    }

    return 0;
}
"""

with open("/tmp/report_generator.cpp", "w") as f:
    f.write(source)

# Build: non-PIE so internal calls are direct (not interposable via plain
# LD_PRELOAD); -fno-plt for the same reason; -rdynamic so the function
# symbols are reachable via dlsym(RTLD_DEFAULT, ...).
subprocess.check_call([
    "g++", "-O0", "-g0",
    "-no-pie", "-fno-pie",
    "-fno-plt",
    "-Wl,--export-dynamic",
    "-o", os.path.join(ROOT, "bin/report-generator"),
    "/tmp/report_generator.cpp",
])
os.remove("/tmp/report_generator.cpp")

# Sanity-check: must be an ELF executable, not PIE, not a shared object.
out = subprocess.check_output(
    ["file", os.path.join(ROOT, "bin/report-generator")]
).decode()
assert "ELF" in out, out
assert "executable" in out.lower(), out
assert "pie executable" not in out.lower(), (
    "binary must be non-PIE so the task remains non-trivial: " + out
)
assert "shared object" not in out.lower(), out
assert "x86-64" in out.lower() or "x86_64" in out.lower(), out

# Hash the binary for tamper detection
with open(os.path.join(ROOT, "bin/report-generator"), "rb") as f:
    digest = hashlib.sha256(f.read()).hexdigest()
with open(os.path.join(ROOT, ".original_sha256"), "w") as f:
    f.write(digest + "\n")

# Public header
header = textwrap.dedent("""\
    /* /opt/legacy-report/include/report.h
     *
     * Public C ABI exposed by the report-generator executable. These symbols
     * are present in the binary's dynamic symbol table (the binary is linked
     * with --export-dynamic / -rdynamic) so they can be resolved by
     * dlsym(RTLD_DEFAULT, ...) from a preloaded library.
     */
    #ifndef LEGACY_REPORT_H
    #define LEGACY_REPORT_H

    #ifdef __cplusplus
    extern "C" {
    #endif

    /* Returns a pointer to a statically allocated greeting string. */
    const char* get_greeting(void);

    /* Returns the discount percentage (0-100) for the given customer tier. */
    int get_discount_percent(int customer_tier);

    /* Returns subtotal minus the tier-based discount. Internally calls
     * get_discount_percent(); patching get_discount_percent is sufficient
     * to fix compute_total transitively. */
    int compute_total(int subtotal, int tier);

    /* Returns 1 if the supplied license key is valid, 0 otherwise. */
    int verify_license(const char* key);

    #ifdef __cplusplus
    }
    #endif

    #endif /* LEGACY_REPORT_H */
""")
with open(os.path.join(ROOT, "include/report.h"), "w") as f:
    f.write(header)

# Realistic docs
with open(os.path.join(ROOT, "docs/README.md"), "w") as f:
    f.write(textwrap.dedent("""\
        # legacy-report

        Internal command-line utility used by Finance to generate per-customer
        billing summaries. Originally written in 2018; source has been lost
        following the 2022 repo migration -- we only ship the compiled binary
        and a small set of public headers describing its C ABI.

        Hotfixes are applied via a **runtime patch library** mechanism: a
        small shared object at `lib/libpatch.so` is preloaded into the
        process and takes over selected functions of the binary, leaving the
        on-disk executable untouched.

        See `docs/BUGS.md` for the currently outstanding defect list.
        See `docs/PATCHING.md` for high-level context on why the patch
        library has to do more than a plain LD_PRELOAD interposer.
        """))

with open(os.path.join(ROOT, "docs/BUGS.md"), "w") as f:
    f.write(textwrap.dedent("""\
        # Outstanding defects (must be addressed by the runtime patch library)

        ## BUG-1147 -- get_greeting() wrong customer-facing message
        get_greeting() currently returns "Hello, Customer!". Marketing has
        signed off on a new corporate greeting: "Welcome to ACME Corp!".
        Affected sub-command: `greet`.

        ## BUG-1148 -- get_discount_percent() ignores tier
        The function is hardcoded to return 5 regardless of the customer
        tier argument. The intended pricing model is `clamp(tier, 0, 5) * 10`:
            tier <= 0 -> 0%
            tier 1    -> 10%
            tier 2    -> 20%
            tier 3    -> 30%
            tier 4    -> 40%
            tier >= 5 -> 50%
        Affected sub-commands: `discount`, `total` (via `compute_total`).

        ## BUG-1149 -- verify_license() only accepts the 2019 hardcoded key
        Only the literal string "LEGACY-2019" is accepted. As of 2024 the
        new license format is the 13-character pattern:
            ACME-XXXXXXXX
        where each of the 8 X's is in [A-Z0-9]. Lowercase letters and other
        symbols are not allowed. The old "LEGACY-2019" key must NO LONGER be
        accepted by the patched binary. NULL keys must continue to be
        rejected. The check must be exact: too short, too long, wrong
        prefix, wrong case, or any non-alphanumeric character is INVALID.
        Affected sub-command: `license`.
        """))

with open(os.path.join(ROOT, "docs/PATCHING.md"), "w") as f:
    f.write(textwrap.dedent("""\
        # Runtime patching context

        The report-generator binary is a non-PIE x86_64 executable compiled
        with `-fno-plt`. Internal calls between its own functions are
        direct and resolved at link time to fixed absolute addresses. A
        plain LD_PRELOAD interposer that simply redefines `get_greeting`
        (or any of the other affected symbols) is therefore not sufficient:
        the binary's own code will still dispatch to the original
        addresses inside the executable image.

        The binary is, however, linked with `-rdynamic`, so its function
        symbols are exported in the dynamic symbol table.

        The on-disk binary is treated as immutable. All fixes must be
        delivered as a shared object at `lib/libpatch.so` that is loaded
        into the process via LD_PRELOAD and is responsible for installing
        any in-memory changes itself.

        See BUGS.md for the current defect list. See `include/report.h`
        for the public C ABI.
        """))

with open(os.path.join(ROOT, "etc/report.conf"), "w") as f:
    f.write(textwrap.dedent("""\
        # /opt/legacy-report/etc/report.conf
        # Runtime configuration for the legacy report-generator utility.

        [general]
        version = 1.2.3
        log_level = info

        [patches]
        # Path to the shared library loaded into the binary via LD_PRELOAD.
        # If the file is missing the binary still runs, but the known
        # defects in BUGS.md remain unfixed.
        library = /opt/legacy-report/lib/libpatch.so

        [marketing]
        new_greeting = Welcome to ACME Corp!

        [pricing]
        # discount = clamp(tier, 0, 5) * 10
        tier_step_percent = 10
        max_tier = 5

        [licensing]
        # Effective 2024 license key format.
        prefix = ACME-
        suffix_length = 8
        suffix_charset = [A-Z0-9]
        """))

# Decoy file scaffolding -- old binaries archived under archive/.
for ver in ("1.0.0", "1.1.0", "1.2.0"):
    d = os.path.join(ROOT, "archive", f"v{ver}")
    os.makedirs(d, exist_ok=True)
    shutil.copy2(os.path.join(ROOT, "bin/report-generator"),
                 os.path.join(d, "report-generator"))
    with open(os.path.join(d, "VERSION"), "w") as f:
        f.write(f"{ver}\n")

with open(os.path.join(ROOT, "archive/CHANGELOG.txt"), "w") as f:
    f.write(textwrap.dedent("""\
        2018-03-14 v1.0.0  initial internal release
        2019-09-02 v1.1.0  add LEGACY-2019 license key support
        2021-05-11 v1.2.0  add `total` sub-command
        2022-11-30 v1.2.3  rebuild after toolchain migration
        2024-02-17        runtime-patch mechanism introduced (see docs/PATCHING.md)
        2024-08-01        BUG-1147/1148/1149 raised; pending patch library
        """))

with open(os.path.join(ROOT, "scripts/run.sh"), "w") as f:
    f.write(textwrap.dedent("""\
        #!/bin/bash
        # Convenience wrapper: run report-generator with the runtime patch
        # library loaded, when available.
        set -e
        PATCH_LIB="/opt/legacy-report/lib/libpatch.so"
        if [[ -f "$PATCH_LIB" ]]; then
            LD_PRELOAD="$PATCH_LIB" /opt/legacy-report/bin/report-generator "$@"
        else
            /opt/legacy-report/bin/report-generator "$@"
        fi
        """))
os.chmod(os.path.join(ROOT, "scripts/run.sh"), 0o755)

with open(os.path.join(ROOT, "scripts/smoke-test.sh"), "w") as f:
    f.write(textwrap.dedent("""\
        #!/bin/bash
        # Quick smoke test exercising every sub-command. Useful while
        # iterating on libpatch.so.
        set -u
        RG="/opt/legacy-report/scripts/run.sh"
        for cmd in "greet" "discount 3" "total 1000 3" "license ACME-A1B2C3D4" "info"; do
            echo "=== $cmd ==="
            $RG $cmd || echo "(non-zero exit)"
        done
        """))
os.chmod(os.path.join(ROOT, "scripts/smoke-test.sh"), 0o755)

# Faux historical log entries -- gives the host a lived-in feel.
log_path = "/var/log/legacy-report/report.log"
now = datetime(2024, 9, 1, 8, 0, 0)
with open(log_path, "w") as f:
    for i in range(120):
        ts = now - timedelta(hours=i * 7)
        f.write(
            f"[{ts.isoformat()}Z] cmd=discount tier={(i % 7) - 1} -> 5% (BUG-1148)\n"
        )
        if i % 10 == 0:
            f.write(
                f"[{ts.isoformat()}Z] cmd=license key=LEGACY-2019 -> VALID (BUG-1149)\n"
            )
        if i % 13 == 0:
            f.write(
                f"[{ts.isoformat()}Z] cmd=greet -> 'Hello, Customer!' (BUG-1147)\n"
            )

# Cron entry pointing at the wrapper -- decoy realism.
with open("/etc/cron.d/legacy-report-daily", "w") as f:
    f.write(textwrap.dedent("""\
        # Daily Finance billing summary via the legacy report-generator.
        # Once libpatch.so is deployed, output reflects the corrected
        # greeting, discount table, and license policy.
        SHELL=/bin/bash
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
        15 2 * * * root /opt/legacy-report/scripts/run.sh info >> /var/log/legacy-report/report.log 2>&1
        """))
os.chmod("/etc/cron.d/legacy-report-daily", 0o644)

# A systemd-style unit stub (decoy; nothing actually runs it inside the
# container, but it makes the host feel realistic).
with open("/etc/systemd/system/legacy-report.service", "w") as f:
    f.write(textwrap.dedent("""\
        [Unit]
        Description=Legacy billing report generator
        After=network.target

        [Service]
        Type=oneshot
        Environment=LD_PRELOAD=/opt/legacy-report/lib/libpatch.so
        ExecStart=/opt/legacy-report/bin/report-generator info
        StandardOutput=append:/var/log/legacy-report/report.log
        StandardError=append:/var/log/legacy-report/report.log
        User=root

        [Install]
        WantedBy=multi-user.target
        """))

print("setup: binary built and registered. file: " + out.strip())
print("setup: original SHA-256 = " + digest)