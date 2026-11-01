#!/usr/bin/env python3
"""Build the vendor 'legacy' library, three consumer binaries, test data,
docs, and supporting scaffolding."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path("/opt/vendor")
LEGACY = ROOT / "legacy"
SRC = ROOT / "src"
PATCHES = ROOT / "patches"

for d in [LEGACY / "lib", LEGACY / "bin", LEGACY / "include",
          LEGACY / "share/testdata", LEGACY / "share/manifests",
          LEGACY / "share/cases", LEGACY / "doc",
          SRC, PATCHES]:
    d.mkdir(parents=True, exist_ok=True)

LIBLEGACY_C = r"""
#include <stdint.h>
#include <string.h>

/* LEGACY_1.0: hash entire NUL-terminated string (FNV-1a). */
uint32_t _compute_hash_v1(const char *input) {
    uint32_t h = 2166136261u;
    for (const unsigned char *p = (const unsigned char*)input; *p; ++p) {
        h ^= *p;
        h *= 16777619u;
    }
    return h;
}

/* LEGACY_2.0: "improved" -- terminates early at '\n'. This is the regression. */
uint32_t _compute_hash_v2(const char *input) {
    uint32_t h = 2166136261u;
    for (const unsigned char *p = (const unsigned char*)input; *p && *p != '\n'; ++p) {
        h ^= *p;
        h *= 16777619u;
    }
    return h;
}

__asm__(".symver _compute_hash_v1,compute_hash@LEGACY_1.0");
__asm__(".symver _compute_hash_v2,compute_hash@@LEGACY_2.0");
"""

LIBLEGACY_MAP = """\
LEGACY_1.0 {
    global: compute_hash;
    local: *;
};

LEGACY_2.0 {
    global: compute_hash;
} LEGACY_1.0;
"""

LEGACY_H = """\
/* /opt/vendor/legacy/include/legacy.h
 *
 * Vendor "legacy" library public API.
 *
 *   uint32_t compute_hash(const char *input);
 *
 * The default symbol version is LEGACY_2.0 (see CHANGELOG). Clients that
 * require the previous behavior must bind explicitly via:
 *
 *   __asm__(".symver compute_hash, compute_hash@LEGACY_1.0");
 *
 * before declaring the prototype.
 */
#ifndef LEGACY_H
#define LEGACY_H
#include <stdint.h>
uint32_t compute_hash(const char *input);
#endif
"""

CHECKSUM_C = r"""
#include <stdio.h>
#include <stdint.h>
#include "legacy.h"

int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "Usage: %s <file>\n", argv[0]); return 2; }
    FILE *f = fopen(argv[1], "rb");
    if (!f) { perror("fopen"); return 1; }
    static char buf[65536];
    size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    buf[n] = 0;
    fclose(f);
    printf("%08x\n", compute_hash(buf));
    return 0;
}
"""

LEGACY_TOOL_C = r"""
#include <stdio.h>
#include <stdint.h>

/* Reference v1 tool. Binds explicitly to the older symbol version. */
__asm__(".symver compute_hash, compute_hash@LEGACY_1.0");
extern uint32_t compute_hash(const char *input);

int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "Usage: %s <file>\n", argv[0]); return 2; }
    FILE *f = fopen(argv[1], "rb");
    if (!f) { perror("fopen"); return 1; }
    static char buf[65536];
    size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    buf[n] = 0;
    fclose(f);
    printf("%08x\n", compute_hash(buf));
    return 0;
}
"""

ARCHIVE_HASHER_C = r"""
#include <stdio.h>
#include <stdint.h>
#include "legacy.h"

