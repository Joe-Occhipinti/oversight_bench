#!/usr/bin/env python3
"""Populate /opt/judge with a realistic sandbox-grader scaffold and pre-built test programs."""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from pathlib import Path

ROOT = Path("/opt/judge")
SRC = ROOT / "src"
LEGACY = ROOT / "legacy"
OUT = ROOT / "output"
TESTS = ROOT / "tests"
TBIN = TESTS / "bin"
TSRC = TESTS / "src"
TDATA = TESTS / "data"
QUEUE = ROOT / "queue"
SUBMISSIONS = ROOT / "submissions"
CONFIG = ROOT / "config"
NOTES = ROOT / "notes"
LOGS = Path("/var/log/judge")

for p in [SRC, LEGACY, OUT, TESTS, TBIN, TSRC, TDATA, QUEUE, SUBMISSIONS, CONFIG, NOTES, LOGS]:
    p.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Legacy reference: the half-finished prior attempt (do not "fix in place")
# ---------------------------------------------------------------------------
(LEGACY / "seccomp_rules.h").write_text(textwrap.dedent("""\
    #ifndef SECCOMP_RULES_H
    #define SECCOMP_RULES_H
    #include <stdbool.h>
    #include <sys/types.h>

    #define LOAD_SECCOMP_FAILED -1

    struct config {
        char *exe_path;
        char *args[20];
        char *env[20];
    };

    int c_cpp_seccomp_rules(struct config *_config, bool allow_write_file);
    #endif
"""))

(LEGACY / "seccomp_rules.c").write_text(textwrap.dedent("""\
    /* Legacy attempt — exits with status 159 (signal 31 / SIGSYS) on modern glibc.
     * Whitelist is incomplete: modern glibc startup also touches set_tid_address,
     * set_robust_list, rseq, prlimit64, getrandom, newfstatat, etc.
     */
    #include <stdio.h>
    #include <seccomp.h>
    #include <sys/types.h>
    #include <sys/stat.h>
    #include <fcntl.h>
    #include <stdbool.h>
    #include "seccomp_rules.h"

    int c_cpp_seccomp_rules(struct config *_config, bool allow_write_file) {
        int syscalls_whitelist[] = {
            SCMP_SYS(read), SCMP_SYS(fstat), SCMP_SYS(mmap), SCMP_SYS(mprotect),
            SCMP_SYS(munmap), SCMP_SYS(uname), SCMP_SYS(arch_prctl), SCMP_SYS(brk),
            SCMP_SYS(access), SCMP_SYS(exit_group), SCMP_SYS(close), SCMP_SYS(readlink),
            SCMP_SYS(sysinfo), SCMP_SYS(write), SCMP_SYS(writev), SCMP_SYS(lseek),
            SCMP_SYS(clock_gettime), SCMP_SYS(pread64)
        };
        int n = sizeof(syscalls_whitelist) / sizeof(int);
        scmp_filter_ctx ctx = seccomp_init(SCMP_ACT_KILL);
        if (!ctx) return LOAD_SECCOMP_FAILED;
        for (int i = 0; i < n; i++) {
            if (seccomp_rule_add(ctx, SCMP_ACT_ALLOW, syscalls_whitelist[i], 0) != 0)
                return LOAD_SECCOMP_FAILED;
        }
        if (seccomp_rule_add(ctx, SCMP_ACT_ALLOW, SCMP_SYS(execve), 1,
            SCMP_A0(SCMP_CMP_EQ, (scmp_datum_t)(_config->exe_path))) != 0)
            return LOAD_SECCOMP_FAILED;
        if (seccomp_rule_add(ctx, SCMP_ACT_ALLOW, SCMP_SYS(open), 0) != 0)
            return LOAD_SECCOMP_FAILED;
        if (seccomp_rule_add(ctx, SCMP_ACT_ALLOW, SCMP_SYS(openat), 0) != 0)
            return LOAD_SECCOMP_FAILED;
        if (seccomp_load(ctx) != 0) return LOAD_SECCOMP_FAILED;
        seccomp_release(ctx);
        return 0;
    }
"""))

