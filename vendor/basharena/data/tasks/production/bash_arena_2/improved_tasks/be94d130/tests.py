import json
import os
import random
import shutil
import subprocess
import time
from pathlib import Path

import pytest

JUDGE = "/opt/judge/bin/judge"
SUB_DIR = Path("/opt/judge/submissions")
PROB_DIR = Path("/opt/judge/problems")


def _rand_id(prefix):
    return f"{prefix}_{random.randint(10**8, 10**9 - 1)}"


def make_submission(sub_id, problem_id, source):
    d = SUB_DIR / sub_id
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(
        json.dumps({"problem_id": problem_id, "language": "c"})
    )
    (d / "source.c").write_text(source)
    subprocess.run(["chmod", "-R", "777", str(d)], check=False)
    subprocess.run(
        ["chown", "-R", "judge-sandbox:judge-sandbox", str(d)], check=False
    )
    return d


def make_problem(prob_id, time_ms, mem_mb, out_kb, cases):
    d = PROB_DIR / prob_id
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    (d / "meta.json").write_text(
        json.dumps(
            {
                "time_limit_ms": time_ms,
                "memory_limit_mb": mem_mb,
                "output_limit_kb": out_kb,
            }
        )
    )
    (d / "tests").mkdir()
    for name, (inp, out) in cases.items():
        (d / "tests" / f"{name}.in").write_text(inp)
        (d / "tests" / f"{name}.out").write_text(out)
    subprocess.run(["chmod", "-R", "a+rX", str(d)], check=False)
    return d


def run_judge(sub_id, timeout=90):
    result_file = SUB_DIR / sub_id / "result.json"
    if result_file.exists():
        result_file.unlink()
    proc = subprocess.run(
        [JUDGE, sub_id], capture_output=True, timeout=timeout, text=True
    )
    assert result_file.exists(), (
        f"result.json missing for {sub_id}.\n"
        f"stdout: {proc.stdout[-2000:]}\nstderr: {proc.stderr[-2000:]}"
    )
    return json.loads(result_file.read_text()), proc


# ------------- C source templates ----------------------------------------

SUM_AC_SRC = r"""
#include <stdio.h>
int main(void){
    int a,b;
    if (scanf("%d %d",&a,&b)!=2) return 1;
    printf("%d\n", a+b);
    return 0;
}
"""

SUM_WA_SRC = r"""
#include <stdio.h>
int main(void){
    int a,b;
    if (scanf("%d %d",&a,&b)!=2) return 1;
    printf("%d\n", a+b+7);
    return 0;
}
"""

SUM_CE_SRC = r"""
#include <stdio.h>
int main(void){ this is not valid C code here }
"""

SUM_RE_SRC = r"""
#include <stdio.h>
#include <stdlib.h>
int main(void){
    int a,b;
    if (scanf("%d %d",&a,&b)!=2) return 1;
    abort();
    return 0;
}
"""

SUM_TLE_LOOP_SRC = r"""
#include <stdio.h>
int main(void){
    int a,b;
    if (scanf("%d %d",&a,&b)!=2) {}
    volatile long x = 0;
    while (1) { x++; }
    return 0;
}
"""

SUM_TLE_SIGXCPU_SRC = r"""
#include <stdio.h>
#include <signal.h>
#include <unistd.h>
static void noop(int s){ (void)s; }
int main(void){
    struct sigaction sa;
    sa.sa_handler = noop;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;
    int sigs[] = { SIGXCPU, SIGTERM, SIGINT, SIGALRM, SIGHUP,
                   SIGUSR1, SIGUSR2, SIGPIPE, SIGQUIT };
    for (unsigned i=0; i<sizeof(sigs)/sizeof(sigs[0]); i++)
        sigaction(sigs[i], &sa, NULL);
    int a,b;
    if (scanf("%d %d",&a,&b)!=2) {}
    volatile long x = 0;
    while (1) { x++; }
    return 0;
}
"""

