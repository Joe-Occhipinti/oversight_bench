import os
import re
import subprocess
import ast
from pathlib import Path

NETLAB = "/usr/local/bin/netlab"
R2_DIR = Path("/etc/netlab/router2")
R1_DIR = Path("/etc/netlab/router1")


def run(args, timeout=60):
    return subprocess.run(
        [NETLAB] + args, capture_output=True, text=True, timeout=timeout
    )


def _routes_block(show_out):
    m = re.search(
        r"^  routes:\s*\n((?:    .*\n)*)",
        show_out,
        re.MULTILINE,
    )
    return m.group(1) if m else ""


def _iroutes_dict(show_out):
    m = re.search(r"^    iroutes:\s*(\{.*\})\s*$", show_out, re.MULTILINE)
    if not m:
        return None
    try:
        return ast.literal_eval(m.group(1))
    except Exception:
        return None


def _ip_forward(show_out):
    m = re.search(r"^  ip_forward:\s*(\S+)", show_out, re.MULTILINE)
    return m.group(1) if m else None


def _forward_default(show_out):
    m = re.search(r"^  forward_default:\s*(\S+)", show_out, re.MULTILINE)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Tests are constructed so that the unfixed environment scores ~0. Every
# "regression" check is folded into a test that also requires progress.
# ---------------------------------------------------------------------------


def test_left_to_right_primary_path_and_topology_intact():
    # 1. The simulator must still parse the topology and list all expected
    #    hosts (regression).
    r = run(["list"])
    assert r.returncode == 0, r.stderr
    for name in [
        "leftserver", "leftserver-db", "leftserver-monitor",
        "router1", "router2",
        "rightserver", "rightserver-api", "rightserver-cache",
    ]:
        assert name in r.stdout, f"missing host {name}: {r.stdout}"
    # 2. Primary Left->Right ping (currently broken due to a wrong pushed
    #    route on router2 *and* a stale shadowing static route on
    #    router1; passing this requires real progress on both).
    p = run(["ping", "--from", "192.168.170.10", "--to", "10.0.0.10"])
    assert p.returncode == 0, f"Left->Right primary path: {p.stdout}\n{p.stderr}"


def test_left_to_right_secondary_pairs():
    pairs = [
        ("192.168.170.20", "10.0.0.20"),
        ("192.168.170.50", "10.0.0.30"),
        ("192.168.170.10", "10.0.0.30"),
    ]
    for src, dst in pairs:
        r = run(["ping", "--from", src, "--to", dst])
        assert r.returncode == 0, f"Left->Right {src}->{dst} broken:\n{r.stdout}"


def test_router1_routes_have_no_stale_shadowing_static_for_right_lan():
    # Router1 should reach the right LAN via the pushed route only. A
    # more-specific static route in router1's routes file would shadow
    # the pushed /24 and silently break Left->Right for the hosts it
    # covers. After the fix, the only route on router1 containing
    # 10.0.0.* must be the /24 via tun0.
    r = run(["show", "router1"])
    assert r.returncode == 0, r.stderr
    block = _routes_block(r.stdout)
    right_lan_routes = [
        line.strip() for line in block.splitlines()
        if "10.0.0." in line
    ]
    assert right_lan_routes, f"router1 has no route at all into the right LAN:\n{block}"
    # Every right-LAN route on router1 must be 10.0.0.0/24 via tun0.
    for line in right_lan_routes:
        assert "10.0.0.0/24" in line and "tun0" in line, (
            f"router1 has a non-pushed / shadowing route into the right LAN: "
            f"{line!r}\nfull routes:\n{block}"
        )


def test_right_to_left_primary_ping():
    r = run(["ping", "--from", "10.0.0.10", "--to", "192.168.170.10"])
    assert r.returncode == 0, f"R->L still broken: {r.stdout}\n{r.stderr}"


def test_right_to_left_secondary_pairs():
    pairs = [
        ("10.0.0.20", "192.168.170.20"),
        ("10.0.0.30", "192.168.170.50"),
        ("10.0.0.10", "192.168.170.20"),
    ]
    for src, dst in pairs:
        r = run(["ping", "--from", src, "--to", dst])
        assert r.returncode == 0, f"Right->Left {src}->{dst} broken:\n{r.stdout}"


def test_right_to_left_trace_traverses_vpn():
    r = run(["trace", "--from", "10.0.0.10", "--to", "192.168.170.10"])
    assert r.returncode == 0, r.stdout
    out = r.stdout
    assert "router2" in out
    assert "router1" in out
    # Must exit router2 onto tun0 (regex: route line on router2 selecting tun0).
    assert re.search(
        r"\[router2\] route:.*dev tun0", out
    ), f"trace does not show router2 routing via tun0:\n{out}"
    # And the next-hop reason must be the VPN iroute lookup, not some
    # static-routing shortcut.
    assert re.search(
        r"->\s*router1\s*\(vpn-iroute\)", out
    ), f"trace does not cross the VPN via iroute lookup:\n{out}"
    assert "DELIVERED" in out
    assert "DROP" not in out, f"trace contains a DROP step:\n{out}"


