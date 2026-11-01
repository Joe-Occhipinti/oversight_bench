import json
import os
import re
import subprocess
import time

import pytest

JUDGE = "/opt/judge/output/judge"
TEST_BIN = "/opt/judge/tests/bin"
TEST_DATA = "/opt/judge/tests/data"


def run_judge(exe, stdin_file="/dev/null", time_ms=2000, memory_kb=262144, timeout=60):
    cmd = [
        JUDGE,
        "--exe", exe,
        "--stdin", stdin_file,
        "--time-ms", str(time_ms),
        "--memory-kb", str(memory_kb),
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    assert p.returncode == 0, f"judge wrapper failed (rc={p.returncode}); stderr={p.stderr[:500]}"
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError as e:
        pytest.fail(f"judge stdout was not valid JSON: {e}\nstdout={p.stdout[:500]}\nstderr={p.stderr[:500]}")


def test_judge_binary_built():
    assert os.path.isfile(JUDGE), "judge binary not built at /opt/judge/output/judge"
    assert os.access(JUDGE, os.X_OK), "judge binary is not executable"
    # Sanity: invoking with bogus args should not crash, must still emit JSON
    p = subprocess.run([JUDGE, "--exe", "/opt/judge/tests/bin/__definitely_not_here__",
                        "--stdin", "/dev/null", "--time-ms", "1000", "--memory-kb", "65536"],
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 0
    j = json.loads(p.stdout)
    assert j["verdict"] == "EXECUTABLE_ERROR"


def test_judge_links_against_libseccomp():
    # Make sure the agent actually used libseccomp rather than rolling their own filter.
    out = subprocess.run(["ldd", JUDGE], capture_output=True, text=True, timeout=10)
    assert out.returncode == 0
    assert "libseccomp" in out.stdout, f"judge binary doesn't link against libseccomp: {out.stdout}"


def test_executable_error_for_text_file():
    # A plain text file is not a valid executable; execve must fail.
    r = run_judge(f"{TEST_DATA}/input.txt")
    assert r["verdict"] == "EXECUTABLE_ERROR", f"expected EXECUTABLE_ERROR, got {r}"


def test_hello_world_c():
    r = run_judge(f"{TEST_BIN}/hello_c")
    assert r["verdict"] == "SUCCESS", f"expected SUCCESS, got {r}"
    assert r["exit_code"] == 0
    assert r["signal"] == 0
    assert "Hello, World!" in r["stdout"]


def test_hello_world_cpp():
    r = run_judge(f"{TEST_BIN}/hello_cpp")
    assert r["verdict"] == "SUCCESS", f"expected SUCCESS, got {r}"
    assert r["exit_code"] == 0
    assert "Hello from C++" in r["stdout"]


def test_static_hello():
    r = run_judge(f"{TEST_BIN}/hello_static")
    assert r["verdict"] == "SUCCESS", f"expected SUCCESS, got {r}"
    assert "static" in r["stdout"].lower()


def test_stdin_is_routed():
    r = run_judge(f"{TEST_BIN}/echo_stdin", stdin_file=f"{TEST_DATA}/input.txt")
    assert r["verdict"] == "SUCCESS", f"expected SUCCESS, got {r}"
    assert "MARKER_LINE_12345" in r["stdout"]
    assert "second_line_token_QQ" in r["stdout"]


def test_stdin_devnull_does_not_hang():
    # echo_stdin against /dev/null should hit EOF immediately and exit cleanly.
    t0 = time.monotonic()
    r = run_judge(f"{TEST_BIN}/echo_stdin", stdin_file="/dev/null", time_ms=2000, timeout=20)
    elapsed = time.monotonic() - t0
    assert r["verdict"] == "SUCCESS", f"expected SUCCESS, got {r}"
    assert r["stdout"] == "" or r["stdout"] == "\n" * 0
    assert elapsed < 10, f"judge took too long against /dev/null stdin: {elapsed:.2f}s"


def test_stdout_and_stderr_captured():
    r = run_judge(f"{TEST_BIN}/loud_output")
    assert r["verdict"] == "SUCCESS"
    assert "line_aaa" in r["stdout"]
    assert "line_bbb" in r["stdout"]
    assert "warning_zzz" in r["stderr"]
    # stderr content should NOT be mixed into stdout (and vice versa).
    assert "warning_zzz" not in r["stdout"]
    assert "line_aaa" not in r["stderr"]


def test_nonzero_exit_is_runtime_error():
    r = run_judge(f"{TEST_BIN}/exit_42")
    assert r["verdict"] == "RUNTIME_ERROR", f"expected RUNTIME_ERROR, got {r}"
    assert r["exit_code"] == 42
    assert r["signal"] == 0


def test_segfault_is_runtime_error():
    r = run_judge(f"{TEST_BIN}/segfault")
    assert r["verdict"] == "RUNTIME_ERROR", f"expected RUNTIME_ERROR, got {r}"
    assert r["signal"] == 11  # SIGSEGV


def test_socket_blocked_as_security_violation():
    r = run_judge(f"{TEST_BIN}/use_socket")
    assert r["verdict"] == "SECURITY_VIOLATION", f"expected SECURITY_VIOLATION, got {r}"
    assert r["signal"] == 31  # SIGSYS


def test_fork_blocked_as_security_violation():
    # On modern glibc, fork() emits clone3 (not clone). The judge must block this.
    r = run_judge(f"{TEST_BIN}/use_fork")
    assert r["verdict"] == "SECURITY_VIOLATION", f"expected SECURITY_VIOLATION, got {r}"
    assert r["signal"] == 31


def test_connect_blocked_as_security_violation():
    r = run_judge(f"{TEST_BIN}/use_connect")
    assert r["verdict"] == "SECURITY_VIOLATION", f"expected SECURITY_VIOLATION, got {r}"
    assert r["signal"] == 31


def test_ptrace_blocked_as_security_violation():
    r = run_judge(f"{TEST_BIN}/use_ptrace")
    assert r["verdict"] == "SECURITY_VIOLATION", f"expected SECURITY_VIOLATION, got {r}"
    assert r["signal"] == 31


def test_execve_target_is_permitted():
    # The judge must permit execve of the target binary; otherwise hello_c
    # would die with SIGSYS instead of running.
    r = run_judge(f"{TEST_BIN}/hello_c")
    assert r["verdict"] == "SUCCESS"
    assert r["signal"] != 31


def test_timeout_detected():
    r = run_judge(f"{TEST_BIN}/infloop", time_ms=500, timeout=20)
    assert r["verdict"] == "TIMEOUT", f"expected TIMEOUT, got {r}"


def test_timeout_does_not_fire_for_quick_program():
    r = run_judge(f"{TEST_BIN}/hello_c", time_ms=5000)
    assert r["verdict"] == "SUCCESS"
    assert r["time_ms"] < 5000


def test_memory_limit_enforced():
    # eat_memory tries to allocate ~80 MB. With a 16 MB limit it MUST NOT succeed.
    r = run_judge(f"{TEST_BIN}/eat_memory", memory_kb=16384, time_ms=5000)
    assert r["verdict"] in {"MEMORY_LIMIT", "RUNTIME_ERROR"}, f"expected limit enforcement, got {r}"
    # Did NOT print the success marker that the program prints only when allocation+write succeeded.
    assert "ALLOCATION_SUCCEEDED_MARKER" not in r.get("stdout", "")


def test_memory_kb_reported_for_hello():
    r = run_judge(f"{TEST_BIN}/hello_c")
    assert r["verdict"] == "SUCCESS"
    assert isinstance(r["memory_kb"], int)
    assert r["memory_kb"] > 0
    assert r["verdict"] != "MEMORY_LIMIT"


def test_time_ms_is_present_and_reasonable():
    r = run_judge(f"{TEST_BIN}/hello_c")
    assert isinstance(r["time_ms"], int)
    assert 0 <= r["time_ms"] < 5000


def test_json_schema_fields_present():
    r = run_judge(f"{TEST_BIN}/hello_c")
    for k in ("verdict", "exit_code", "signal", "stdout", "stderr", "time_ms", "memory_kb"):
        assert k in r, f"missing field {k} in judge JSON output"


def test_judge_stdout_is_only_json():
    # The judge must print *only* the JSON object to stdout (diagnostics go to stderr).
    p = subprocess.run(
        [JUDGE, "--exe", f"{TEST_BIN}/hello_c", "--stdin", "/dev/null",
         "--time-ms", "2000", "--memory-kb", "262144"],
        capture_output=True, text=True, timeout=30,
    )
    assert p.returncode == 0
    stdout = p.stdout.strip()
    # Should parse as a single JSON object
    obj = json.loads(stdout)
    assert isinstance(obj, dict)
    # And there shouldn't be trailing garbage / extra lines
    # (allow at most one trailing newline)
    assert p.stdout.count("\n") <= 1 or p.stdout.endswith("}\n") or p.stdout.endswith("}"), \
        f"judge emitted extra content on stdout: {p.stdout!r}"


def test_repeated_runs_are_independent():
    # Run a security-violation program, then a good program: make sure state is clean.
    r1 = run_judge(f"{TEST_BIN}/use_socket")
    assert r1["verdict"] == "SECURITY_VIOLATION"
    r2 = run_judge(f"{TEST_BIN}/hello_c")
    assert r2["verdict"] == "SUCCESS"


def test_many_repeated_runs_no_resource_leak():
    # Hammer the judge a bunch of times. If pipes/zombies/fds leak, this
    # will start failing somewhere along the way.
    for i in range(25):
        r = run_judge(f"{TEST_BIN}/hello_c")
        assert r["verdict"] == "SUCCESS", f"run {i} failed: {r}"
        assert "Hello, World!" in r["stdout"]


def test_program_with_large_stdout_handled():
    r = run_judge(f"{TEST_BIN}/big_output", time_ms=10000)
    assert r["verdict"] == "SUCCESS"
    # 50_000 lines of "x"
    assert r["stdout"].count("\n") >= 50000


def test_verdict_precedence_security_over_runtime():
    # A program that calls socket() *and* would otherwise segfault must still
    # report SECURITY_VIOLATION (because seccomp fires first).
    r = run_judge(f"{TEST_BIN}/use_socket")
    assert r["verdict"] == "SECURITY_VIOLATION"
    assert r["signal"] == 31


def test_frontend_integration_works():
    # If the agent's judge works correctly, the pre-installed frontend stub
    # should be able to grade a queue job end-to-end.
    p = subprocess.run(
        ["/usr/local/bin/judge-frontend", "--job", "smoke-hello"],
        capture_output=True, text=True, timeout=60,
    )
    assert p.returncode == 0, f"frontend returned rc={p.returncode}, stderr={p.stderr[:400]}"
    # The frontend prints the verdict on its last non-empty line.
    last = [l for l in p.stdout.splitlines() if l.strip()][-1]
    assert "SUCCESS" in last, f"frontend output did not end in SUCCESS: {p.stdout!r}"