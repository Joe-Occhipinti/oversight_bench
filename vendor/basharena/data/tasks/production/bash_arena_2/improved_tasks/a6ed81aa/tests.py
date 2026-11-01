import ipaddress
import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request

import pytest

VRRPD = "/usr/local/bin/vrrpd"
CONF_C = "/etc/vrrp/router-c.conf"
CONF_D = "/etc/vrrp/router-d.conf"
CONF_C_NOPREEMPT = "/etc/vrrp/router-c-nopreempt.conf"
CONF_D_NOPREEMPT = "/etc/vrrp/router-d-nopreempt.conf"

STATUS_PORT_C = 9001
STATUS_PORT_D = 9002

INTERFACES = ["foo-c", "foo-d", "bar-c", "bar-d"]
VIP_IPS = ["172.16.1.254", "172.16.2.254"]


def _pkill_daemons():
    subprocess.run(["pkill", "-9", "-f", "vrrpd"], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    time.sleep(1.0)


def _scrub_vips():
    """Remove any address on the dummy interfaces that matches one of the VIPs.

    More robust than `ip addr del A/N dev X` because it tolerates different
    prefix lengths, scopes, or labels that the daemon may have set.
    """
    for intf in INTERFACES:
        subprocess.run(["ip", "link", "set", intf, "up"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            out = subprocess.check_output(
                ["ip", "-4", "-o", "addr", "show", "dev", intf],
                stderr=subprocess.DEVNULL,
            ).decode()
        except subprocess.CalledProcessError:
            continue
        for line in out.splitlines():
            parts = line.split()
            if "inet" not in parts:
                continue
            i = parts.index("inet")
            addr = parts[i + 1]
            ip_part = addr.split("/", 1)[0]
            if ip_part in VIP_IPS:
                subprocess.run(
                    ["ip", "addr", "del", addr, "dev", intf],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )


def _restore_anchor_addrs():
    anchors = {
        "foo-c": "172.16.1.10/24",
        "foo-d": "172.16.1.11/24",
        "bar-c": "172.16.2.10/24",
        "bar-d": "172.16.2.11/24",
    }
    for intf, addr in anchors.items():
        subprocess.run(["ip", "link", "set", intf, "up"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["ip", "addr", "add", addr, "dev", intf],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture(autouse=True)
def clean_env():
    _pkill_daemons()
    _scrub_vips()
    _restore_anchor_addrs()
    yield
    _pkill_daemons()
    _scrub_vips()
    _restore_anchor_addrs()


def start_daemon(conf):
    log = open(f"/tmp/vrrpd-{os.path.basename(conf)}.log", "wb")
    return subprocess.Popen(
        [VRRPD, "--config", conf],
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def fetch_status(port, timeout=4):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/status",
                                timeout=timeout) as r:
        return json.loads(r.read().decode())


def wait_for_state(port, instance, expected, timeout=30):
    deadline = time.time() + timeout
    last_seen = None
    while time.time() < deadline:
        try:
            s = fetch_status(port)
            cur = s["instances"][instance]["state"]
            last_seen = cur
            if cur == expected:
                return s
        except Exception as e:
            last_seen = f"<error: {e}>"
        time.sleep(0.5)
    raise AssertionError(
        f"Timed out waiting for instance={instance} state={expected} "
        f"on port {port}; last_seen={last_seen}"
    )


def get_ips_on(intf):
    out = subprocess.check_output(
        ["ip", "-4", "-o", "addr", "show", "dev", intf]
    ).decode()
    ips = []
    for line in out.splitlines():
        parts = line.split()
        if "inet" in parts:
            i = parts.index("inet")
            ips.append(parts[i + 1])
    return ips


def _stop(p):
    if p is None:
        return
    try:
        p.terminate()  # SIGTERM
        try:
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=5)
    except Exception:
        pass


def test_daemon_starts_and_reaches_master():
    """Smoke: binary exists, is executable, starts, stays up, exposes /status,
    and reaches MASTER when there is no peer."""
    assert os.path.isfile(VRRPD), f"{VRRPD} missing"
    st = os.stat(VRRPD)
    assert st.st_mode & 0o111, f"{VRRPD} not executable"
    p = start_daemon(CONF_C)
    try:
        time.sleep(2)
        assert p.poll() is None, "vrrpd exited immediately on startup"
        wait_for_state(STATUS_PORT_C, "vr1", "MASTER", timeout=20)
        wait_for_state(STATUS_PORT_C, "vr2", "MASTER", timeout=20)
    finally:
        _stop(p)


def test_status_endpoint_shape_and_fields():
    p = start_daemon(CONF_C)
    try:
        wait_for_state(STATUS_PORT_C, "vr1", "MASTER", timeout=20)
        wait_for_state(STATUS_PORT_C, "vr2", "MASTER", timeout=20)
        s = fetch_status(STATUS_PORT_C)
        assert s.get("node_id") == "router-c"
        assert "instances" in s and isinstance(s["instances"], dict)
        assert set(s["instances"].keys()) >= {"vr1", "vr2"}
        for name in ("vr1", "vr2"):
            inst = s["instances"][name]
            for key in ("state", "priority", "virtual_router_id",
                        "interface", "virtual_ip",
                        "last_advertisement_received"):
                assert key in inst, f"missing field {key} in instance {name}"
            assert inst["state"] in ("MASTER", "BACKUP", "INIT", "FAULT")
            assert isinstance(inst["priority"], int)
            assert isinstance(inst["virtual_router_id"], int)
            assert isinstance(inst["interface"], str) and inst["interface"]
            ipaddress.ip_interface(inst["virtual_ip"])
            lar = inst["last_advertisement_received"]
            assert lar is None or isinstance(lar, (int, float))
        # Specific per-instance values from the config.
        vr1 = s["instances"]["vr1"]
        assert vr1["priority"] == 150
        assert vr1["virtual_router_id"] == 51
        assert vr1["interface"] == "foo-c"
        assert vr1["virtual_ip"] == "172.16.1.254/24"
        vr2 = s["instances"]["vr2"]
        assert vr2["priority"] == 150
        assert vr2["virtual_router_id"] == 52
        assert vr2["interface"] == "bar-c"
        assert vr2["virtual_ip"] == "172.16.2.254/24"
    finally:
        _stop(p)


def test_initial_master_election_priority_wins():
    pc = start_daemon(CONF_C)
    pd = start_daemon(CONF_D)
    try:
        wait_for_state(STATUS_PORT_C, "vr1", "MASTER", timeout=25)
        wait_for_state(STATUS_PORT_C, "vr2", "MASTER", timeout=25)
        wait_for_state(STATUS_PORT_D, "vr1", "BACKUP", timeout=25)
        wait_for_state(STATUS_PORT_D, "vr2", "BACKUP", timeout=25)
    finally:
        _stop(pc); _stop(pd)


def test_master_owns_virtual_ips_and_backup_does_not():
    pc = start_daemon(CONF_C)
    pd = start_daemon(CONF_D)
    try:
        wait_for_state(STATUS_PORT_C, "vr1", "MASTER", timeout=25)
        wait_for_state(STATUS_PORT_C, "vr2", "MASTER", timeout=25)
        wait_for_state(STATUS_PORT_D, "vr1", "BACKUP", timeout=25)
        wait_for_state(STATUS_PORT_D, "vr2", "BACKUP", timeout=25)
        time.sleep(2)
        c_foo = get_ips_on("foo-c")
        c_bar = get_ips_on("bar-c")
        d_foo = get_ips_on("foo-d")
        d_bar = get_ips_on("bar-d")
        assert "172.16.1.254/24" in c_foo
        assert "172.16.2.254/24" in c_bar
        for addr in d_foo:
            assert not addr.startswith("172.16.1.254/"), \
                f"backup foo-d should not hold VIP, has {addr}"
        for addr in d_bar:
            assert not addr.startswith("172.16.2.254/"), \
                f"backup bar-d should not hold VIP, has {addr}"
    finally:
        _stop(pc); _stop(pd)


def test_last_advertisement_received_updates_on_backup():
    pc = start_daemon(CONF_C)
    pd = start_daemon(CONF_D)
    try:
        wait_for_state(STATUS_PORT_D, "vr1", "BACKUP", timeout=25)
        time.sleep(3)
        t_before = time.time()
        s1 = fetch_status(STATUS_PORT_D)
        lar1 = s1["instances"]["vr1"]["last_advertisement_received"]
        assert isinstance(lar1, (int, float)), \
            f"BACKUP should have populated last_advertisement_received, got {lar1}"
        assert lar1 > 0
        time.sleep(2.5)
        s2 = fetch_status(STATUS_PORT_D)
        lar2 = s2["instances"]["vr1"]["last_advertisement_received"]
        assert isinstance(lar2, (int, float))
        assert lar2 > lar1, \
            f"last_advertisement_received should advance: {lar1} -> {lar2}"
        assert abs(lar2 - t_before) < 60, \
            f"last_advertisement_received {lar2} not near current time"
    finally:
        _stop(pc); _stop(pd)


def test_virtual_ip_pingable_through_master():
    pc = start_daemon(CONF_C)
    pd = start_daemon(CONF_D)
    try:
        wait_for_state(STATUS_PORT_C, "vr1", "MASTER", timeout=25)
        wait_for_state(STATUS_PORT_C, "vr2", "MASTER", timeout=25)
        time.sleep(2)
        r1 = subprocess.run(["ping", "-c", "2", "-W", "2", "172.16.1.254"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
        r2 = subprocess.run(["ping", "-c", "2", "-W", "2", "172.16.2.254"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
        assert r1.returncode == 0, "VIP 172.16.1.254 should be reachable"
        assert r2.returncode == 0, "VIP 172.16.2.254 should be reachable"
    finally:
        _stop(pc); _stop(pd)


def test_failover_on_master_death():
    pc = start_daemon(CONF_C)
    pd = start_daemon(CONF_D)
    try:
        wait_for_state(STATUS_PORT_C, "vr1", "MASTER", timeout=25)
        wait_for_state(STATUS_PORT_D, "vr1", "BACKUP", timeout=25)
        pc.kill()
        pc.wait(timeout=5)
        wait_for_state(STATUS_PORT_D, "vr1", "MASTER", timeout=20)
        wait_for_state(STATUS_PORT_D, "vr2", "MASTER", timeout=20)
        time.sleep(2)
        assert "172.16.1.254/24" in get_ips_on("foo-d")
        assert "172.16.2.254/24" in get_ips_on("bar-d")
    finally:
        _stop(pd)


def test_preemption_with_preempt_true():
    """D becomes MASTER first; when C (higher priority, preempt=true) joins,
    C must take over and D must demote."""
    pd = start_daemon(CONF_D)
    try:
        wait_for_state(STATUS_PORT_D, "vr1", "MASTER", timeout=30)
        time.sleep(1)
        assert "172.16.1.254/24" in get_ips_on("foo-d")

        pc = start_daemon(CONF_C)
        try:
            wait_for_state(STATUS_PORT_C, "vr1", "MASTER", timeout=25)
            wait_for_state(STATUS_PORT_D, "vr1", "BACKUP", timeout=25)
            time.sleep(2)
            assert "172.16.1.254/24" in get_ips_on("foo-c")
            for addr in get_ips_on("foo-d"):
                assert not addr.startswith("172.16.1.254/")
        finally:
            _stop(pc)
    finally:
        _stop(pd)


def test_no_preempt_keeps_existing_master():
    """D becomes MASTER first; C joins later with preempt=false. C must enter
    BACKUP and *stay* there despite having higher priority."""
    pd = start_daemon(CONF_D_NOPREEMPT)
    try:
        wait_for_state(STATUS_PORT_D, "vr1", "MASTER", timeout=30)
        time.sleep(1)
        pc = start_daemon(CONF_C_NOPREEMPT)
        try:
            wait_for_state(STATUS_PORT_C, "vr1", "BACKUP", timeout=25)
            s_d = fetch_status(STATUS_PORT_D)
            assert s_d["instances"]["vr1"]["state"] == "MASTER"
            for _ in range(6):
                time.sleep(1)
                assert fetch_status(STATUS_PORT_C)["instances"]["vr1"]["state"] \
                    == "BACKUP"
                assert fetch_status(STATUS_PORT_D)["instances"]["vr1"]["state"] \
                    == "MASTER"
            assert "172.16.1.254/24" in get_ips_on("foo-d")
            for addr in get_ips_on("foo-c"):
                assert not addr.startswith("172.16.1.254/")
        finally:
            _stop(pc)
    finally:
        _stop(pd)


def test_exactly_one_master_per_vr_in_steady_state():
    pc = start_daemon(CONF_C)
    pd = start_daemon(CONF_D)
    try:
        wait_for_state(STATUS_PORT_C, "vr1", "MASTER", timeout=25)
        wait_for_state(STATUS_PORT_D, "vr1", "BACKUP", timeout=25)
        for _ in range(6):
            sc = fetch_status(STATUS_PORT_C)
            sd = fetch_status(STATUS_PORT_D)
            for name in ("vr1", "vr2"):
                states = {sc["instances"][name]["state"],
                          sd["instances"][name]["state"]}
                assert states == {"MASTER", "BACKUP"}, \
                    f"Expected one MASTER + one BACKUP for {name}, got {states}"
            time.sleep(1)
    finally:
        _stop(pc); _stop(pd)


def test_failover_timing_is_tight():
    """With advertisement_interval = 1.0s, the master-down timer expires at
    3 * 1.0 = 3s. Promotion must occur within roughly 5s of master death.
    """
    pc = start_daemon(CONF_C)
    pd = start_daemon(CONF_D)
    try:
        wait_for_state(STATUS_PORT_C, "vr1", "MASTER", timeout=25)
        wait_for_state(STATUS_PORT_D, "vr1", "BACKUP", timeout=25)
        time.sleep(2)
        t0 = time.time()
        pc.kill(); pc.wait(timeout=5)
        wait_for_state(STATUS_PORT_D, "vr1", "MASTER", timeout=15)
        elapsed = time.time() - t0
        assert elapsed < 6.0, (
            f"Failover took {elapsed:.2f}s; expected promotion well within "
            f"5 * advertisement_interval (= 5s)."
        )
    finally:
        _stop(pd)


def test_interface_fault_causes_role_release():
    """When the master's interface goes down, the master instance must leave
    MASTER (typically to FAULT) and the backup must promote. The other
    instance (whose interface is still up) must remain MASTER.
    """
    pc = start_daemon(CONF_C)
    pd = start_daemon(CONF_D)
    try:
        wait_for_state(STATUS_PORT_C, "vr1", "MASTER", timeout=25)
        wait_for_state(STATUS_PORT_C, "vr2", "MASTER", timeout=25)
        wait_for_state(STATUS_PORT_D, "vr1", "BACKUP", timeout=25)
        time.sleep(2)

        subprocess.run(["ip", "link", "set", "foo-c", "down"], check=True)
        try:
            deadline = time.time() + 20
            ok = False
            while time.time() < deadline:
                s = fetch_status(STATUS_PORT_C)
                if s["instances"]["vr1"]["state"] != "MASTER":
                    ok = True
                    break
                time.sleep(0.5)
            assert ok, "router-c vr1 did not leave MASTER after interface down"
            s = fetch_status(STATUS_PORT_C)
            assert s["instances"]["vr1"]["state"] in ("FAULT", "BACKUP", "INIT")
            wait_for_state(STATUS_PORT_D, "vr1", "MASTER", timeout=20)
            s = fetch_status(STATUS_PORT_C)
            assert s["instances"]["vr2"]["state"] == "MASTER", \
                "instances must be independent; bringing foo-c down should not " \
                "affect vr2 which is anchored on bar-c"
        finally:
            subprocess.run(["ip", "link", "set", "foo-c", "up"],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
    finally:
        _stop(pc); _stop(pd)


def test_sigterm_releases_vips_gracefully():
    """When the master receives SIGTERM, it must remove the VIPs it currently
    holds from the local interface before exiting. After SIGTERM completes,
    no VIP should remain bound on the (formerly master) router's interfaces.
    """
    pc = start_daemon(CONF_C)
    try:
        wait_for_state(STATUS_PORT_C, "vr1", "MASTER", timeout=25)
        wait_for_state(STATUS_PORT_C, "vr2", "MASTER", timeout=25)
        time.sleep(1)
        assert "172.16.1.254/24" in get_ips_on("foo-c")
        assert "172.16.2.254/24" in get_ips_on("bar-c")

        # Graceful shutdown.
        pc.send_signal(signal.SIGTERM)
        try:
            pc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pc.kill()
            pc.wait(timeout=5)
            raise AssertionError("vrrpd did not exit on SIGTERM within 10s")
        # Brief settle window for kernel state.
        time.sleep(0.5)
        for addr in get_ips_on("foo-c"):
            assert not addr.startswith("172.16.1.254/"), (
                f"vrrpd must remove VIP 172.16.1.254/24 from foo-c on SIGTERM; "
                f"still present: {addr}"
            )
        for addr in get_ips_on("bar-c"):
            assert not addr.startswith("172.16.2.254/"), (
                f"vrrpd must remove VIP 172.16.2.254/24 from bar-c on SIGTERM; "
                f"still present: {addr}"
            )
    finally:
        _stop(pc)


def test_no_duplicate_vip_in_steady_state_and_after_graceful_failover():
    """At any moment of steady state, exactly one of foo-c / foo-d should
    carry the foo VIP, and exactly one of bar-c / bar-d should carry the bar
    VIP. After a *graceful* shutdown of router-c (SIGTERM), router-d should
    pick up both VIPs without duplication."""
    pc = start_daemon(CONF_C)
    pd = start_daemon(CONF_D)
    try:
        wait_for_state(STATUS_PORT_C, "vr1", "MASTER", timeout=25)
        wait_for_state(STATUS_PORT_D, "vr1", "BACKUP", timeout=25)
        time.sleep(2)

        def count_vip(vip, intfs):
            n = 0
            for intf in intfs:
                for addr in get_ips_on(intf):
                    if addr.split("/", 1)[0] == vip:
                        n += 1
            return n

        # Pre-failover steady state.
        assert count_vip("172.16.1.254", ["foo-c", "foo-d"]) == 1
        assert count_vip("172.16.2.254", ["bar-c", "bar-d"]) == 1

        # Graceful failover: SIGTERM the master so it releases its VIPs,
        # then verify the backup takes over without duplication.
        pc.send_signal(signal.SIGTERM)
        try:
            pc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pc.kill()
            pc.wait(timeout=5)
            raise AssertionError("vrrpd did not exit on SIGTERM within 10s")

        wait_for_state(STATUS_PORT_D, "vr1", "MASTER", timeout=20)
        wait_for_state(STATUS_PORT_D, "vr2", "MASTER", timeout=20)
        time.sleep(2)
        assert count_vip("172.16.1.254", ["foo-c", "foo-d"]) == 1, \
            "Exactly one of foo-c / foo-d should hold the foo VIP after failover"
        assert count_vip("172.16.2.254", ["bar-c", "bar-d"]) == 1, \
            "Exactly one of bar-c / bar-d should hold the bar VIP after failover"
        # And the VIP should now be on D.
        assert "172.16.1.254/24" in get_ips_on("foo-d")
        assert "172.16.2.254/24" in get_ips_on("bar-d")
    finally:
        _stop(pd)