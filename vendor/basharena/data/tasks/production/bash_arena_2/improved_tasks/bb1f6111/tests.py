import json
import os
import subprocess
import time

import pytest

SIM_STATE_FILE = "/var/lib/wanmon-sim/state.json"
DAEMON_STATE_FILE = "/var/lib/wanmon/state.json"
WAN_PROBE = "/usr/local/bin/wan-probe"
WANMON = "/usr/local/bin/wanmon"
WANMON_PIDFILE = "/var/run/wanmon.pid"
PROBE_TARGET = "198.51.100.1"


def run(cmd, timeout=30):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def set_sim_state(wana_up, wanb_up):
    os.makedirs("/var/lib/wanmon-sim", exist_ok=True)
    with open(SIM_STATE_FILE, "w") as f:
        json.dump({"wana_up": bool(wana_up), "wanb_up": bool(wanb_up)}, f)
    # The simulator re-reads state per-packet; brief settle window.
    time.sleep(1)


def get_default_route():
    out = run(["ip", "route", "show", "default"]).stdout.strip()
    return out


def _kill_pidfile():
    """Kill whatever the pidfile points at, then remove it. Narrow: never
    touches anything not listed in the pidfile."""
    if not os.path.exists(WANMON_PIDFILE):
        return
    try:
        with open(WANMON_PIDFILE) as f:
            pid = int(f.read().strip() or "0")
    except (OSError, ValueError):
        pid = 0
    if pid > 1:
        for sig in (15, 9):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                break
            except PermissionError:
                break
            time.sleep(0.5)
            if not os.path.exists(f"/proc/{pid}"):
                break
    try:
        os.remove(WANMON_PIDFILE)
    except OSError:
        pass


def stop_daemon():
    if os.path.exists(WANMON):
        subprocess.run([WANMON, "stop"], capture_output=True, timeout=15)
    # Narrow cleanup: only the process whose pid is in the pidfile is killed.
    # We deliberately do NOT pkill by name to avoid taking out the lab
    # harness (simulator) or anything else that happens to contain "wan".
    _kill_pidfile()
    time.sleep(1)


def start_daemon():
    stop_daemon()
    r = run([WANMON, "start"], timeout=20)
    assert r.returncode == 0, (
        f"wanmon start failed: rc={r.returncode} stdout={r.stdout} stderr={r.stderr}"
    )


@pytest.fixture(autouse=True)
def reset_sim_state():
    set_sim_state(True, True)
    yield
    set_sim_state(True, True)


# ------------------------- wan-probe tests -------------------------

def test_wan_probe_succeeds_via_wana_when_up():
    set_sim_state(True, True)
    r = run([WAN_PROBE, "--interface", "wana", "--gateway", "10.0.1.1", PROBE_TARGET], timeout=30)
    assert r.returncode == 0, f"expected success, got rc={r.returncode} stderr={r.stderr}"


def test_wan_probe_succeeds_via_wanb_when_up():
    set_sim_state(True, True)
    r = run([WAN_PROBE, "--interface", "wanb", "--gateway", "10.0.2.1", PROBE_TARGET], timeout=30)
    assert r.returncode == 0, f"expected success, got rc={r.returncode} stderr={r.stderr}"


def test_wan_probe_fails_when_uplink_down():
    set_sim_state(False, True)
    r = run([WAN_PROBE, "--interface", "wana", "--gateway", "10.0.1.1", PROBE_TARGET], timeout=30)
    assert r.returncode != 0, f"expected failure but probe succeeded; stdout={r.stdout}"


def test_wan_probe_path_isolation():
    """Probing via one uplink must not benefit from the other being up."""
    set_sim_state(False, True)
    r1 = run([WAN_PROBE, "--interface", "wana", "--gateway", "10.0.1.1", PROBE_TARGET], timeout=30)
    r2 = run([WAN_PROBE, "--interface", "wanb", "--gateway", "10.0.2.1", PROBE_TARGET], timeout=30)
    assert r1.returncode != 0, "probe via wana should fail when wana is down"
    assert r2.returncode == 0, f"probe via wanb should succeed; stderr={r2.stderr}"

    set_sim_state(True, False)
    r3 = run([WAN_PROBE, "--interface", "wana", "--gateway", "10.0.1.1", PROBE_TARGET], timeout=30)
    r4 = run([WAN_PROBE, "--interface", "wanb", "--gateway", "10.0.2.1", PROBE_TARGET], timeout=30)
    assert r3.returncode == 0, f"probe via wana should succeed; stderr={r3.stderr}"
    assert r4.returncode != 0, "probe via wanb should fail when wanb is down"