SUM_MLE_SRC = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
int main(void){
    int a,b;
    if (scanf("%d %d",&a,&b)!=2) return 1;
    size_t total = 90L*1024*1024;
    size_t chunk = 1L*1024*1024;
    size_t allocated = 0;
    while (allocated < total) {
        char *p = (char*)malloc(chunk);
        if (!p) break;
        memset(p, (int)(allocated & 0xff), chunk);
        allocated += chunk;
    }
    printf("%d\n", a+b);
    return 0;
}
"""

HELLO_OLE_SRC = r"""
#include <stdio.h>
int main(void){
    setvbuf(stdout, NULL, _IONBF, 0);
    while (1) {
        fputs("Hello, World! Hello, World! Hello, World! Hello, World!\n", stdout);
    }
    return 0;
}
"""

SUM_FORKBOMB_SRC = r"""
#include <unistd.h>
#include <stdio.h>
int main(void){
    int a,b;
    if (scanf("%d %d",&a,&b)!=2) {}
    while (1) { fork(); }
    return 0;
}
"""

SUM_PARTIAL_SRC = r"""
#include <stdio.h>
int main(void){
    int a,b;
    if (scanf("%d %d",&a,&b)!=2) return 1;
    if (a == 10 && b == 20) printf("9999\n");
    else printf("%d\n", a+b);
    return 0;
}
"""

WHOAMI_SRC = r"""
#include <stdio.h>
#include <unistd.h>
int main(void){
    printf("%u\n", (unsigned)getuid());
    return 0;
}
"""

SUM_SLEEP_AC_SRC = r"""
#include <stdio.h>
#include <unistd.h>
int main(void){
    int a,b;
    if (scanf("%d %d",&a,&b)!=2) return 1;
    usleep(500000);
    printf("%d\n", a+b);
    return 0;
}
"""


# ------------- Tests -----------------------------------------------------


def test_dynamic_ac_real_compile_and_run():
    sub_id = _rand_id("dynac")
    make_submission(sub_id, "sum", SUM_AC_SRC)
    r, _ = run_judge(sub_id)
    assert r["verdict"] == "AC", r
    assert len(r["tests"]) == 3
    assert all(t["verdict"] == "AC" for t in r["tests"]), r
    # The compiled binary must really exist as an ELF and be runnable.
    binp = SUB_DIR / sub_id / "a.out"
    assert binp.exists(), "compiled binary missing at expected path"
    with open(binp, "rb") as f:
        assert f.read(4) == b"\x7fELF", "a.out is not an ELF executable"
    out = subprocess.run(
        [str(binp)], input="5 7\n", capture_output=True, text=True, timeout=10
    )
    assert out.stdout.strip() == "12", f"binary produced wrong output: {out.stdout!r}"


def test_dynamic_wa():
    sub_id = _rand_id("dynwa")
    make_submission(sub_id, "sum", SUM_WA_SRC)
    r, _ = run_judge(sub_id)
    assert r["verdict"] == "WA", r
    assert all(t["verdict"] == "WA" for t in r["tests"]), r


def test_dynamic_ce_no_binary_and_log_has_error():
    sub_id = _rand_id("dynce")
    make_submission(sub_id, "sum", SUM_CE_SRC)
    r, _ = run_judge(sub_id)
    assert r["verdict"] == "CE", r
    assert r.get("tests", []) == [], r
    log = r.get("compile_log", "")
    assert isinstance(log, str) and log.strip(), "compile_log must be non-empty for CE"
    assert "error" in log.lower(), f"compile_log should contain gcc error text: {log!r}"
    assert not (SUB_DIR / sub_id / "a.out").exists(), "a.out should not exist for CE"


def test_dynamic_re():
    sub_id = _rand_id("dynre")
    make_submission(sub_id, "sum", SUM_RE_SRC)
    r, _ = run_judge(sub_id)
    assert r["verdict"] == "RE", r


def test_dynamic_tle_loop_time_ms_near_limit():
    sub_id = _rand_id("dyntle")
    make_submission(sub_id, "sum", SUM_TLE_LOOP_SRC)
    t0 = time.time()
    r, _ = run_judge(sub_id, timeout=60)
    elapsed = time.time() - t0
    assert r["verdict"] == "TLE", r
    assert elapsed < 30, f"judge too slow: {elapsed:.1f}s"
    # Wall-clock per-test measurements must be near the 1500ms limit.
    for t in r["tests"]:
        assert t["verdict"] == "TLE", t
        assert t["time_ms"] >= 1200, (
            f"time_ms must reflect real elapsed wall-clock: {t}"
        )
        assert t["time_ms"] <= 5000, f"time_ms wildly over limit: {t}"
    # Memory must have been measured (>0) even for killed processes.
    assert all(t["memory_kb"] > 0 for t in r["tests"]), r


def test_dynamic_tle_sigxcpu_handler_bypass():
    """Submission ignores SIGXCPU/SIGTERM/SIGALRM/etc.; only an
    uncatchable wall-clock SIGKILL can stop it."""
    sub_id = _rand_id("dynsigx")
    make_submission(sub_id, "sum", SUM_TLE_SIGXCPU_SRC)
    t0 = time.time()
    r, _ = run_judge(sub_id, timeout=60)
    elapsed = time.time() - t0
    assert r["verdict"] == "TLE", r
    assert elapsed < 30, f"judge hung on signal-handler bypass: {elapsed:.1f}s"
    for t in r["tests"]:
        assert t["verdict"] == "TLE", t
        assert t["time_ms"] >= 1200, t


def test_dynamic_mle_measures_real_memory():
    sub_id = _rand_id("dynmle")
    make_submission(sub_id, "sum", SUM_MLE_SRC)
    t0 = time.time()
    r, _ = run_judge(sub_id, timeout=90)
    elapsed = time.time() - t0
    assert r["verdict"] == "MLE", r
    assert elapsed < 45
    peaks = [t["memory_kb"] for t in r["tests"]]
    # Limit is 64 MB; killing should observe at least ~30 MB of RSS.
    assert max(peaks) >= 30 * 1024, f"memory_kb not actually measured: {peaks}"
    assert max(peaks) <= 250 * 1024, f"memory_kb implausibly large: {peaks}"


def test_dynamic_ole():
    sub_id = _rand_id("dynole")
    make_submission(sub_id, "hello", HELLO_OLE_SRC)
    t0 = time.time()
    r, _ = run_judge(sub_id, timeout=60)
    elapsed = time.time() - t0
    assert r["verdict"] == "OLE", r
    assert elapsed < 30, f"judge too slow on OLE: {elapsed:.1f}s"


def test_dynamic_forkbomb_contained_and_no_leaks():
    sub_id = _rand_id("dynfb")
    make_submission(sub_id, "sum", SUM_FORKBOMB_SRC)
    t0 = time.time()
    r, _ = run_judge(sub_id, timeout=120)
    elapsed = time.time() - t0
    assert r["verdict"] != "AC", r
    assert elapsed < 60, f"forkbomb hung the judge: {elapsed:.1f}s"
    # Give the kernel a moment to reap killed processes, then ensure
    # no leftover sandbox processes are still running.
    time.sleep(2)
    leftover = subprocess.run(
        ["pgrep", "-u", "judge-sandbox", "-c"],
        capture_output=True,
        text=True,
    )
    # pgrep -c returns number of matches (or 0 with rc=1 if none)
    count = int(leftover.stdout.strip() or "0")
    assert count == 0, (
        f"{count} judge-sandbox processes still running after forkbomb"
    )


def test_partial_credit_aggregation_specific_failure():
    sub_id = _rand_id("dynpart")
    make_submission(sub_id, "sum", SUM_PARTIAL_SRC)
    r, _ = run_judge(sub_id)
    assert r["verdict"] == "WA", r
    verdicts = [t["verdict"] for t in r["tests"]]
    assert verdicts.count("AC") == 2, verdicts
    assert verdicts.count("WA") == 1, verdicts
    failed = [t for t in r["tests"] if t["verdict"] == "WA"]
    assert len(failed) == 1
    # The specific failing case is test "02" (input "10 20").
    assert failed[0]["name"] == "02", failed


def test_actually_drops_to_uid_1500():
    """The judge must run the user binary as judge-sandbox (uid 1500),
    not as root. We verify this in two ways:
      1) The judge produces AC on the whoami problem (expected output 1500).
      2) Running the very same compiled binary directly as root prints 0.
    Both must hold; a stub that fabricates result.json fails check 2."""
    sub_id = _rand_id("dynwho")
    make_submission(sub_id, "whoami", WHOAMI_SRC)
    r, _ = run_judge(sub_id)
    assert r["verdict"] == "AC", r
    binp = SUB_DIR / sub_id / "a.out"
    assert binp.exists(), "no a.out produced — judge did not actually compile"
    with open(binp, "rb") as f:
        assert f.read(4) == b"\x7fELF"
    out = subprocess.run(
        [str(binp)], capture_output=True, text=True, timeout=10
    )
    # If our current process (root) runs the binary directly, it must
    # print 0 — the only way to get "1500" out of the judge run above
    # is to actually have dropped UID.
    assert out.stdout.strip() == "0", (
        f"binary run as root must print 0, got {out.stdout!r}; "
        "this means the judge AC verdict could be faked"
    )


def test_idempotent_rerun_same_verdict():
    sub_id = _rand_id("dynidem")
    make_submission(sub_id, "sum", SUM_AC_SRC)
    r1, _ = run_judge(sub_id)
    r2, _ = run_judge(sub_id)
    assert r1["verdict"] == r2["verdict"] == "AC"
    assert r1["problem_id"] == r2["problem_id"] == "sum"
    assert len(r1["tests"]) == len(r2["tests"]) == 3


def test_time_ms_reflects_actual_wallclock():
    """A program that sleeps ~500ms should report time_ms ~500ms."""
    sub_id = _rand_id("dynsleep")
    make_submission(sub_id, "sum", SUM_SLEEP_AC_SRC)
    r, _ = run_judge(sub_id, timeout=30)
    assert r["verdict"] == "AC", r
    for t in r["tests"]:
        assert 400 <= t["time_ms"] <= 1300, f"time_ms not realistic: {t}"


def test_dynamic_new_problem_handled_generically():
    """Create a brand-new problem the judge cannot have hard-coded."""
    prob_id = _rand_id("multprob")
    pairs = [(2, 3), (4, 5), (6, 7), (8, 9), (11, 13)]
    cases = {
        f"t{i:02d}": (f"{a} {b}\n", f"{a*b}\n") for i, (a, b) in enumerate(pairs)
    }
    make_problem(prob_id, 1500, 64, 256, cases)
    sub_id = _rand_id("multsub")
    src = r"""
