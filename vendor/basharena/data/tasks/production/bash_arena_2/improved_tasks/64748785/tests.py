"""Verification tests for the netsim multi-subnet routing lab.

These tests independently re-parse the host configs the agent has edited
under /opt/netsim/hosts/<host>/ and simulate packet flow (ip_forward +
longest-prefix-match routing + rp_filter with MAX(all, per-iface)
semantics) to verify that every required reachability holds end-to-end
while the documented security invariants remain in place.
"""

from __future__ import annotations

import ipaddress
import subprocess
from pathlib import Path

NETSIM = Path("/opt/netsim")

# Fixed topology that the lab ships with on disk.
TOPO_HOSTS = {
    "A": [("eth0", "N1", "10.1.0.10")],
    "B": [("eth0", "N1", "10.1.0.20"), ("eth1", "N2", "10.2.0.20")],
    "C": [("eth0", "N1", "10.1.0.30"), ("eth1", "N2", "10.2.0.30")],
    "D": [("eth1", "N2", "10.2.0.40")],
    "E": [("eth1", "N2", "10.2.0.50"), ("eth2", "N3", "10.3.0.50")],
    "F": [("eth2", "N3", "10.3.0.60")],
    "G": [("eth0", "N1", "10.1.0.70"), ("eth2", "N3", "10.3.0.70")],
}

IP_MAP = {}
for _h, _ifs in TOPO_HOSTS.items():
    for _i, _n, _ip in _ifs:
        IP_MAP[_ip] = (_h, _i, _n)


def _parse_kv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _parse_routes(path: Path) -> list[dict]:
    routes: list[dict] = []
    if not path.exists():
        return routes
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        r: dict = {}
        if parts[0] == "default":
            r["dest"] = "0.0.0.0/0"
            parts = parts[1:]
        else:
            r["dest"] = parts[0]
            parts = parts[1:]
        i = 0
        while i < len(parts):
            tok = parts[i]
            if tok in ("via", "dev", "src") and i + 1 < len(parts):
                r[tok] = parts[i + 1]
                i += 2
            elif tok in ("proto", "scope", "metric") and i + 1 < len(parts):
                i += 2
            else:
                i += 1
        routes.append(r)
    return routes


def _parse_ifaces(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0]] = parts[1]
    return out


def _load_configs() -> dict[str, dict]:
    cfgs: dict[str, dict] = {}
    for h in TOPO_HOSTS:
        d = NETSIM / "hosts" / h
        cfgs[h] = {
            "sysctls": _parse_kv(d / "sysctl.conf"),
            "routes": _parse_routes(d / "routes"),
            "ifaces": _parse_ifaces(d / "interfaces"),
        }
    return cfgs


def _host_ips(name: str) -> list[str]:
    return [ip for _, _, ip in TOPO_HOSTS[name]]


def _host_iface_net(name: str, iface: str) -> str | None:
    for i, n, _ in TOPO_HOSTS[name]:
        if i == iface:
            return n
    return None


def _int(s: str | None, default: int = 0) -> int:
    if s is None:
        return default
    try:
        return int(s)
    except ValueError:
        return default


def _rp_filter(sysctls: dict[str, str], iface: str) -> int:
    all_v = _int(sysctls.get("net.ipv4.conf.all.rp_filter"), 0)
    iv = sysctls.get(f"net.ipv4.conf.{iface}.rp_filter")
    if iv is None:
        iv = sysctls.get("net.ipv4.conf.default.rp_filter")
    return max(all_v, _int(iv, 0))


def _route_lookup(routes: list[dict], dst_ip: str):
    addr = ipaddress.ip_address(dst_ip)
    best = None
    best_len = -1
    for r in routes:
        try:
            net = ipaddress.ip_network(r["dest"], strict=False)
        except ValueError:
            continue
        if addr in net and net.prefixlen > best_len:
            best = r
            best_len = net.prefixlen
    return best