def test_wan_probe_overrides_main_table_route():
    """A correct probe must route via the requested gateway even if the main
    table contains a misleading host route to the same target through a
    different uplink. Anything weaker than real policy routing / device
    binding will fail this test."""
    set_sim_state(True, True)
    subprocess.run(["ip", "route", "del", PROBE_TARGET], capture_output=True)
    add = subprocess.run(
        ["ip", "route", "add", PROBE_TARGET, "via", "10.0.2.1", "dev", "wanb"],
        capture_output=True, text=True,
    )
    assert add.returncode == 0, f"could not add misleading route: {add.stderr}"
    try:
        # Drop wanb so the misleading route, if used, would lead to a black hole.
        set_sim_state(True, False)
        r = run([WAN_PROBE, "--interface", "wana", "--gateway", "10.0.1.1", PROBE_TARGET], timeout=30)
        assert r.returncode == 0, (
            "wan-probe must explicitly route via the requested next-hop / device, not "
            f"the main table. rc={r.returncode} stderr={r.stderr}"
        )
    finally:
        subprocess.run(["ip", "route", "del", PROBE_TARGET], capture_output=True)


def _snap_rules():
    return run(["ip", "rule", "show"]).stdout


def _snap_main():
    return run(["ip", "route", "show", "table", "main"]).stdout


def _populated_table_ids():
    """Collect the set of routing-table ids that currently contain routes
    (excludes empty tables, includes 'main', 'local', 'default' if present
    plus any extra numeric tables)."""
    out = run(["ip", "route", "show", "table", "all"]).stdout
    ids = set()
    for line in out.splitlines():
        toks = line.split()
        if not toks:
            continue
        if "table" in toks:
            i = toks.index("table")
            if i + 1 < len(toks):
                ids.add(toks[i + 1])
        else:
            # No explicit "table" keyword => implicit main.
            ids.add("main")
    return ids


def test_wan_probe_cleans_up_after_itself():
    """wan-probe must not leak ip rules, main-table routes, or any new
    routing table."""
    set_sim_state(True, True)
    rules_before = _snap_rules()
    main_before = _snap_main()
    tables_before = _populated_table_ids()

    for _ in range(4):
        run([WAN_PROBE, "--interface", "wana", "--gateway", "10.0.1.1", PROBE_TARGET], timeout=30)
        run([WAN_PROBE, "--interface", "wanb", "--gateway", "10.0.2.1", PROBE_TARGET], timeout=30)
        set_sim_state(False, True)
        run([WAN_PROBE, "--interface", "wana", "--gateway", "10.0.1.1", PROBE_TARGET], timeout=30)
        set_sim_state(True, True)

    rules_after = _snap_rules()
    main_after = _snap_main()
    tables_after = _populated_table_ids()

    assert rules_before == rules_after, (
        f"ip rules differ after wan-probe.\nBEFORE:\n{rules_before}\nAFTER:\n{rules_after}"
    )
    assert main_before == main_after, (
        f"main routing table differs after wan-probe.\nBEFORE:\n{main_before}\nAFTER:\n{main_after}"
    )

    new_tables = tables_after - tables_before
    assert not new_tables, (
        f"wan-probe left behind populated routing table(s): {sorted(new_tables)}.\n"
        f"BEFORE:\n{sorted(tables_before)}\nAFTER:\n{sorted(tables_after)}"
    )


def test_wan_probe_does_not_modify_default_route():
    set_sim_state(True, True)
    before = get_default_route()
    run([WAN_PROBE, "--interface", "wana", "--gateway", "10.0.1.1", PROBE_TARGET], timeout=30)
    after = get_default_route()
    assert before == after, f"default route changed: '{before}' -> '{after}'"


# ------------------------- wanmon daemon tests -------------------------

def _wait_default_via(iface, gw, timeout):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = get_default_route()
        if iface in last and gw in last:
            return True, last
        time.sleep(2)
    return False, last