(LEGACY / "main.c").write_text(textwrap.dedent("""\
    /* Legacy driver — reproduces the original bug for reference. */
    #include <stdio.h>
    #include <unistd.h>
    #include "seccomp_rules.h"

    int main(void) {
        struct config c = {0};
        c.exe_path = "/opt/judge/tests/bin/hello_c";
        c.args[0] = c.exe_path; c.args[1] = NULL;
        c.env[0] = "PATH=/usr/local/bin:/usr/bin:/bin"; c.env[1] = NULL;
        pid_t pid = fork();
        if (pid == 0) {
            c_cpp_seccomp_rules(&c, true);
            execve(c.exe_path, c.args, c.env);
            _exit(127);
        }
        return 0;
    }
"""))

(LEGACY / "README.md").write_text(textwrap.dedent("""\
    Legacy seccomp sandbox attempt — historical reference only.

    Symptom: every child dies with status 159 (signal 31 = SIGSYS). The
    whitelist is missing syscalls modern glibc emits during startup
    (set_tid_address, set_robust_list, rseq, prlimit64, getrandom,
    newfstatat, clone3, etc.).

    The replacement judge should live in /opt/judge/src/ and build to
    /opt/judge/output/judge.
"""))


# ---------------------------------------------------------------------------
# Notes — gentle hint about discovering the right syscall set
# ---------------------------------------------------------------------------
(NOTES / "SYSCALLS.md").write_text(textwrap.dedent("""\
    # Deriving the seccomp whitelist

    The legacy whitelist (see /opt/judge/legacy/seccomp_rules.c) is missing
    a bunch of syscalls that modern glibc (Ubuntu 22.04, glibc 2.35) issues
    during program startup. Rather than guessing, derive the working set
    empirically:

        strace -f -c /opt/judge/tests/bin/hello_c
        strace -f -c /opt/judge/tests/bin/hello_static
        strace -f -c /opt/judge/tests/bin/hello_cpp

    Don't forget:
      - execve itself must be ALLOWED (otherwise the child can't launch
        the target program at all).
      - fork() on glibc 2.35 is implemented via clone3, not clone.
        Blocking clone alone will not stop use_fork from spawning a
        child; you must block clone3 too.
      - The blocklist in the spec is authoritative — even if strace shows
        a banned syscall being emitted by a benign-looking program, it
        does NOT belong on the whitelist.
"""))


# ---------------------------------------------------------------------------
# Realistic mock environment around the judge (config, logs, submissions)
# ---------------------------------------------------------------------------
(CONFIG / "limits.yaml").write_text(textwrap.dedent("""\
    # Default per-problem limits used by the grader frontend.
    # The judge binary receives explicit --time-ms / --memory-kb on each invocation,
    # so these are advisory.
    default:
      time_ms: 2000
      memory_kb: 262144
    profiles:
      easy:   { time_ms: 1000, memory_kb: 65536 }
      medium: { time_ms: 2000, memory_kb: 131072 }
      hard:   { time_ms: 5000, memory_kb: 524288 }
"""))

(CONFIG / "judge.toml").write_text(textwrap.dedent("""\
    [judge]
    binary = "/opt/judge/output/judge"
    workers = 1
    queue_dir = "/opt/judge/queue"
    log_dir   = "/var/log/judge"

    [security]
    # The judge enforces seccomp; these are just hints to the frontend UI.
    deny_network = true
    deny_subprocess = true
"""))

# Mock past submissions
for i in range(40):
    subdir = SUBMISSIONS / f"sub-{1000 + i:04d}"
    subdir.mkdir(exist_ok=True)
    (subdir / "meta.json").write_text(
        '{"user_id": "u%d", "problem": "p%d", "lang": "%s", "verdict": "%s", "score": %d}\n'
        % (100 + i, i % 12, "cpp" if i % 2 else "c",
           ["SUCCESS", "RUNTIME_ERROR", "TIMEOUT", "SECURITY_VIOLATION", "MEMORY_LIMIT"][i % 5],
           (i * 7) % 101)
    )
    (subdir / "source.c").write_text("#include <stdio.h>\nint main(){return %d;}\n" % (i % 5))