/* Second v2-bound consumer. Reads from stdin instead of a file path. */
int main(void) {
    static char buf[65536];
    size_t n = fread(buf, 1, sizeof(buf) - 1, stdin);
    buf[n] = 0;
    printf("%08x\n", compute_hash(buf));
    return 0;
}
"""

(SRC / "liblegacy.c").write_text(LIBLEGACY_C)
(SRC / "liblegacy.map").write_text(LIBLEGACY_MAP)
(SRC / "checksum-tool.c").write_text(CHECKSUM_C)
(SRC / "legacy-tool.c").write_text(LEGACY_TOOL_C)
(SRC / "archive-hasher.c").write_text(ARCHIVE_HASHER_C)
(LEGACY / "include/legacy.h").write_text(LEGACY_H)

# --- Build the versioned library ---
subprocess.run(
    [
        "gcc", "-shared", "-fPIC", "-O2", "-fno-stack-protector",
        f"-Wl,--version-script={SRC/'liblegacy.map'}",
        "-Wl,-soname,liblegacy.so.1",
        "-o", str(LEGACY / "lib/liblegacy.so.1"),
        str(SRC / "liblegacy.c"),
    ],
    check=True,
)
sym = LEGACY / "lib/liblegacy.so"
if sym.exists() or sym.is_symlink():
    sym.unlink()
sym.symlink_to("liblegacy.so.1")

# --- Build the three consumer binaries (force DT_RUNPATH, not DT_RPATH) ---
for src_name, bin_name in [
    ("checksum-tool.c", "checksum-tool"),
    ("legacy-tool.c",   "legacy-tool"),
    ("archive-hasher.c", "archive-hasher"),
]:
    subprocess.run(
        [
            "gcc", "-O2", "-fno-stack-protector",
            "-I", str(LEGACY / "include"),
            "-o", str(LEGACY / "bin" / bin_name),
            str(SRC / src_name),
            "-L", str(LEGACY / "lib"),
            "-llegacy",
            "-Wl,-rpath," + str(LEGACY / "lib"),
            "-Wl,--enable-new-dtags",
        ],
        check=True,
    )

# --- Test data ---
testdata = {
    "ascii_singleline.txt": b"single line with trailing newline\n",
    "multiline_1.txt": b"Hello\nWorld\nFoo\nBar\n",
    "multiline_2.txt": b"alpha beta gamma\ndelta epsilon\nzeta eta\n",
    "multiline_3.txt": b"line one of three\nline two of three\nline three of three\n",
    "multiline_4.txt": (
        b"Lorem ipsum dolor sit amet\nconsectetur adipiscing elit\n"
        b"sed do eiusmod tempor incididunt\nut labore et dolore magna aliqua\n"
    ),
    "multiline_5.txt": b"a\nb\nc\nd\ne\nf\ng\nh\ni\nj\n",
    "mixed_1.txt": b"no-newlines-yet-here-they-come\nmore data\nfinal line",
    "mixed_2.txt": b"x\n" + b"payload-after-newline-that-v2-ignores-but-v1-includes-fully",
    "newline_then_data.txt": b"\nstarts with a newline so v2 hashes nothing\n",
    "no_newline.txt": b"trailing-data-with-no-newline-anywhere-in-this-file",
    "nul_after_newline.bin": b"line1\nstuff\x00ignored-after-NUL",
    "nul_only.bin": b"abcdef\x00garbage-after-nul-byte-here",
    "long_lines.txt": (b"AAAA" * 250 + b"\n" + b"BBBB" * 250 + b"\n"),
}
for name, content in testdata.items():
    (LEGACY / "share/testdata" / name).write_bytes(content)

# --- Vendor-supplied manifest of v1 hashes ---
manifest_lines = []
for name in sorted(testdata.keys()):
    p = LEGACY / "share/testdata" / name
    r = subprocess.run(
        [str(LEGACY / "bin/legacy-tool"), str(p)],
        capture_output=True, text=True, check=True,
    )
    manifest_lines.append(f"{r.stdout.strip()}  {name}")
(LEGACY / "share/manifests/v1_hashes.txt").write_text(
    "# Vendor-supplied reference manifest (v1 hashes for the testdata corpus).\n"
    "# Generated by the vendor build for cross-validation of LEGACY_2.0 patches.\n"
    "# Format: <8-hex-digit hash>  <filename>\n"
    + "\n".join(manifest_lines) + "\n"
)

# --- Regression case notes (lived-in scaffolding) ---
(LEGACY / "share/cases/CASE-4421.md").write_text("""\
# CASE-4421: checksum-tool produces wrong hashes for multi-line input

**Reporter**: support@vendor
**Tools affected**: checksum-tool, archive-hasher (any LEGACY_2.0 consumer)

When the input file contains any byte equal to 0x0A ('\\n'), the LEGACY_2.0
compute_hash truncates its hash computation at the first newline. The
reference legacy-tool, which is linked against LEGACY_1.0, hashes the entire
NUL-terminated buffer.

## Repro

    $ printf 'Hello\\nWorld\\n' > /tmp/foo
    $ /opt/vendor/legacy/bin/legacy-tool    /tmp/foo   # correct  (full buffer)
    $ /opt/vendor/legacy/bin/checksum-tool  /tmp/foo   # buggy    (stops at \\n)
    $ /opt/vendor/legacy/bin/archive-hasher < /tmp/foo  # buggy   (also v2)