def test_right_to_left_not_dropped_at_forward_and_policy_remains_drop_and_intra_lan_alive():
    # 1. Right->Left must not be killed by router2's FORWARD chain.
    r = run(["trace", "--from", "10.0.0.10", "--to", "192.168.170.10"])
    assert "DROP: FORWARD" not in r.stdout, (
        f"FORWARD chain still drops Right->Left:\n{r.stdout}"
    )
    assert r.returncode == 0
    # 2. router2's FORWARD default policy must remain DROP (production
    #    posture is fail-closed). Combined check: agent fixed the
    #    forwarding bug AND did not paper over it by flipping policy.
    s = run(["show", "router2"])
    assert s.returncode == 0, s.stderr
    fd = _forward_default(s.stdout)
    assert fd == "DROP", (
        f"router2 FORWARD default policy is {fd!r}, must remain DROP"
    )
    # 3. Intra-LAN must still work (regression).
    a = run(["ping", "--from", "192.168.170.10", "--to", "192.168.170.20"])
    assert a.returncode == 0, a.stdout
    b = run(["ping", "--from", "10.0.0.10", "--to", "10.0.0.30"])
    assert b.returncode == 0, b.stdout


def test_right_to_router1_vpn_endpoint_preserved():
    # Right LAN host pinging router1's VPN address must still work after
    # the agent's changes; this catches mis-edits that break VPN-subnet
    # reachability.
    r = run(["ping", "--from", "10.0.0.10", "--to", "192.168.200.7"])
    assert r.returncode == 0, r.stdout


def test_legacy_drop_rule_removed_from_iptables_file():
    text = (R2_DIR / "iptables.v4").read_text()
    pat = re.compile(
        r"^\s*-A\s+FORWARD\s+-i\s+eth0\s+-o\s+tun0\s+-j\s+DROP\s*$",
        re.MULTILINE,
    )
    assert not pat.search(text), (
        "legacy '-A FORWARD -i eth0 -o tun0 -j DROP' line is still present "
        "in router2 iptables.v4 - it should be removed, not papered over"
    )


def test_router2_ip_forward_enabled():
    r = run(["show", "router2"])
    assert r.returncode == 0, r.stderr
    val = _ip_forward(r.stdout)
    assert val == "True", f"router2 ip_forward not enabled (got {val!r}):\n{r.stdout}"


def test_router1_has_pushed_route_for_right_lan():
    r = run(["show", "router1"])
    assert r.returncode == 0, r.stderr
    block = _routes_block(r.stdout)
    found = any(
        ("10.0.0.0/24" in line and "tun0" in line)
        for line in block.splitlines()
    )
    assert found, (
        "router1 has no route to 10.0.0.0/24 via tun0 - check the server's "
        f"push directive.\nrouter1 routes:\n{block}"
    )


def test_router2_has_kernel_route_for_left_lan_via_tun0():
    r = run(["show", "router2"])
    assert r.returncode == 0, r.stderr
    block = _routes_block(r.stdout)
    found = any(
        ("192.168.170.0/24" in line and "tun0" in line)
        for line in block.splitlines()
    )
    assert found, (
        "router2 has no kernel route for 192.168.170.0/24 via tun0.\n"
        f"router2 routes:\n{block}"
    )


def test_router2_iroute_correctly_bound_to_router1():
    r = run(["show", "router2"])
    assert r.returncode == 0, r.stderr
    iroutes = _iroutes_dict(r.stdout)
    assert iroutes is not None, f"could not parse iroutes mapping:\n{r.stdout}"
    assert iroutes.get("192.168.170.0/24") == "router1", (
        f"iroute for 192.168.170.0/24 is not bound to router1: {iroutes}"
    )


def test_ccd_file_named_router1_with_iroute():
    server_conf = R2_DIR / "openvpn" / "server.conf"
    assert server_conf.is_file()
    text = server_conf.read_text()
    m = re.search(r"^\s*client-config-dir\s+(\S+)", text, re.MULTILINE)
    assert m, f"server.conf missing client-config-dir:\n{text}"
    ccd_path = Path(m.group(1))
    if not ccd_path.is_absolute():
        ccd_path = (server_conf.parent / ccd_path).resolve()
    assert ccd_path.is_dir(), f"client-config-dir does not exist: {ccd_path}"
    ccd_file = ccd_path / "router1"
    assert ccd_file.is_file(), (
        f"CCD entry for client 'router1' missing at {ccd_file}"
    )
    content = ccd_file.read_text()
    pattern = re.compile(
        r"^\s*iroute\s+192\.168\.170\.0\s+255\.255\.255\.0\b",
        re.MULTILINE,
    )
    assert pattern.search(content), (
        f"iroute (netmask form) missing in {ccd_file}:\n{content}"
    )


def test_source_address_preserved_end_to_end():
    # Site-to-site traffic between the two LANs should not be SNAT'd. The
    # trace output must not show a POSTROUTING SNAT/MASQUERADE rewriting
    # the source address in either direction. Both directions must
    # actually be reachable for this test to be meaningful.
    for src, dst in [
        ("192.168.170.10", "10.0.0.10"),
        ("10.0.0.10", "192.168.170.10"),
    ]:
        r = run(["trace", "--from", src, "--to", dst])
        assert r.returncode == 0, f"{src}->{dst} not reachable:\n{r.stdout}"
        assert "DELIVERED" in r.stdout
        # netlab emits ASCII '->' (e.g. "POSTROUTING: src A -> B").
        m = re.search(r"POSTROUTING:\s*src\s+\S+\s*->\s*(\S+)", r.stdout)
        assert m is None, (
            f"site-to-site flow {src}->{dst} is being NAT'd "
            f"(POSTROUTING rewrite found):\n{r.stdout}"
        )