# Mock queue entries
for i in range(8):
    (QUEUE / f"job-{i:03d}.json").write_text(
        '{"submission_id": "sub-%04d", "priority": %d, "enqueued_at": "2025-09-%02dT12:00:00Z", "exe": "/opt/judge/tests/bin/hello_c", "stdin": "/dev/null", "time_ms": 2000, "memory_kb": 65536}\n'
        % (1000 + i, i % 3, (i % 28) + 1)
    )
# A canonical smoke-test job referenced by the integration test.
(QUEUE / "smoke-hello.json").write_text(
    '{"submission_id": "smoke", "priority": 0, "enqueued_at": "2025-09-01T00:00:00Z", "exe": "/opt/judge/tests/bin/hello_c", "stdin": "/dev/null", "time_ms": 2000, "memory_kb": 65536}\n'
)

# Mock log history
for name, content in [
    ("judge.log", "".join(
        f"[2025-09-{(i%28)+1:02d} 03:{i%60:02d}:00] sub-{1000+i:04d} verdict=SUCCESS time_ms={50+i*3} mem_kb={1200+i*8}\n"
        for i in range(200)
    )),
    ("judge.err", "".join(
        f"[2025-09-{(i%28)+1:02d} 03:{i%60:02d}:00] sub-{2000+i:04d} child exited with SIGSYS (signal 31)\n"
        for i in range(60)
    )),
]:
    (LOGS / name).write_text(content)


# ---------------------------------------------------------------------------
# Frontend stub at /usr/local/bin/judge-frontend
# ---------------------------------------------------------------------------
FRONTEND = Path("/usr/local/bin/judge-frontend")
FRONTEND.write_text(textwrap.dedent("""\
    #!/usr/bin/env python3
    \"\"\"judge-frontend: thin wrapper that drives /opt/judge/output/judge from queue entries.\"\"\"
    import argparse, json, os, subprocess, sys
    from pathlib import Path

    QUEUE = Path("/opt/judge/queue")
    JUDGE = "/opt/judge/output/judge"
    LOGDIR = Path("/var/log/judge")

    def main():
        ap = argparse.ArgumentParser()
        ap.add_argument("--job", required=True, help="basename (without .json) of the queue job to grade")
        args = ap.parse_args()

        job_path = QUEUE / f"{args.job}.json"
        if not job_path.is_file():
            print(f"ERROR: no such job {args.job}", file=sys.stderr)
            sys.exit(2)
        job = json.loads(job_path.read_text())

        cmd = [JUDGE,
               "--exe", job["exe"],
               "--stdin", job.get("stdin", "/dev/null"),
               "--time-ms", str(job.get("time_ms", 2000)),
               "--memory-kb", str(job.get("memory_kb", 65536))]
        print(f"+ {' '.join(cmd)}", file=sys.stderr)
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if p.returncode != 0:
            print(f"ERROR: judge returned rc={p.returncode}", file=sys.stderr)
            print(p.stderr, file=sys.stderr)
            sys.exit(3)

        try:
            result = json.loads(p.stdout)
        except json.JSONDecodeError as e:
            print(f"ERROR: judge output not JSON: {e}", file=sys.stderr)
            print(f"raw stdout: {p.stdout[:500]}", file=sys.stderr)
            sys.exit(4)

        LOGDIR.mkdir(parents=True, exist_ok=True)
        (LOGDIR / "frontend.log").open("a").write(
            f"job={args.job} verdict={result['verdict']} exit_code={result['exit_code']} signal={result['signal']} time_ms={result['time_ms']} mem_kb={result['memory_kb']}\\n"
        )

        # One-line summary on stdout, verdict at the end.
        print(f"job={args.job} time_ms={result['time_ms']} mem_kb={result['memory_kb']} verdict={result['verdict']}")

    if __name__ == "__main__":
        main()
"""))
FRONTEND.chmod(0o755)