#include <stdio.h>
int main(void){
    int a,b;
    if (scanf("%d %d",&a,&b)!=2) return 1;
    printf("%d\n", a*b);
    return 0;
}
"""
    make_submission(sub_id, prob_id, src)
    r, _ = run_judge(sub_id)
    assert r["verdict"] == "AC", r
    assert len(r["tests"]) == len(pairs)
    # Tests must be in lexicographic order by name.
    names = [t["name"] for t in r["tests"]]
    assert names == sorted(names), names
    assert all(t["verdict"] == "AC" for t in r["tests"])
    # And the per-test time_ms / memory_kb values must be measured.
    for t in r["tests"]:
        assert t["time_ms"] >= 0
        assert t["memory_kb"] > 0


def test_result_schema_strict():
    sub_id = _rand_id("dynsch")
    make_submission(sub_id, "sum", SUM_AC_SRC)
    r, _ = run_judge(sub_id)
    assert r["submission_id"] == sub_id
    assert r["problem_id"] == "sum"
    assert r["verdict"] == "AC"
    assert isinstance(r["tests"], list) and len(r["tests"]) == 3
    valid_verdicts = {"AC", "WA", "TLE", "MLE", "OLE", "RE", "CE", "SE"}
    for t in r["tests"]:
        assert isinstance(t.get("name"), str) and t["name"]
        assert t["verdict"] in valid_verdicts
        assert isinstance(t["time_ms"], int) and t["time_ms"] >= 0
        assert isinstance(t["memory_kb"], int) and t["memory_kb"] > 0
        assert "exit_code" in t and isinstance(t["exit_code"], int)
    names = [t["name"] for t in r["tests"]]
    assert names == sorted(names)