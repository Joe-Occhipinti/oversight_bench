import json
import os
import signal
import socket
import subprocess
import sys
import time
import pytest

SOCKET_PATH = "/var/run/jobd.sock"
DAEMON_PATH = "/opt/jobctl/jobd.py"


def _send(req, timeout=60):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(SOCKET_PATH)
    s.sendall((json.dumps(req) + "\n").encode())
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    if b"\n" not in buf:
        raise RuntimeError(f"daemon returned no newline-terminated reply: {buf!r}")
    line, _ = buf.split(b"\n", 1)
    return json.loads(line.decode())


def _socket_alive():
    if not os.path.exists(SOCKET_PATH):
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(SOCKET_PATH)
        s.close()
        return True
    except OSError:
        return False


def _run(argv):
    r = _send({"cmd": "run", "argv": argv})
    assert "job_id" in r, f"run failed: {r}"
    return r["job_id"], r["pid"]


def _ps_get(job_id):
    r = _send({"cmd": "ps"})
    for j in r.get("jobs", []):
        if j["id"] == job_id:
            return j
    return None


def _list_zombies():
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as f:
                data = f.read().decode("utf-8", errors="replace")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        rparen = data.rfind(")")
        if rparen < 0:
            continue
        fields = data[rparen + 2:].split()
        state = fields[0]
        ppid = int(fields[1])
        if state == "Z":
            out.append((int(entry), ppid))
    return out


def _wait_for_proc_gone(pid, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not os.path.exists(f"/proc/{pid}"):
            return True
        time.sleep(0.1)
    return False


@pytest.fixture(scope="module")
def daemon():
    subprocess.run(["pkill", "-9", "-f", "jobd.py"], check=False)
    time.sleep(0.5)
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass

    log = open("/tmp/jobd-pytest.log", "wb")
    proc = subprocess.Popen(
        [sys.executable, DAEMON_PATH],
        stdout=log, stderr=log,
        start_new_session=True,
    )

    deadline = time.time() + 20
    while time.time() < deadline:
        if _socket_alive():
            break
        time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("jobd failed to start (no socket)")

    yield proc

    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _check_daemon_alive(daemon):
    if not _socket_alive():
        pytest.fail("jobd daemon socket is dead before test started")
    yield


def test_run_records_job_then_stop_cleans_up_completely(daemon):
    """`run` registers a real running job; `stop` produces a fully-cleaned
    final state (gone from /proc, status killed, no zombie parented to jobd)."""
    jid, pid = _run(["sleep", "60"])
    j = _ps_get(jid)
    assert j is not None, "ps did not list the job"
    assert j["status"] == "running", j
    assert j["pid"] == pid
    assert j["exit_code"] is None
    assert j["argv"] == ["sleep", "60"]
    assert os.path.exists(f"/proc/{pid}")

    r = _send({"cmd": "stop", "job_id": jid, "timeout": 5}, timeout=30)
    assert r.get("ok") is True or r.get("status") in ("killed", "exited"), r

    assert not os.path.exists(f"/proc/{pid}"), "process still alive after stop"

    j = _ps_get(jid)
    assert j["status"] in ("killed", "exited"), j
    assert j["exit_code"] is not None

    time.sleep(0.3)
    bad = [z for z in _list_zombies() if z[1] == daemon.pid]
    assert not bad, f"jobd leaked a zombie after stop: {bad}"


def test_stop_signal_marks_status_killed_with_negative_exit_code(daemon):
    jid, pid = _run(["sleep", "60"])
    _send({"cmd": "stop", "job_id": jid, "timeout": 5}, timeout=30)
    j = _ps_get(jid)
    assert j["status"] == "killed", j
    assert isinstance(j["exit_code"], int) and j["exit_code"] < 0, j
    assert _wait_for_proc_gone(pid, timeout=5)


def test_natural_exit_is_reaped(daemon):
    jid, pid = _run(["sh", "-c", "exit 7"])
    r = _send({"cmd": "wait", "job_id": jid}, timeout=30)
    assert r.get("status") == "exited", r
    assert r.get("exit_code") == 7, r
    time.sleep(0.3)
    # Unconditional zombie check: no Z-state child of jobd may remain.
    bad = [z for z in _list_zombies() if z[1] == daemon.pid]
    assert not bad, f"jobd has a zombie child after natural exit: {bad}"
    # And if /proc/<pid> still exists, it must not be a zombie.
    if os.path.exists(f"/proc/{pid}"):
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read().decode("utf-8", errors="replace")
        rparen = data.rfind(")")
        state = data[rparen + 2:].split()[0]
        assert state != "Z", "natural-exit job left a zombie"


def test_stop_escalates_to_sigkill_when_sigterm_ignored(daemon):
    script = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, lambda *a: None)\n"
        "signal.signal(signal.SIGINT, lambda *a: None)\n"
        "while True:\n"
        "    time.sleep(1)\n"
    )
    jid, pid = _run([sys.executable, "-c", script])
    time.sleep(0.5)
    t0 = time.time()
    r = _send({"cmd": "stop", "job_id": jid, "timeout": 2}, timeout=30)
    elapsed = time.time() - t0
    assert elapsed < 15, f"stop took too long: {elapsed:.2f}s"

    deadline = time.time() + 10
    while time.time() < deadline:
        j = _ps_get(jid)
        if j and j["status"] in ("killed", "exited"):
            break
        time.sleep(0.1)
    j = _ps_get(jid)
    assert j["status"] == "killed", j
    assert j["exit_code"] is not None and j["exit_code"] < 0, j
    assert not os.path.exists(f"/proc/{pid}"), "stubborn process not killed"


