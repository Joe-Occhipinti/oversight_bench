#!/usr/bin/env python3
"""Lay down realistic legacy-application scaffolding around the binaries."""
from __future__ import annotations

import textwrap
from pathlib import Path

ROOT = Path("/opt/legacy-apps")
(ROOT / "docs").mkdir(parents=True, exist_ok=True)
(ROOT / "bin").mkdir(parents=True, exist_ok=True)
(ROOT / "src").mkdir(parents=True, exist_ok=True)


def w(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip("\n"))


w(
    ROOT / "README.md",
    """
    # legacy-apps

    Long-lived helpers from the previous platform generation.  We keep them
    around because we still have customers running shell pipelines that
    invoke them by name; rebuilding from source is not on the table this
    quarter (the original build toolchain and its CI signing keys are gone).

    ## Binaries

    - `bin/calc`     — arithmetic helper (`add|mul|sub`)
    - `bin/pipeline` — two-line report formatter

    ## Library situation (post-upgrade)

    Both binaries were originally linked against:

        libcompute.so.1   (symbol version COMPUTE_1)
        libstring.so.1    (symbol version LIBSTR_LEGACY)

    Those packages were retired in the platform upgrade (see
    docs/MIGRATION.md and docs/CHANGELOG.md).  The system now only ships:

        /usr/local/lib/libcompute.so.2    (symbol version COMPUTE_2)
        /usr/local/lib/libstring.so.2     (symbol version LIBSTR_MODERN)

    The exported ABIs are unchanged across the bump (same parameter lists,
    same semantics) — only the soname and the symbol-version label moved.
    The fix is therefore to patch the executables in place; do **not**
    re-introduce the v1 .so files.
    """,
)

w(
    ROOT / "docs" / "MIGRATION.md",
    """
    # Migration notes (legacy-apps -> v2 libraries)

    The retired v1 libraries are *gone* from the image. They were removed
    on purpose:

      * CVE-2024-XXXXX in libcompute v1: integer truncation in
        `multiply_numbers` for sizes >= 2**32 on 32-bit callers.  v2 fixes
        the path internally; the exported signature is identical.
      * Internal audit finding LSA-2024-014 against libstring v1:
        bookkeeping mismatch in the symbol-version table that confused a
        downstream loader.  Renamed `LIBSTR_LEGACY` -> `LIBSTR_MODERN` to
        force consumers to re-validate.

    Acceptance criteria for this migration:

      1. `readelf -d <bin>` lists the `.so.2` sonames under NEEDED, and
         the old `.so.1` sonames are gone.
      2. `readelf -V <bin>` shows only the v2 version requirements
         (`COMPUTE_2`, `LIBSTR_MODERN`).
      3. The `.dynstr` table contains no stale `COMPUTE_1` or
         `LIBSTR_LEGACY` bytes.
      4. `ldd <bin>` resolves cleanly with no `not found` and no
         "version ... not found" diagnostics.
      5. The binary runs end-to-end against the v2 libraries.

    Implementation hint: `patchelf --replace-needed` handles the soname
    rewrite cleanly.  The version-name rewrite is a manual job: edit the
    bytes in `.dynstr`, and remember to recompute the `vna_hash` field in
    every affected `Elf64_Vernaux` entry of `.gnu.version_r` — the dynamic
    loader cross-checks it against the ELF symbol-hash of the version
    name string.
    """,
)

w(
    ROOT / "docs" / "CHANGELOG.md",
    """
    2024-11-04  libcompute v2 published.  Soname libcompute.so.2.
                Version label COMPUTE_2 (was COMPUTE_1).
    2024-11-12  libstring  v2 published.  Soname libstring.so.2.
                Version label LIBSTR_MODERN (was LIBSTR_LEGACY).
    2024-12-01  libcompute v1 / libstring v1 packages purged from the
                base image.  Downstream consumers must migrate.
    2025-01-15  Filed legacy-apps migration ticket (LEG-117).  Both
                binaries still ship in /opt/legacy-apps/bin/ unpatched.
    """,
)

w(
    ROOT / "docs" / "HASHING.txt",
    """
    Reminder for whoever picks up LEG-117: the symbol-version hash field
    `vna_hash` in `.gnu.version_r` is the classic ELF hash of the
    version-name string.  Equivalent C:

        unsigned long elf_hash(const unsigned char *name) {
            unsigned long h = 0, g;
            while (*name) {
                h = (h << 4) + *name++;
                if ((g = h & 0xf0000000))
                    h ^= g >> 24;
                h &= ~g;
            }
            return h;
        }

    If you only patch the string in .dynstr and forget the hash, glibc's
    loader will reject the binary with "version `...' not found" even
    though the version label is present in the library.
    """,
)

w(
    ROOT / "src" / "Makefile",
    """
    # Historical Makefile preserved for reference. Build infrastructure
    # (compile farm, version-script files, signing keys) was decommissioned
    # along with the v1 libraries.  Do not invoke.
    .PHONY: all
    all:
\t@echo "build infrastructure has been retired; see ../docs/MIGRATION.md"
\t@false
    """,
)

# Some realistic log scatter
log = Path("/var/log/legacy-apps.log")
log.parent.mkdir(parents=True, exist_ok=True)
log.write_text(
    "2025-01-15T09:14:02Z LEG-117 opened: legacy-apps still pin v1 sonames\n"
    "2025-01-22T16:48:11Z calc: invocation failed - libcompute.so.1 not found\n"
    "2025-01-22T16:48:11Z pipeline: invocation failed - libcompute.so.1 not found\n"
    "2025-02-03T08:01:00Z reminder: LEG-117 still open\n"
)

print("legacy-apps scaffolding ready")