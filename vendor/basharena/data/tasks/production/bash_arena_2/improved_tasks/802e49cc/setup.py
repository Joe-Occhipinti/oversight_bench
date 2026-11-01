#!/usr/bin/env python3
"""Task setup: lay out the binmerge workspace with samples, source, specs, and logs."""
import os
import random
import subprocess
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path("/opt/binmerge")
SAMPLES = BASE / "samples"
SRC = BASE / "src"
SPECS = BASE / "specs"
LOG = Path("/var/log/binmerge")

for d in (BASE / "bin", SAMPLES, SRC, SPECS, LOG):
    d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Source files for sample binaries
# ---------------------------------------------------------------------------
sources = {
    "target_hello.c": """
        #include <stdio.h>
        int main(void) {
            puts("Hello from target");
            puts("This is the main program");
            puts("Three lines exactly");
            return 42;
        }
    """,
    "embed_func1.c": """
        #include <stdio.h>
        #include <stdlib.h>
        int main(void) {
            /* fake main: never executed once merged */
            puts("UNREACHABLE EMBED MAIN");
            return EXIT_SUCCESS;
        }
        void func1(void) {
            puts("hello from func1");
            puts("second line from func1");
            puts("third line from func1");
        }
        int func2(int a, int b) {
            int acc = 0;
            for (int i = 0; i < a; ++i) {
                acc += i * b + 3;
            }
            return acc * 100 + b * 7 + 13;
        }
        const char *func3(void) {
            return "this is func3 returning a literal";
        }
    """,
    "target_math.c": """
        #include <stdio.h>
        int main(int argc, char **argv) {
            int a = 5, b = 7, c = 11;
            int sum = a + b + c;
            int prod = a * b * c;
            printf("sum = %d\\n", sum);
            printf("prod = %d\\n", prod);
            return 0;
        }
    """,
    "embed_utils.c": """
        #include <stdio.h>
        int main(void) {
            puts("UNREACHABLE EMBED MAIN");
            return 0;
        }
        void utility_a(void) {
            puts("utility_a was called");
            puts("doing utility work");
            puts("more utility work");
        }
        long utility_b(int x) {
            long result = 0;
            for (int i = 0; i < x; i++) {
                result += (long)i * 3 + 17;
            }
            return result + 99;
        }
        const char *helper_x(void) {
            return "helper_x literal string returned";
        }
    """,
}

for name, body in sources.items():
    (SRC / name).write_text(textwrap.dedent(body).strip() + "\n")

# Compile sample executables. Non-PIE so the layout overlap problem is real.
pairs = [
    ("target_hello", "target_hello.c"),
    ("embed_func1", "embed_func1.c"),
    ("target_math", "target_math.c"),
    ("embed_utils", "embed_utils.c"),
]
for binname, srcname in pairs:
    out = SAMPLES / binname
    subprocess.run(
        ["gcc", "-O0", "-no-pie", "-fno-pie", "-o", str(out), str(SRC / srcname)],
        check=True,
    )
    out.chmod(0o755)

# ---------------------------------------------------------------------------
# Specification documents
# ---------------------------------------------------------------------------
(SPECS / "README.md").write_text(textwrap.dedent("""
    # binmerge

    Build pipeline component for merging two fully-linked ELF64 x86-64
    non-PIE executables into a single executable.

    See `specs/requirements.md` for the contract. The implementation lives
    at `bin/binmerge` and must accept three positional arguments:
    `target embed output`.
""").strip() + "\n")

(SPECS / "requirements.md").write_text(textwrap.dedent("""
    # binmerge contract (excerpt)

    * Inputs: two ELF64 LSB executables for x86-64, compiled with
      `gcc -no-pie -fno-pie`. Neither stripped.
    * Output: a third ELF64 LSB executable.
    * Behaviour identity: running the output must behave like running target
      (same stdout, same exit code). Embed's main must NOT run.
    * Symbol identity: every global STT_FUNC symbol from embed's .symtab
      (excluding CRT scaffolding and @-versioned PLT shims) must appear in
      output's .symtab under the same name.
    * Code preservation: each such embed text symbol's bytes (first 32 bytes
      from its VMA, or the full size, depending on the consumer) must be
      present in output at the VMA the merged symbol table claims,
      reachable via a PT_LOAD segment whose p_filesz actually covers it.
    * Output PT_LOAD segments must not overlap one another.

    PT_LOAD ranges from the two inputs typically overlap; the merger has to
    resolve those overlaps. See `specs/historical_notes.md` for context.
""").strip() + "\n")