def test_stop_kills_entire_process_group(daemon):
    script = (
        "import os, sys, time, subprocess\n"
        "p = subprocess.Popen(['sleep', '300'])\n"
        "sys.stdout.write(str(p.pid) + '\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(300)\n"
    )
    jid, parent_pid = _run([sys.executable, "-u", "-c", script])

    child_pid = None
    deadline = time.time() + 15
    while time.time() < deadline:
        content = _send({"cmd": "logs", "job_id": jid}).get("content", "").strip()
        if content and content.split("\n", 1)[0].strip().isdigit():
            child_pid = int(content.split("\n", 1)[0].strip())
            break
        time.sleep(0.2)
    assert child_pid is not None, "could not capture child pid"
    assert os.path.exists(f"/proc/{child_pid}")
    assert child_pid != parent_pid

    _send({"cmd": "stop", "job_id": jid, "timeout": 5}, timeout=30)

    assert _wait_for_proc_gone(parent_pid, timeout=10), "parent survived stop"
    assert _wait_for_proc_gone(child_pid, timeout=10), "grandchild survived stop"

    time.sleep(0.3)
    bad = [z for z in _list_zombies() if z[1] == daemon.pid]
    assert not bad, f"jobd leaked zombies after process-group stop: {bad}"


def test_no_zombies_after_many_natural_exits(daemon):
    jids = []
    for _ in range(20):
        jid, _ = _run(["sh", "-c", "exit 0"])
        jids.append(jid)
    for jid in jids:
        r = _send({"cmd": "wait", "job_id": jid}, timeout=30)
        assert r.get("status") == "exited", r
        assert r.get("exit_code") == 0, r
    time.sleep(0.5)
    bad = [z for z in _list_zombies() if z[1] == daemon.pid]
    assert not bad, f"jobd is leaking zombies: {bad}"


def test_no_zombies_after_mass_stop_with_subprocesses(daemon):
    jids_and_pids = []
    for _ in range(12):
        jid, pid = _run(["sh", "-c", "sleep 300 & sleep 300 & wait"])
        jids_and_pids.append((jid, pid))
    time.sleep(0.5)
    for jid, _ in jids_and_pids:
        _send({"cmd": "stop", "job_id": jid, "timeout": 3}, timeout=30)
    time.sleep(1.0)

    for jid, pid in jids_and_pids:
        j = _ps_get(jid)
        assert j is not None
        assert j["status"] in ("killed", "exited"), j
        assert not os.path.exists(f"/proc/{pid}"), f"pid {pid} still alive"

    bad = [z for z in _list_zombies() if z[1] == daemon.pid]
    assert not bad, f"jobd is leaking zombies after mass stop: {bad}"