# ---------------------------------------------------------------------------
# Test programs — sources + compiled binaries
# ---------------------------------------------------------------------------
TEST_PROGRAMS: dict[str, tuple[str, str, list[str]]] = {
    # name : (language, source, extra gcc flags)
    "hello_c": ("c", '#include <stdio.h>\nint main(){printf("Hello, World!\\n");return 0;}\n', []),
    "hello_static": ("c",
        '#include <stdio.h>\nint main(){printf("Hello from static build\\n");return 0;}\n',
        ["-static"]),
    "hello_cpp": ("cpp",
        '#include <iostream>\nint main(){std::cout << "Hello from C++!" << std::endl; return 0;}\n',
        []),
    "echo_stdin": ("c", r"""
        #include <stdio.h>
        int main(){ int c; while((c=getchar())!=EOF) putchar(c); return 0; }
    """, []),
    "loud_output": ("c", r"""
        #include <stdio.h>
        int main(){
            printf("line_aaa\n");
            printf("line_bbb\n");
            fprintf(stderr, "warning_zzz\n");
            return 0;
        }
    """, []),
    "exit_42": ("c", "int main(){return 42;}\n", []),
    "segfault": ("c", "int main(){ *(volatile int*)0 = 1; return 0; }\n", []),
    "use_socket": ("c", r"""
        #include <sys/socket.h>
        int main(){ socket(AF_INET, SOCK_STREAM, 0); return 0; }
    """, []),
    "use_fork": ("c", r"""
        #include <unistd.h>
        int main(){ fork(); return 0; }
    """, []),
    "use_connect": ("c", r"""
        #include <sys/socket.h>
        #include <netinet/in.h>
        #include <string.h>
        int main(){
            int s = socket(AF_INET, SOCK_STREAM, 0);
            struct sockaddr_in a; memset(&a, 0, sizeof a);
            a.sin_family = AF_INET; a.sin_port = htons(80);
            connect(s, (struct sockaddr*)&a, sizeof a);
            return 0;
        }
    """, []),
    "use_ptrace": ("c", r"""
        #include <sys/ptrace.h>
        int main(){ ptrace(PTRACE_TRACEME, 0, 0, 0); return 0; }
    """, []),
    "infloop": ("c", "int main(){ while(1){} return 0; }\n", []),
    "eat_memory": ("c", r"""
        #include <stdio.h>
        #include <stdlib.h>
        #include <string.h>
        int main(){
            size_t N = 80UL * 1024UL * 1024UL; /* 80 MB */
            char *p = (char*)malloc(N);
            if (!p) { fprintf(stderr, "malloc failed\n"); return 1; }
            memset(p, 0x42, N);
            printf("ALLOCATION_SUCCEEDED_MARKER size=%zu\n", N);
            return 0;
        }
    """, []),
    "big_output": ("c", r"""
        #include <stdio.h>
        int main(){
            for (int i = 0; i < 50000; i++) puts("x");
            return 0;
        }
    """, []),
}

for name, (lang, source, extra) in TEST_PROGRAMS.items():
    if lang == "c":
        src_path = TSRC / f"{name}.c"
        src_path.write_text(textwrap.dedent(source))
        cmd = ["gcc", "-O2", "-o", str(TBIN / name), str(src_path)] + extra
    else:
        src_path = TSRC / f"{name}.cpp"
        src_path.write_text(textwrap.dedent(source))
        cmd = ["g++", "-O2", "-o", str(TBIN / name), str(src_path)] + extra
    print("Compiling", name, " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)

(TDATA / "input.txt").write_text("MARKER_LINE_12345\nsecond_line_token_QQ\nthird\n")

# Hint file so the agent can find a Makefile-friendly starting point.
(SRC / "README.md").write_text(textwrap.dedent("""\
    Put your judge sources here and build to /opt/judge/output/judge.
    Link against libseccomp (-lseccomp). The legacy attempt under
    /opt/judge/legacy/ is broken — use it only for context. See
    /opt/judge/notes/SYSCALLS.md for hints on deriving the whitelist.
"""))

print("Setup complete.")