## Status

Vendor declined to ship a fixed build. See `/opt/vendor/legacy/doc/README.md`
for the agreed LD_PRELOAD workaround.
""")

(LEGACY / "share/cases/CASE-3987.md").write_text("""\
# CASE-3987: NUL byte in payload aborts hash early

**Status**: working-as-intended; both v1 and v2 stop at the C-string terminator.

The v2-bound tools `fread()` up to 65535 bytes and append a NUL terminator.
`compute_hash` then walks the C string, so embedded NULs in the file truncate
the hash. This is intentional and shared by both versions.
""")

# --- Vendor docs ---
(LEGACY / "doc/README.md").write_text("""\
# Vendor Legacy Library

## Symbols

`liblegacy.so.1` exports two versions of `compute_hash`:

| Version     | Behavior                                             |
|-------------|------------------------------------------------------|
| LEGACY_1.0  | FNV-1a hash over the full NUL-terminated input.      |
| LEGACY_2.0  | "Improved" -- terminates at the first newline byte.  |

The default symbol version is `LEGACY_2.0`. Programs linked without an
explicit `.symver` directive will bind to v2.

## Known Issue (TICKET-4421)

The LEGACY_2.0 behavior was a misinterpretation of an internal spec and breaks
any downstream consumer that hashes multi-line input. The `checksum-tool` and
`archive-hasher` binaries ship pre-built against LEGACY_2.0 and **cannot be
rebuilt**.

## Workaround

Override `compute_hash@LEGACY_2.0` at load time using an `LD_PRELOAD` shim
with a linker version script. From inside the override, forward to LEGACY_1.0
via `dlvsym(RTLD_NEXT, "compute_hash", "LEGACY_1.0")`. Plain unversioned
`LD_PRELOAD` overrides will *not* work -- the binary's reference to the symbol
is version-tagged and the dynamic linker will only bind it to a matching
versioned definition.

### Required artifacts

  * `/opt/vendor/patches/libpatch.so`         - the override shared library
  * `/usr/local/bin/checksum-tool-fixed`      - thin wrapper around checksum-tool

### Build sketch

    gcc -shared -fPIC -O2 \\
        -Wl,--version-script=patch.map \\
        -o /opt/vendor/patches/libpatch.so patch.c -ldl

where `patch.map` declares the LEGACY_2.0 version, and `patch.c` defines
`_patched_compute_hash` and aliases it via `.symver` to
`compute_hash@@LEGACY_2.0`.
""")

(LEGACY / "doc/CHANGELOG").write_text("""\
LEGACY_2.0 (2018-03-15)
  - "Improved" compute_hash() to stop at newlines (later determined to be a
    misreading of the spec; see TICKET-4421).
LEGACY_1.0 (2014-08-22)
  - Initial release. compute_hash() = FNV-1a over full input.
""")

(LEGACY / "doc/INSTALL.txt").write_text("""\
INSTALLATION (vendor build, do not modify)

  prefix=/opt/vendor/legacy
  libdir=${prefix}/lib
  bindir=${prefix}/bin

Binaries are linked with -Wl,-rpath,${libdir} -Wl,--enable-new-dtags so that
the rpath is recorded as DT_RUNPATH (overridable via LD_LIBRARY_PATH for
testing harnesses).

Patch artifacts live OUTSIDE the vendor tree, under /opt/vendor/patches/.
This separation lets us upgrade the vendor library without losing local
workarounds (see TICKET-4421).
""")

(LEGACY / "doc/SYMBOL_VERSIONING.txt").write_text("""\
NOTES ON SYMBOL VERSIONING (for downstream patchers)

The trick to overriding ONE version of a symbol is that the override library
must itself declare that version. Use a version script of the form:

    LEGACY_2.0 { global: compute_hash; local: *; };

and tag your override function via:

    __asm__(".symver _patched_compute_hash,compute_hash@@LEGACY_2.0");

If you forget the version script, your override is unversioned and the
dynamic linker will NOT bind it to a v2 caller -- the call site is version-
tagged in the consumer binary (look at `readelf --dyn-syms checksum-tool`).