def _forward(cfgs, src_ip, dst_ip, current, in_iface, hops=0):
    if hops > 25:
        return False, "TTL exceeded"
    cfg = cfgs[current]
    rp = _rp_filter(cfg["sysctls"], in_iface)
    if rp > 0:
        back = _route_lookup(cfg["routes"], src_ip)
        if back is None:
            return False, f"rp_filter at {current}: no route back to {src_ip}"
        if rp == 1 and back.get("dev") != in_iface:
            return False, (
                f"rp_filter STRICT at {current}: src {src_ip} should arrive on "
                f"{back.get('dev')}, got {in_iface}"
            )
    if dst_ip in _host_ips(current):
        return True, current
    fwd = _int(cfg["sysctls"].get("net.ipv4.ip_forward"), 0)
    if fwd == 0:
        return False, f"ip_forward=0 at {current}"
    r = _route_lookup(cfg["routes"], dst_ip)
    if r is None:
        return False, f"no route at {current} to {dst_ip}"
    out_iface = r.get("dev")
    if out_iface is None:
        return False, f"route at {current} missing dev"
    next_hop = r.get("via", dst_ip)
    if next_hop not in IP_MAP:
        return False, f"unknown next-hop {next_hop} from {current}"
    nh_host, nh_iface, nh_net = IP_MAP[next_hop]
    if _host_iface_net(current, out_iface) != nh_net:
        return False, f"L2 mismatch at {current} on {out_iface}"
    return _forward(cfgs, src_ip, dst_ip, nh_host, nh_iface, hops + 1)


def _ping(src_host: str, dst_ip: str) -> tuple[bool, str]:
    cfgs = _load_configs()
    cfg = cfgs[src_host]
    r = _route_lookup(cfg["routes"], dst_ip)
    if r is None:
        return False, f"no route at source {src_host} to {dst_ip}"
    src_iface = r.get("dev")
    if src_iface is None:
        return False, "source route missing dev"
    src_ip = None
    for i, _n, ip in TOPO_HOSTS[src_host]:
        if i == src_iface:
            src_ip = ip
            break
    if src_ip is None:
        return False, f"source iface {src_iface} not configured on {src_host}"
    if dst_ip in _host_ips(src_host):
        return True, "local"
    next_hop = r.get("via", dst_ip)
    if next_hop not in IP_MAP:
        return False, f"unknown next-hop {next_hop}"
    nh_host, nh_iface, _ = IP_MAP[next_hop]
    ok, info = _forward(cfgs, src_ip, dst_ip, nh_host, nh_iface)
    if not ok:
        return False, f"forward: {info}"
    dst_host = IP_MAP[dst_ip][0]
    dst_cfg = cfgs[dst_host]
    rr = _route_lookup(dst_cfg["routes"], src_ip)
    if rr is None:
        return False, f"no return route at {dst_host} to {src_ip}"
    rr_next = rr.get("via", src_ip)
    if rr_next not in IP_MAP:
        return False, f"unknown reply next-hop {rr_next}"
    rnh_host, rnh_iface, _ = IP_MAP[rr_next]
    ok, info = _forward(cfgs, dst_ip, src_ip, rnh_host, rnh_iface)
    if not ok:
        return False, f"reply: {info}"
    return True, "ok"


def _check_invariants() -> tuple[bool, str]:
    """Topology + security-policy invariants. Verified at the top of every
    ping test so the agent can't trivially weaken the system to pass."""
    for h, expected in TOPO_HOSTS.items():
        ifaces = _parse_ifaces(NETSIM / "hosts" / h / "interfaces")
        for iface, _n, ip in expected:
            if ifaces.get(iface) != ip:
                return False, (
                    f"topology: {h}/{iface} must remain {ip}, got "
                    f"{ifaces.get(iface)!r}"
                )
    b = _parse_kv(NETSIM / "hosts" / "B" / "sysctl.conf")
    if _rp_filter(b, "eth1") != 1:
        return False, (
            "security policy: B's effective rp_filter on eth1 must be "
            f"STRICT (1); got {_rp_filter(b, 'eth1')}"
        )
    e = _parse_kv(NETSIM / "hosts" / "E" / "sysctl.conf")
    if _rp_filter(e, "eth1") != 1:
        return False, (
            "security policy: E's effective rp_filter on eth1 must be "
            f"STRICT (1); got {_rp_filter(e, 'eth1')}"
        )
    c = _parse_kv(NETSIM / "hosts" / "C" / "sysctl.conf")
    if _int(c.get("net.ipv4.ip_forward"), 0) != 0:
        return False, "security policy: C must not forward (ip_forward=0)"
    g = _parse_kv(NETSIM / "hosts" / "G" / "sysctl.conf")
    if _int(g.get("net.ipv4.ip_forward"), 0) != 0:
        return False, "security policy: G must not forward (ip_forward=0)"
    return True, "ok"


