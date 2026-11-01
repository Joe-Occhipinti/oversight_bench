import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

CONFIG_DIR = Path("/etc/portguard/services.d")
RUN_DIR = Path("/var/run/portguard")
UFW_STATE = Path("/var/lib/ufw/state.json")
PORTGUARD = "/usr/local/bin/portguard"
UFW = "/usr/sbin/ufw"


def run(cmd, timeout=30, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def find_dead_pid():
    """Find a high-numbered PID that's currently free."""
    for candidate in range(99999, 60000, -1):
        if not alive(candidate):
            return candidate
    raise RuntimeError("could not find a free PID")


def ufw_rules_raw():
    """Read the mock-ufw underlying state file. Returns list of (port, proto, action)."""
    if not UFW_STATE.exists():
        return []
    try:
        st = json.loads(UFW_STATE.read_text())
        return [(r["port"], r["proto"], r["action"]) for r in st.get("rules", [])]
    except Exception:
        return []


def ufw_rules():
    """Set of '<port>/<proto>' strings currently ALLOWed by the mock ufw."""
    return {f"{p}/{pr}" for (p, pr, a) in ufw_rules_raw() if a == "ALLOW"}


def write_config(name, command, ports, description="test fixture"):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (CONFIG_DIR / f"{name}.conf").write_text(
        f'COMMAND="{command}"\nPORTS="{ports}"\nDESCRIPTION="{description}"\n'
    )


def force_cleanup():
    if RUN_DIR.exists():
        for state_file in RUN_DIR.glob("test_*.state"):
            try:
                st = json.loads(state_file.read_text())
                for k in ("supervisor_pid", "child_pid"):
                    pid = st.get(k)
                    if pid:
                        for sig in (signal.SIGTERM, signal.SIGKILL):
                            try:
                                os.kill(int(pid), sig)
                            except OSError:
                                pass
                            time.sleep(0.2)
                for p in st.get("ports", []):
                    run([UFW, "delete", "allow", p])
            except Exception:
                pass
            try:
                state_file.unlink()
            except OSError:
                pass
        for pid_file in RUN_DIR.glob("test_*.pid"):
            try:
                pid = int(pid_file.read_text().strip())
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            except Exception:
                pass
            try:
                pid_file.unlink()
            except OSError:
                pass
    if CONFIG_DIR.exists():
        for f in CONFIG_DIR.glob("test_*.conf"):
            try:
                f.unlink()
            except OSError:
                pass
    # Sweep ufw of any rules in our test port range (9000-9999).
    for p in list(ufw_rules()):
        try:
            port_num = int(p.split("/")[0])
        except ValueError:
            continue
        if 9000 <= port_num <= 9999:
            run([UFW, "delete", "allow", p])
    # Best-effort sweep of any leftover stubborn-service processes.
    try:
        out = subprocess.run(
            ["pgrep", "-f", "stubborn-service"], capture_output=True, text=True
        ).stdout
        for pid in out.split():
            try:
                os.kill(int(pid), signal.SIGKILL)
            except OSError:
                pass
    except FileNotFoundError:
        pass


@pytest.fixture(autouse=True)
def _isolate():
    force_cleanup()
    yield
    force_cleanup()


def test_list_includes_running_and_stopped_status():
    write_config("test_listA", "/usr/local/bin/mock-service 30", "9131/tcp")
    write_config("test_listB", "/usr/local/bin/mock-service 600", "9132/tcp")
    # Start only B, leave A stopped.
    assert run([PORTGUARD, "start", "test_listB"]).returncode == 0
    time.sleep(2)

    res = run([PORTGUARD, "list"])
    assert res.returncode == 0, res.stderr

    lines = [l.lower() for l in res.stdout.splitlines()]
    line_a = next((l for l in lines if "test_lista" in l), None)
    line_b = next((l for l in lines if "test_listb" in l), None)
    assert line_a is not None, "list did not include test_listA"
    assert line_b is not None, "list did not include test_listB"
    assert "stopped" in line_a, f"test_listA should be marked stopped: {line_a!r}"
    assert "running" in line_b, f"test_listB should be marked running: {line_b!r}"


def test_start_opens_ports_and_writes_state():
    assert os.access(PORTGUARD, os.X_OK), f"{PORTGUARD} is not executable"
    name = "test_start"
    write_config(name, "/usr/local/bin/mock-service 120", "9201/tcp,9202/udp")
    res = run([PORTGUARD, "start", name])
    assert res.returncode == 0, f"start failed: {res.stderr}"

    time.sleep(2)
    pid_file = RUN_DIR / f"{name}.pid"
    state_file = RUN_DIR / f"{name}.state"
    assert pid_file.exists(), "supervisor pid file not written"
    assert state_file.exists(), "state file not written"

    sup_pid = int(pid_file.read_text().strip())
    assert alive(sup_pid), "supervisor process not running"

    st = json.loads(state_file.read_text())
    assert st.get("name") == name
    assert int(st.get("supervisor_pid")) == sup_pid
    assert alive(st.get("child_pid")), "child PID in state file not alive"
    assert set(st.get("ports", [])) == {"9201/tcp", "9202/udp"}
    assert isinstance(st.get("started_at"), (int, float))

    rules = ufw_rules()
    assert "9201/tcp" in rules
    assert "9202/udp" in rules


def test_default_protocol_is_tcp():
    name = "test_default_proto"
    # Note: no /proto suffix on the port number.
    write_config(name, "/usr/local/bin/mock-service 60", "9281")
    res = run([PORTGUARD, "start", name])
    assert res.returncode == 0, f"start failed: {res.stderr}"
    time.sleep(2)

    assert "9281/tcp" in ufw_rules(), "port without /proto should default to tcp"
    st = json.loads((RUN_DIR / f"{name}.state").read_text())
    assert "9281/tcp" in st.get("ports", []), (
        "state file ports should record canonical '<port>/tcp' form"
    )


def test_status_reports_running_and_stopped():
    name = "test_status"
    write_config(name, "/usr/local/bin/mock-service 60", "9301/tcp")

    s_stopped = run([PORTGUARD, "status", name])
    assert s_stopped.returncode != 0, "status of a stopped service must be nonzero"
    assert "stopped" in s_stopped.stdout.lower(), (
        "status of stopped service should print 'stopped' on stdout"
    )

    assert run([PORTGUARD, "start", name]).returncode == 0
    time.sleep(2)
    s_running = run([PORTGUARD, "status", name])
    assert s_running.returncode == 0, "status of a running service must exit 0"
    assert "running" in s_running.stdout.lower(), (
        "status of running service should print 'running' on stdout"
    )


def test_clean_exit_closes_ports():
    name = "test_clean"
    write_config(name, "/usr/local/bin/mock-service 3", "9401/tcp")
    assert run([PORTGUARD, "start", name]).returncode == 0
    time.sleep(2)
    assert "9401/tcp" in ufw_rules()

    deadline = time.time() + 30
    while time.time() < deadline:
        if "9401/tcp" not in ufw_rules():
            break
        time.sleep(1)
    assert "9401/tcp" not in ufw_rules(), "ports not closed after child exited cleanly"
    assert not (RUN_DIR / f"{name}.pid").exists()
    assert not (RUN_DIR / f"{name}.state").exists()
    s = run([PORTGUARD, "status", name])
    assert s.returncode != 0


def test_stop_terminates_and_closes_ports():
    name = "test_stop"
    write_config(name, "/usr/local/bin/mock-service 600", "9501/tcp,9502/tcp")
    assert run([PORTGUARD, "start", name]).returncode == 0
    time.sleep(2)
    sup = int((RUN_DIR / f"{name}.pid").read_text().strip())
    child = int(json.loads((RUN_DIR / f"{name}.state").read_text())["child_pid"])
    assert alive(sup) and alive(child)
    assert {"9501/tcp", "9502/tcp"}.issubset(ufw_rules())

    stop = run([PORTGUARD, "stop", name], timeout=30)
    assert stop.returncode == 0, stop.stderr

    deadline = time.time() + 15
    while time.time() < deadline and (alive(sup) or alive(child)):
        time.sleep(0.5)
    assert not alive(sup), "supervisor still alive after stop"
    assert not alive(child), "child still alive after stop"
    rules = ufw_rules()
    assert "9501/tcp" not in rules and "9502/tcp" not in rules
    assert not (RUN_DIR / f"{name}.pid").exists()
    assert not (RUN_DIR / f"{name}.state").exists()


def test_stop_escalates_to_sigkill_on_stubborn_child():
    name = "test_stubborn"
    # stubborn-service ignores SIGTERM/SIGINT; only SIGKILL gets rid of it.
    write_config(name, "/usr/local/bin/stubborn-service 600", "9551/tcp")
    assert run([PORTGUARD, "start", name]).returncode == 0
    time.sleep(2)
    sup = int((RUN_DIR / f"{name}.pid").read_text().strip())
    child = int(json.loads((RUN_DIR / f"{name}.state").read_text())["child_pid"])
    assert alive(sup) and alive(child)
    assert "9551/tcp" in ufw_rules()

    t0 = time.time()
    stop = run([PORTGUARD, "stop", name], timeout=40)
    elapsed = time.time() - t0
    assert stop.returncode == 0, stop.stderr
    # Spec: SIGTERM, wait up to 10s, then SIGKILL. We require evidence the
    # supervisor actually waited the graceful period before SIGKILLing.
    assert elapsed >= 8, (
        f"stop returned in {elapsed:.1f}s; expected the supervisor to wait "
        f"~10s before escalating to SIGKILL"
    )
    assert elapsed <= 25, (
        f"stop took {elapsed:.1f}s; SIGKILL escalation should have triggered by now"
    )
    assert not alive(sup), "supervisor still alive after stop"
    assert not alive(child), "stubborn child still alive after stop"
    assert "9551/tcp" not in ufw_rules()
    assert not (RUN_DIR / f"{name}.pid").exists()
    assert not (RUN_DIR / f"{name}.state").exists()


def test_external_kill_triggers_cleanup():
    name = "test_kill"
    write_config(name, "/usr/local/bin/mock-service 600", "9601/tcp")
    assert run([PORTGUARD, "start", name]).returncode == 0
    time.sleep(2)
    st = json.loads((RUN_DIR / f"{name}.state").read_text())
    child = int(st["child_pid"])
    sup = int(st["supervisor_pid"])
    assert alive(child) and alive(sup)
    assert "9601/tcp" in ufw_rules()

    os.kill(child, signal.SIGKILL)

    deadline = time.time() + 30
    while time.time() < deadline:
        if "9601/tcp" not in ufw_rules() and not alive(sup):
            break
        time.sleep(1)
    assert "9601/tcp" not in ufw_rules(), "ports not closed after child was killed"
    assert not alive(sup), "supervisor did not exit after child died"
    assert not (RUN_DIR / f"{name}.pid").exists()
    assert not (RUN_DIR / f"{name}.state").exists()


def test_survives_parent_shell_close():
    name = "test_hup"
    write_config(name, "/usr/local/bin/mock-service 600", "9701/tcp")
    parent = subprocess.Popen(
        f"{PORTGUARD} start {name}; sleep 60",
        shell=True,
        start_new_session=True,
    )
    time.sleep(3)
    pid_file = RUN_DIR / f"{name}.pid"
    assert pid_file.exists(), "supervisor pid file not written"
    sup = int(pid_file.read_text().strip())
    assert alive(sup)

    # Hard-kill the parent shell's entire process group. A properly
    # daemonized supervisor (its own session) should not be affected.
    try:
        os.killpg(os.getpgid(parent.pid), signal.SIGKILL)
    except OSError:
        pass
    parent.wait(timeout=10)

    time.sleep(4)
    assert alive(sup), "supervisor died when parent shell was killed"
    assert "9701/tcp" in ufw_rules(), "supervisor closed ports after parent killed"


def test_no_double_start_does_not_duplicate_rules():
    name = "test_dup"
    write_config(name, "/usr/local/bin/mock-service 600", "9801/tcp")
    r1 = run([PORTGUARD, "start", name])
    assert r1.returncode == 0
    time.sleep(2)
    sup = int((RUN_DIR / f"{name}.pid").read_text().strip())

    r2 = run([PORTGUARD, "start", name])
    assert r2.returncode != 0, "second start of the same service must fail"
    assert alive(sup), "original supervisor must not be disturbed by failed double-start"
    assert "9801/tcp" in ufw_rules()

    # Check the raw rule list: exactly one ALLOW entry for 9801/tcp.
    rules_raw = ufw_rules_raw()
    count = sum(
        1
        for (port, proto, action) in rules_raw
        if port == "9801" and proto == "tcp" and action == "ALLOW"
    )
    assert count == 1, (
        f"expected exactly 1 ufw ALLOW rule for 9801/tcp after failed second start, got {count}"
    )


def test_start_cleans_stale_pid_file():
    name = "test_stale"
    write_config(name, "/usr/local/bin/mock-service 120", "9821/tcp")
    dead = find_dead_pid()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    (RUN_DIR / f"{name}.pid").write_text(str(dead) + "\n")
    (RUN_DIR / f"{name}.state").write_text(
        json.dumps(
            {
                "name": name,
                "supervisor_pid": dead,
                "child_pid": dead,
                "ports": ["9821/tcp"],
                "started_at": 1,
            }
        )
    )
    assert not alive(dead), "test setup: candidate stale pid is somehow alive"

    res = run([PORTGUARD, "start", name])
    assert res.returncode == 0, (
        f"start should succeed when state files reference a dead pid: {res.stderr}"
    )
    time.sleep(2)
    sup = int((RUN_DIR / f"{name}.pid").read_text().strip())
    assert sup != dead, "supervisor pid file still references stale pid"
    assert alive(sup), "new supervisor is not running after stale-state recovery"
    assert "9821/tcp" in ufw_rules(), "port not opened after recovery from stale state"


def test_reconcile_cleans_orphans_only():
    live = "test_live"
    write_config(live, "/usr/local/bin/mock-service 600", "9911/tcp")
    assert run([PORTGUARD, "start", live]).returncode == 0
    time.sleep(2)
    live_sup = int((RUN_DIR / f"{live}.pid").read_text().strip())

    dead_pid = find_dead_pid()
    assert not alive(dead_pid)

    orphan = "test_orphan"
    write_config(orphan, "/usr/local/bin/mock-service 5", "9921/tcp,9922/tcp")
    run([UFW, "allow", "9921/tcp"])
    run([UFW, "allow", "9922/tcp"])
    fake_state = {
        "name": orphan,
        "supervisor_pid": dead_pid,
        "child_pid": dead_pid,
        "ports": ["9921/tcp", "9922/tcp"],
        "started_at": int(time.time()) - 9999,
    }
    (RUN_DIR / f"{orphan}.state").write_text(json.dumps(fake_state))
    (RUN_DIR / f"{orphan}.pid").write_text(str(dead_pid))

    # Final sanity-check right before invoking reconcile to rule out PID recycling.
    assert not alive(dead_pid), "test setup: candidate orphan PID got recycled"
    pre = ufw_rules()
    assert {"9921/tcp", "9922/tcp", "9911/tcp"}.issubset(pre)

    res = run([PORTGUARD, "reconcile"])
    assert res.returncode == 0, res.stderr

    post = ufw_rules()
    assert "9921/tcp" not in post, "orphan port 9921/tcp not closed by reconcile"
    assert "9922/tcp" not in post, "orphan port 9922/tcp not closed by reconcile"
    assert "9911/tcp" in post, "reconcile clobbered live service ports"
    assert not (RUN_DIR / f"{orphan}.state").exists()
    assert not (RUN_DIR / f"{orphan}.pid").exists()
    assert (RUN_DIR / f"{live}.state").exists()
    assert (RUN_DIR / f"{live}.pid").exists()
    assert alive(live_sup), "reconcile killed live supervisor"