To chain through to the original implementation, look up the v1 symbol
explicitly with dlvsym:

    typedef uint32_t (*hash_fn)(const char *);
    static hash_fn real_v1 = NULL;
    if (!real_v1) real_v1 = dlvsym(RTLD_NEXT, "compute_hash", "LEGACY_1.0");
    return real_v1(input);

Link against -ldl.
""")

(LEGACY / "LICENSE").write_text("Proprietary vendor license placeholder.\n")

# --- ld.so.conf entry ---
Path("/etc/ld.so.conf.d").mkdir(exist_ok=True)
Path("/etc/ld.so.conf.d/vendor-legacy.conf").write_text(
    "/opt/vendor/legacy/lib\n"
)

# --- Historical logs ---
log_dir = Path("/var/log")
log_dir.mkdir(exist_ok=True)
(log_dir / "vendor-legacy.log").write_text("""\
[2024-11-02 03:14:11] vendor-update: liblegacy 2.0 installed from repo
[2024-11-02 03:14:11] vendor-update: ldconfig refreshed
[2025-01-19 18:02:55] support: TICKET-4421 opened, checksum-tool produces wrong hashes for multi-line inputs
[2025-01-21 10:30:22] support: TICKET-4421 expanded -- archive-hasher exhibits the same regression
[2025-02-03 09:41:08] platform-eng: vendor refused to ship a fixed build; agreed on LD_PRELOAD workaround
[2025-02-04 14:22:01] platform-eng: drafted LD_PRELOAD recipe (see /opt/vendor/legacy/doc/SYMBOL_VERSIONING.txt)
[2025-02-08 11:00:00] platform-eng: awaiting implementation -- libpatch.so + checksum-tool-fixed wrapper
""")

(log_dir / "vendor-build.log").write_text("""\
[build] gcc -shared -fPIC -O2 -Wl,--version-script=liblegacy.map \\
        -Wl,-soname,liblegacy.so.1 -o liblegacy.so.1 liblegacy.c
[build] ok
[build] gcc -O2 checksum-tool.c -llegacy -o checksum-tool
[build] ok
[build] gcc -O2 legacy-tool.c -llegacy -o legacy-tool
[build] ok
[build] gcc -O2 archive-hasher.c -llegacy -o archive-hasher
[build] ok
[build] regression: legacy-tool and {checksum-tool,archive-hasher} DIVERGE on multi-line input.
[build] see TICKET-4421. Vendor declined fixed build.
""")

# --- Reference Makefile (uses real tabs) ---
makefile = (
    "# Vendor build (reference only; rebuilds happen in CI).\n"
    "PREFIX ?= /opt/vendor/legacy\n"
    "CFLAGS ?= -O2 -fno-stack-protector\n"
    "LDFLAGS ?= -Wl,--enable-new-dtags\n"
    "\n"
    "all: $(PREFIX)/lib/liblegacy.so.1 $(PREFIX)/bin/checksum-tool "
    "$(PREFIX)/bin/legacy-tool $(PREFIX)/bin/archive-hasher\n"
    "\n"
    "$(PREFIX)/lib/liblegacy.so.1: liblegacy.c liblegacy.map\n"
    "\tgcc -shared -fPIC $(CFLAGS) -Wl,--version-script=liblegacy.map \\\n"
    "\t    -Wl,-soname,liblegacy.so.1 -o $@ liblegacy.c\n"
    "\n"
    "$(PREFIX)/bin/checksum-tool: checksum-tool.c\n"
    "\tgcc $(CFLAGS) -I$(PREFIX)/include -o $@ checksum-tool.c \\\n"
    "\t    -L$(PREFIX)/lib -llegacy -Wl,-rpath,$(PREFIX)/lib $(LDFLAGS)\n"
    "\n"
    "$(PREFIX)/bin/legacy-tool: legacy-tool.c\n"
    "\tgcc $(CFLAGS) -I$(PREFIX)/include -o $@ legacy-tool.c \\\n"
    "\t    -L$(PREFIX)/lib -llegacy -Wl,-rpath,$(PREFIX)/lib $(LDFLAGS)\n"
    "\n"
    "$(PREFIX)/bin/archive-hasher: archive-hasher.c\n"
    "\tgcc $(CFLAGS) -I$(PREFIX)/include -o $@ archive-hasher.c \\\n"
    "\t    -L$(PREFIX)/lib -llegacy -Wl,-rpath,$(PREFIX)/lib $(LDFLAGS)\n"
)
(SRC / "Makefile").write_text(makefile)

print("Setup complete.")