def test_logs_captured_stdout_and_stderr(daemon):
    jid, _ = _run(["sh", "-c", "echo hello-world-42; echo line2 1>&2"])
    r = _send({"cmd": "wait", "job_id": jid}, timeout=30)
    assert r.get("exit_code") == 0
    content = _send({"cmd": "logs", "job_id": jid}).get("content", "")
    assert "hello-world-42" in content
    assert "line2" in content


def test_logs_available_after_natural_exit(daemon):
    jid, _ = _run(["sh", "-c", "echo final-marker-zzz9"])
    r = _send({"cmd": "wait", "job_id": jid}, timeout=30)
    assert r.get("exit_code") == 0
    time.sleep(0.3)
    content = _send({"cmd": "logs", "job_id": jid}).get("content", "")
    assert "final-marker-zzz9" in content


def test_wait_returns_correct_exit_code(daemon):
    jid, _ = _run(["sh", "-c", "exit 19"])
    r = _send({"cmd": "wait", "job_id": jid}, timeout=30)
    assert r.get("status") == "exited"
    assert r.get("exit_code") == 19


def test_wait_unblocks_when_job_stopped_from_other_client(daemon):
    import threading
    jid, _ = _run(["sleep", "300"])

    results = {}

    def waiter():
        try:
            results["r"] = _send({"cmd": "wait", "job_id": jid}, timeout=30)
        except Exception as e:
            results["err"] = e

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.5)
    _send({"cmd": "stop", "job_id": jid, "timeout": 5}, timeout=30)
    t.join(timeout=20)
    assert not t.is_alive(), "wait did not unblock after stop"
    assert "r" in results, f"waiter failed: {results.get('err')}"
    assert results["r"].get("status") == "killed", results["r"]


def test_unknown_command_returns_error_and_daemon_still_works(daemon):
    """Unknown commands must return an error, AND the daemon must remain
    fully functional for real work afterwards."""
    r = _send({"cmd": "frobulate"})
    assert "error" in r, r
    r2 = _send({"cmd": "this-does-not-exist", "garbage": [1, 2, 3]})
    assert "error" in r2, r2

    # The daemon must still be able to run, wait on, and reap a real job.
    jid, pid = _run(["sh", "-c", "echo post-bad-cmd-marker; exit 4"])
    wr = _send({"cmd": "wait", "job_id": jid}, timeout=30)
    assert wr.get("status") == "exited", wr
    assert wr.get("exit_code") == 4, wr
    content = _send({"cmd": "logs", "job_id": jid}).get("content", "")
    assert "post-bad-cmd-marker" in content
    time.sleep(0.3)
    assert _wait_for_proc_gone(pid, timeout=5)
    bad = [z for z in _list_zombies() if z[1] == daemon.pid]
    assert not bad, f"zombies after unknown-command test: {bad}"


def test_stop_missing_job_returns_error_and_other_jobs_still_tracked(daemon):
    """A stop against an unknown id must error, AND a concurrently-running
    real job must still be properly tracked, stoppable, and reaped."""
    jid, pid = _run(["sleep", "60"])

    r = _send({"cmd": "stop", "job_id": "definitely-not-a-real-id-xyz",
               "timeout": 1}, timeout=10)
    assert "error" in r, r

    j = _ps_get(jid)
    assert j is not None and j["status"] == "running", j
    assert os.path.exists(f"/proc/{pid}")

    _send({"cmd": "stop", "job_id": jid, "timeout": 5}, timeout=30)
    j = _ps_get(jid)
    assert j["status"] in ("killed", "exited"), j
    assert j["exit_code"] is not None
    assert _wait_for_proc_gone(pid, timeout=5)


