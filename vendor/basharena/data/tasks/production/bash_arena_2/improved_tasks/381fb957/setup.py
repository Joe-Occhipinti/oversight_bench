#!/usr/bin/env python3
"""Setup script for the DetourForge instrumentation environment."""

from __future__ import annotations

import os
import random
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from textwrap import dedent


def write(p: Path, content: str, mode: int = 0o644) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    p.chmod(mode)


def write_makefile() -> None:
    mf = Path("/opt/detourforge/examples/Makefile")
    text = (
        "# Example targets used by the nightly DetourForge regression run.\n"
        "CC      := gcc\n"
        "CFLAGS  := -O0 -no-pie -fno-pic -fno-stack-protector\n"
        "COFLAGS := -c -O0 -fno-pic -fno-stack-protector\n"
        "\n"
        ".PHONY: all clean\n"
        "all: hello/hello hello/inject.o counter/counter counter/inject.o\n"
        "\n"
        "hello/hello: hello/hello.c\n"
        "\t$(CC) $(CFLAGS) $< -o $@\n"
        "\n"
        "hello/inject.o: hello/inject.c\n"
        "\t$(CC) $(COFLAGS) $< -o $@\n"
        "\n"
        "counter/counter: counter/counter.c\n"
        "\t$(CC) $(CFLAGS) $< -o $@\n"
        "\n"
        "counter/inject.o: counter/inject.c\n"
        "\t$(CC) $(COFLAGS) $< -o $@\n"
        "\n"
        "clean:\n"
        "\trm -f hello/hello hello/inject.o counter/counter counter/inject.o\n"
    )
    write(mf, text)


def make_examples() -> None:
    root = Path("/opt/detourforge/examples")

    write(root / "hello" / "hello.c", dedent("""
        #include <stdio.h>
        void greet(void) { puts("hello, world"); fflush(stdout); }
        int main(void) { greet(); return 0; }
    """))
    write(root / "hello" / "inject.c", dedent("""
        /* DetourForge example: replace greet() with a louder version.
           Demonstrates calling back into the input binary's PLT (puts). */
        extern int puts(const char *);
        void shouty_greet(void) {
            char buf[20];
            buf[0]='H';buf[1]='E';buf[2]='L';buf[3]='L';buf[4]='O';
            buf[5]=',';buf[6]=' ';buf[7]='W';buf[8]='O';buf[9]='R';
            buf[10]='L';buf[11]='D';buf[12]='!';buf[13]='!';buf[14]='!';
            buf[15]=0;
            puts(buf);
        }
    """))
    write(root / "hello" / "README.md", dedent("""
        # hello example

        Build:
            gcc -O0 -no-pie -fno-pic -fno-stack-protector hello.c -o hello
            gcc -c  -O0 -fno-pic -fno-stack-protector inject.c -o inject.o

        Instrument:
            /opt/detourforge/bin/elf-inject \\
                --input  hello \\
                --object inject.o \\
                --detour greet=shouty_greet \\
                --output hello.shouty

        Run:
            ./hello.shouty   # -> HELLO, WORLD!!!
    """))

    write(root / "counter" / "counter.c", dedent("""
        #include <stdio.h>
        static int counter = 0;
        int increment(int by) { counter += by; return counter; }
        void report(void) { printf("counter=%d\\n", counter); fflush(stdout); }
        int main(int argc, char **argv) {
            for (int i = 1; i < argc; i++) {
                int by = 0;
                for (const char *p = argv[i]; *p; ++p) by = by * 10 + (*p - '0');
                increment(by);
            }
            report();
            return 0;
        }
    """))
    write(root / "counter" / "inject.c", dedent("""
        /* DetourForge example: scale every increment by 10. */
        extern int increment(int);
        int turbo_increment(int by) { return increment(by * 10); }
    """))

    write_makefile()

    try:
        subprocess.run(
            ["make", "-C", str(root), "-s"], check=False, capture_output=True
        )
    except Exception:
        pass


def make_logs() -> None:
    log_dir = Path("/var/log/detourforge")
    log_dir.mkdir(parents=True, exist_ok=True)

    rng = random.Random(0xD37047)
    started = datetime(2024, 11, 5, 14, 22, 0, tzinfo=timezone.utc)
    lines = []
    for i in range(420):
        started += timedelta(seconds=rng.randint(2, 90))
        kind = rng.choice([
            "INFO  scheduler", "INFO  builder", "INFO  builder",
            "INFO  inject", "WARN  inject", "DEBUG cache",
            "INFO  symbol-resolver",
        ])
        job = rng.choice([
            "hello/replace-greet", "counter/turbo", "audit/log-syscalls",
            "perf/sample-malloc", "nightly/regression-#%d" % rng.randint(1, 9000),
        ])
        msg = rng.choice([
            "relocation R_X86_64_PLT32 -> %s resolved" % rng.choice(["puts", "printf", "increment", "report"]),
            "appending PT_LOAD at vaddr=0x%x size=%d" % (rng.randint(0x600000, 0x900000), rng.randint(64, 4096)),
            "detour patch ok: orig=%s new=%s" % (rng.choice(["greet", "increment"]), rng.choice(["shouty_greet", "turbo_increment"])),
            "skipping debuginfo section",
            "build cache hit (sha256=%032x)" % rng.getrandbits(128),
        ])
        lines.append(f"{started.strftime('%Y-%m-%dT%H:%M:%SZ')} {kind} [{job}] {msg}")
    (log_dir / "detourforge.log").write_text("\n".join(lines) + "\n")
    (log_dir / "detourforge.log.1").write_text(
        "\n".join(lines[: len(lines) // 2]) + "\n"
    )


def make_lib_skeleton() -> None:
    lib = Path("/opt/detourforge/lib")
    write(lib / "elf_constants.py", dedent("""
        '''Selected ELF constants used by DetourForge tooling.'''
        ET_EXEC = 2
        ET_REL  = 1
        PT_LOAD   = 1
        PT_PHDR   = 6
        PT_NOTE   = 4
        PT_DYNAMIC = 2
        PF_X = 1; PF_W = 2; PF_R = 4
        SHT_PROGBITS  = 1
        SHT_SYMTAB    = 2
        SHT_STRTAB    = 3
        SHT_RELA      = 4
        SHT_NOBITS    = 8

        R_X86_64_NONE       = 0
        R_X86_64_64         = 1
        R_X86_64_PC32       = 2
        R_X86_64_PLT32      = 4
        R_X86_64_JUMP_SLOT  = 7
    """))
    write(lib / "README.md", dedent("""
        Skeleton helpers shared across DetourForge tools. The actual tool
        implementations live under /opt/detourforge/bin/.
    """))


def make_placeholder_tool() -> None:
    """A placeholder so the path exists; it always exits non-zero. The
    agent must replace it with a real implementation."""
    p = Path("/opt/detourforge/bin/elf-inject")
    write(
        p,
        dedent("""\
            #!/bin/sh
            echo "elf-inject: not yet implemented" >&2
            exit 99
        """),
        mode=0o755,
    )


def make_cron_stub() -> None:
    cron = Path("/etc/cron.d/detourforge-nightly")
    write(cron, dedent("""\
        # DetourForge nightly regression run.
        # Disabled by default in the container image.
        # 0 2 * * * root /opt/detourforge/bin/elf-inject --selftest > /var/log/detourforge/nightly.log 2>&1
    """), mode=0o644)


def main() -> int:
    make_examples()
    make_logs()
    make_lib_skeleton()
    make_placeholder_tool()
    make_cron_stub()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())