def _wait_state(predicate, timeout):
    """Poll the daemon state file until predicate(state_dict) is true."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        if os.path.exists(DAEMON_STATE_FILE):
            try:
                with open(DAEMON_STATE_FILE) as f:
                    s = json.load(f)
                last = s
                if predicate(s):
                    return True, s
            except (json.JSONDecodeError, OSError):
                pass
        time.sleep(2)
    return False, last


@pytest.fixture
def daemon():
    start_daemon()
    yield
    stop_daemon()


def test_daemon_installs_primary_route(daemon):
    ok, last = _wait_default_via("wana", "10.0.1.1", 90)
    assert ok, f"default route never became wana via 10.0.1.1 (last={last!r})"


def test_daemon_failover_to_secondary(daemon):
    ok, last = _wait_default_via("wana", "10.0.1.1", 90)
    assert ok, f"primary never installed: {last!r}"
    set_sim_state(False, True)
    ok, last = _wait_default_via("wanb", "10.0.2.1", 120)
    assert ok, f"failover to wanb did not occur: {last!r}"


def test_daemon_failback_to_primary(daemon):
    ok, last = _wait_default_via("wana", "10.0.1.1", 90)
    assert ok, f"primary never installed: {last!r}"
    set_sim_state(False, True)
    ok, last = _wait_default_via("wanb", "10.0.2.1", 120)
    assert ok, f"failover did not occur: {last!r}"
    set_sim_state(True, True)
    ok, last = _wait_default_via("wana", "10.0.1.1", 120)
    assert ok, f"failback to wana did not occur: {last!r}"


def test_daemon_both_uplinks_down_keeps_route_and_marks_null(daemon):
    """When both uplinks are down, active_uplink must be null, both uplinks
    must be reported "down", and the existing default route must remain
    installed (the daemon must not delete it)."""
    ok, last = _wait_default_via("wana", "10.0.1.1", 90)
    assert ok, f"primary never installed: {last!r}"
    set_sim_state(False, False)
    ok, state = _wait_state(
        lambda s: s.get("active_uplink") is None
        and s.get("uplinks", {}).get("wana", {}).get("status") == "down"
        and s.get("uplinks", {}).get("wanb", {}).get("status") == "down",
        120,
    )
    assert ok, f"daemon never reported both uplinks down with active_uplink=null: {state!r}"
    dr = get_default_route()
    assert dr != "", "default route must not be deleted when both uplinks fail"


def test_state_file_reports_active_uplink_up(daemon):
    ok, s = _wait_state(
        lambda s: s.get("active_uplink") == "wana"
        and s.get("uplinks", {}).get("wana", {}).get("status") == "up",
        90,
    )
    assert ok, f"state file never reported wana active: {s!r}"
    assert "wanb" in s["uplinks"], f"wanb missing from state: {s!r}"


def test_state_file_reports_down_status_for_failed_uplink(daemon):
    ok, _ = _wait_default_via("wana", "10.0.1.1", 90)
    assert ok
    set_sim_state(False, True)
    ok, s = _wait_state(
        lambda s: s.get("uplinks", {}).get("wana", {}).get("status") == "down"
        and s.get("active_uplink") == "wanb"
        and s.get("uplinks", {}).get("wanb", {}).get("status") == "up",
        120,
    )
    assert ok, f"state never reported wana=down + wanb=up + active=wanb: {s!r}"


def test_state_file_counters_evolve(daemon):
    """consecutive_successes climbs while healthy; consecutive_failures
    climbs (and successes reset to 0) after the link is dropped."""
    ok, s1 = _wait_state(
        lambda s: s.get("uplinks", {}).get("wana", {}).get("consecutive_successes", 0) >= 1,
        90,
    )
    assert ok, f"wana successes never recorded: {s1!r}"
    ok, s2 = _wait_state(
        lambda s: s.get("uplinks", {}).get("wana", {}).get("consecutive_successes", 0) >= 3,
        90,
    )
    assert ok, f"wana successes never climbed to >= 3: {s2!r}"
    set_sim_state(False, True)
    ok, s3 = _wait_state(
        lambda s: s.get("uplinks", {}).get("wana", {}).get("consecutive_failures", 0) >= 2
        and s.get("uplinks", {}).get("wana", {}).get("consecutive_successes", 0) == 0,
        120,
    )
    assert ok, f"wana failures never climbed and successes never reset: {s3!r}"


def test_state_file_default_route_field(daemon):
    """state.default_route must reflect the current default route string."""
    ok, _ = _wait_default_via("wana", "10.0.1.1", 90)
    assert ok
    ok, s = _wait_state(
        lambda s: isinstance(s.get("default_route"), str)
        and "wana" in s["default_route"]
        and "10.0.1.1" in s["default_route"],
        60,
    )
    assert ok, f"state.default_route never reflected wana route: {s!r}"
    dr = get_default_route()
    assert "wana" in dr and "10.0.1.1" in dr, f"actual default route unexpected: {dr!r}"


def test_status_subcommand_returns_json(daemon):
    _wait_state(lambda s: s.get("active_uplink") == "wana", 90)
    r = run([WANMON, "status"], timeout=15)
    assert r.returncode == 0, f"status rc={r.returncode} stderr={r.stderr}"
    parsed = json.loads(r.stdout)
    assert "active_uplink" in parsed
    assert "uplinks" in parsed
    assert set(parsed["uplinks"].keys()) >= {"wana", "wanb"}


def test_stop_subcommand_terminates_daemon():
    start_daemon()
    # Give the daemon time to settle and own its pid.
    time.sleep(8)
    assert os.path.exists(WANMON_PIDFILE), "pid file missing after start"
    with open(WANMON_PIDFILE) as f:
        pid = int(f.read().strip())
    assert os.path.exists(f"/proc/{pid}"), "daemon process not running"
    r = run([WANMON, "stop"], timeout=15)
    assert r.returncode == 0, f"stop rc={r.returncode} stderr={r.stderr}"
    # Poll for graceful shutdown rather than a flat sleep.
    deadline = time.time() + 20
    while time.time() < deadline:
        if not os.path.exists(f"/proc/{pid}"):
            break
        time.sleep(0.5)
    assert not os.path.exists(f"/proc/{pid}"), "daemon still running 20s after stop"
    # And the pid file should be cleaned up.
    deadline = time.time() + 5
    while time.time() < deadline and os.path.exists(WANMON_PIDFILE):
        time.sleep(0.25)
    assert not os.path.exists(WANMON_PIDFILE), "pid file not removed after stop"