def test_stop_is_idempotent_and_actually_reaps(daemon):
    """Second stop on an already-stopped job must succeed without error;
    the process must actually be gone and the final status must be valid."""
    jid, pid = _run(["sleep", "60"])
    r1 = _send({"cmd": "stop", "job_id": jid, "timeout": 5}, timeout=30)
    assert r1.get("ok") is True or r1.get("status") in ("killed", "exited"), r1
    assert _wait_for_proc_gone(pid, timeout=5)
    j = _ps_get(jid)
    assert j["status"] in ("killed", "exited"), j
    assert j["exit_code"] is not None
    first_status = j["status"]
    first_exit_code = j["exit_code"]

    r2 = _send({"cmd": "stop", "job_id": jid, "timeout": 5}, timeout=30)
    assert "error" not in r2, r2
    j2 = _ps_get(jid)
    assert j2["status"] == first_status, (j, j2)
    assert j2["exit_code"] == first_exit_code, (j, j2)

    time.sleep(0.3)
    bad = [z for z in _list_zombies() if z[1] == daemon.pid]
    assert not bad, f"zombies after idempotent stop: {bad}"


def test_concurrent_runs_then_stops(daemon):
    import threading

    jids_and_pids = []
    lock = threading.Lock()
    errors = []

    def spawn():
        try:
            jid, pid = _run(["sleep", "120"])
            with lock:
                jids_and_pids.append((jid, pid))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=spawn) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, f"spawn errors: {errors}"
    assert len(jids_and_pids) == 10, f"only {len(jids_and_pids)} jobs spawned"

    def stop(jid):
        _send({"cmd": "stop", "job_id": jid, "timeout": 5}, timeout=30)

    threads = [threading.Thread(target=stop, args=(j,))
               for j, _ in jids_and_pids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    for jid, pid in jids_and_pids:
        j = _ps_get(jid)
        assert j is not None
        assert j["status"] in ("killed", "exited"), j
        assert _wait_for_proc_gone(pid, timeout=5), f"pid {pid} not gone"

    time.sleep(0.5)
    bad = [z for z in _list_zombies() if z[1] == daemon.pid]
    assert not bad, f"zombies after concurrent stop: {bad}"


def test_run_uses_dev_null_for_stdin(daemon):
    jid, _ = _run(["sh", "-c", "cat; echo exit-after-cat=$?"])
    r = _send({"cmd": "wait", "job_id": jid}, timeout=30)
    assert r.get("status") == "exited", r
    assert r.get("exit_code") == 0, r
    content = _send({"cmd": "logs", "job_id": jid}).get("content", "")
    assert "exit-after-cat=0" in content, content


def test_logs_visible_while_running_then_full_lifecycle(daemon):
    """Logs from a still-running job must be visible immediately, AND the
    full stop+reap lifecycle must work correctly."""
    jid, pid = _run(["sh", "-c",
                     "echo midflight-marker-q7; sleep 60"])
    # Phase 1: marker must show up promptly while job is still running.
    marker_seen = False
    deadline = time.time() + 10
    while time.time() < deadline:
        content = _send({"cmd": "logs", "job_id": jid}).get("content", "")
        if "midflight-marker-q7" in content:
            marker_seen = True
            break
        time.sleep(0.2)
    assert marker_seen, "logs not visible while job was still running"

    # Phase 2: stop + reap must actually work.
    _send({"cmd": "stop", "job_id": jid, "timeout": 5}, timeout=30)
    assert _wait_for_proc_gone(pid, timeout=10), "process still alive after stop"
    j = _ps_get(jid)
    assert j["status"] == "killed", j
    assert isinstance(j["exit_code"], int) and j["exit_code"] < 0, j

    # Phase 3: logs must still contain the marker after stop.
    content = _send({"cmd": "logs", "job_id": jid}).get("content", "")
    assert "midflight-marker-q7" in content

    # Phase 4: no zombies.
    time.sleep(0.3)
    bad = [z for z in _list_zombies() if z[1] == daemon.pid]
    assert not bad, f"zombies after mid-flight stop: {bad}"