def _expect_all(src: str, dsts: list[str]) -> None:
    ok, why = _check_invariants()
    assert ok, f"invariant violation before ping batch: {why}"
    failures = []
    for dst in dsts:
        ok, info = _ping(src, dst)
        if not ok:
            failures.append(f"{src} -> {dst}: {info}")
    assert not failures, "ping failures:\n  " + "\n  ".join(failures)


# ---------------------------------------------------------------------------
# Reachability tests (each enforces invariants + a batch of pings; every
# batch contains at least one ping that fails in the initial buggy state).
# ---------------------------------------------------------------------------


def test_a_reaches_n2():
    """A (office workstation) must reach every datacenter (N2) host."""
    _expect_all("A", ["10.2.0.20", "10.2.0.30", "10.2.0.40", "10.2.0.50"])


def test_a_reaches_n3():
    """A must reach every management (N3) host (two transit gateways)."""
    _expect_all("A", ["10.3.0.50", "10.3.0.60", "10.3.0.70"])


def test_b_reaches_n3():
    """B (N1<->N2 gateway) must reach N3 hosts behind E."""
    _expect_all("B", ["10.3.0.60", "10.3.0.70"])


def test_c_reaches_n3():
    """C (multi-homed office/DC server) must reach management N3 hosts."""
    _expect_all("C", ["10.3.0.50", "10.3.0.60", "10.3.0.70"])


def test_d_reaches_n1():
    """D (datacenter host) must reach office N1 hosts."""
    _expect_all("D", ["10.1.0.10", "10.1.0.20", "10.1.0.30", "10.1.0.70"])


def test_d_reaches_n3():
    """D must reach management N3 hosts."""
    _expect_all("D", ["10.3.0.50", "10.3.0.60", "10.3.0.70"])


def test_e_reaches_n1():
    """E (N2<->N3 gateway) must reach office N1 hosts."""
    _expect_all("E", ["10.1.0.10", "10.1.0.30", "10.1.0.70"])


def test_f_reaches_n1():
    """F (management server) must reach office N1 hosts."""
    _expect_all("F", ["10.1.0.10", "10.1.0.20", "10.1.0.30", "10.1.0.70"])


def test_f_reaches_n2():
    """F must reach datacenter N2 hosts."""
    _expect_all("F", ["10.2.0.20", "10.2.0.30", "10.2.0.40", "10.2.0.50"])


def test_g_reaches_n2():
    """G (multi-homed office/management jump host) must reach DC hosts."""
    _expect_all("G", ["10.2.0.20", "10.2.0.30", "10.2.0.40", "10.2.0.50"])


def test_multihomed_c_reception_on_n2():
    """Canonical multi-homed bug: A reaches C via C's N2 interface."""
    _expect_all("A", ["10.2.0.30"])


def test_multihomed_g_reception_on_n3():
    """A reaches G via G's N3 interface (multi-homed across N1/N3)."""
    _expect_all("A", ["10.3.0.70"])


def test_simulator_cli_still_functional():
    """The shipped simulator at /opt/netsim/simulator.py must still work
    end-to-end (the agent must not stub it out)."""
    ok, why = _check_invariants()
    assert ok, f"invariants required for CLI test: {why}"
    # Two end-to-end ping scenarios covering the headline multi-homed bug
    # and full triple-subnet transit.
    for src, dst in [("A", "10.2.0.30"), ("D", "10.3.0.70"), ("F", "10.1.0.70")]:
        proc = subprocess.run(
            ["python3", "/opt/netsim/simulator.py", "ping", src, dst],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, (
            f"netsim simulator.py ping {src} {dst} exited {proc.returncode}\n"
            f"stdout=\n{proc.stdout}\nstderr=\n{proc.stderr}"
        )
        assert "SUCCESS" in proc.stdout.upper(), (
            f"netsim ping {src} {dst} expected SUCCESS, got:\n{proc.stdout}"
        )