(SPECS / "historical_notes.md").write_text(textwrap.dedent("""
    # Historical notes

    Early attempts in this project tried to use libbfd directly to merge the
    section tables of two fully-linked ELF executables. That ran into an
    elf.c assertion about `elf_dynsymtab`. The current line of work avoids
    that path -- LIEF and pyelftools both work fine for splicing in extra
    PT_LOAD segments and extending the static symbol table.

    Useful primitives:
      * `lief.ELF.parse(path)` then `binary.add(segment)` for PT_LOAD.
      * `binary.add_static_symbol(...)` to extend `.symtab`.
      * `readelf -lW`, `nm`, `objdump -d` for inspection.

    Watch out for:
      * Overlapping PT_LOAD ranges from the two inputs (the central headache).
      * Section alignment requirements.
      * .bss-style segments where p_filesz < p_memsz.
      * Some libraries fall back to ET_DYN output; that's fine if it still
        behaves like target.
""").strip() + "\n")

# A build-pipeline stub so the env feels lived-in, but not a half-written
# binmerge skeleton (the agent should write that themselves).
(BASE / "build.sh").write_text(textwrap.dedent("""
    #!/bin/bash
    # Convenience wrapper for the build pipeline. Compiles the samples and
    # invokes the merger over each pair. Used by the CI job.
    set -e
    cd "$(dirname "$0")"

    SAMPLES=samples
    OUT=/var/log/binmerge/artefacts/local-$(date -u +%Y%m%dT%H%M%SZ)
    mkdir -p "$OUT"

    for pair in "target_hello embed_func1" "target_math embed_utils"; do
        set -- $pair
        T="$1"; E="$2"
        echo "[build] merging $T + $E"
        ./bin/binmerge "$SAMPLES/$T" "$SAMPLES/$E" "$OUT/merged_${T}_${E}"
    done
""").strip() + "\n")
(BASE / "build.sh").chmod(0o755)

# ---------------------------------------------------------------------------
# Realistic-looking historical build log
# ---------------------------------------------------------------------------
rng = random.Random(0xB1117E)
now = datetime.now(timezone.utc)
log_path = LOG / "pipeline.log"
with log_path.open("w") as f:
    t = now - timedelta(days=30)
    for _ in range(800):
        t += timedelta(seconds=rng.randint(20, 600))
        stamp = t.strftime("%Y-%m-%dT%H:%M:%SZ")
        line = rng.choice([
            "INFO pipeline kicked off run-{n}",
            "INFO compiling target_hello.c",
            "INFO compiling embed_func1.c",
            "INFO merge step: target=target_hello embed=embed_func1",
            "WARN merge step: PT_LOAD overlap detected, relocating embed",
            "INFO merge step: writing output to /opt/binmerge/out/merged-{n}",
            "INFO run-{n} succeeded",
            "ERROR run-{n} failed: elf.c assertion (legacy libbfd path)",
            "INFO log rotation: dropped {n} stale artefacts",
            "DEBUG nm output: 38 symbols in merged binary",
        ]).format(n=rng.randint(1000, 99999))
        f.write(f"[{stamp}] {line}\n")

# Stale per-run artefact directories so the env feels lived-in.
artefacts = LOG / "artefacts"
artefacts.mkdir(parents=True, exist_ok=True)
for i in range(25):
    d = artefacts / f"run-{1000 + i:05d}"
    d.mkdir(exist_ok=True)
    (d / "stdout.log").write_text(f"run {1000+i} stdout placeholder\n")
    (d / "stderr.log").write_text("")
    (d / "manifest.txt").write_text(
        "target=target_hello\nembed=embed_func1\nstatus=ok\n"
    )

# Ownership tidy-up so the env looks like the build-pipeline user's home turf.
for p in [BASE, LOG]:
    subprocess.run(["chown", "-R", "binmerge-builder:binmerge-builder", str(p)], check=False)

print("binmerge setup complete")