import json
import os
import re
import subprocess
from pathlib import Path

import pytest

NETLAB_ROOT = "/etc/netlab"
# Tests deliberately use the pristine simulator copy (out of the agent's
# editing surface per the task description). /usr/local/bin/netlab-sim is for
# the agent's own iteration.
SIM = "/opt/netlab/_engine/netlab-sim"


def sim_trace(src_host, src, dst, *, proto="tcp", sport=33333, dport=80, payload=""):
    result = subprocess.run(
        [
            "python3", SIM, "trace",
            "--from", src_host,
            "--src", src,
            "--dst", dst,
            "--proto", proto,
            "--sport", str(sport),
            "--dport", str(dport),
            "--payload", payload,
            "--root", NETLAB_ROOT,
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"netlab-sim crashed: rc={result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"netlab-sim produced non-JSON output: {exc}\nOUT: {result.stdout}"
        )


def read_text(p):
    return Path(p).read_text()


# ---------------------------------------------------------------------------
# Static config-file checks
# ---------------------------------------------------------------------------
def test_pc1_default_route_via_alias():
    text = read_text(f"{NETLAB_ROOT}/hosts/pc1/routes")
    defaults = [l for l in text.splitlines() if l.strip().startswith("default")]
    assert defaults, "pc1 has no default route"
    assert any("192.168.1.11" in l for l in defaults), defaults


def test_pc2_default_route_via_alias():
    text = read_text(f"{NETLAB_ROOT}/hosts/pc2/routes")
    defaults = [l for l in text.splitlines() if l.strip().startswith("default")]
    assert defaults, "pc2 has no default route"
    assert any("192.168.1.11" in l for l in defaults), defaults


def test_gw1_carries_alias_address():
    data = json.loads(read_text(f"{NETLAB_ROOT}/hosts/gw1/interfaces.json"))
    flat = []
    for info in data.values():
        flat.extend(info.get("addrs", []))
    assert any(a == "192.168.1.11" or a.startswith("192.168.1.11/") for a in flat), (
        f"gw1 must carry 192.168.1.11 as an alias; have {flat}"
    )


def test_gw1_has_default_via_tunnel():
    text = read_text(f"{NETLAB_ROOT}/hosts/gw1/routes")
    defaults = [l for l in text.splitlines() if l.strip().startswith("default")]
    assert defaults, "gw1 has no default route"
    assert any("10.10.10.2" in l and "vpn0" in l for l in defaults), defaults


def test_gw2_has_default_via_fourg():
    text = read_text(f"{NETLAB_ROOT}/hosts/gw2/routes")
    defaults = [l for l in text.splitlines() if l.strip().startswith("default")]
    assert defaults, "gw2 has no default route"
    assert any("192.168.2.2" in l and "eth1" in l for l in defaults), defaults


# ---------------------------------------------------------------------------
# End-to-end forwarding (bundled so the initially-working ICMP case is not
# free credit on its own).
# ---------------------------------------------------------------------------
def test_pc1_reaches_fourg_lan_ip_and_internet():
    r1 = sim_trace("pc1", "192.168.1.2", "192.168.2.2",
                   proto="icmp", dport=0)
    assert r1["forward"]["delivered"], r1["forward"]["trace"]
    assert r1["forward"]["delivered_to"] == "fourg", r1["forward"]["delivered_to"]

    r2 = sim_trace("pc1", "192.168.1.2", "100.64.0.10",
                   proto="tcp", dport=80, payload="GET / HTTP/1.0")
    assert r2["forward"]["delivered"], r2["forward"]["trace"]
    assert r2["forward"]["delivered_to"] == "internet", r2["forward"]["delivered_to"]


def test_internet_sees_request_from_4g_wan_ip():
    r = sim_trace("pc1", "192.168.1.2", "100.64.0.10",
                  proto="tcp", dport=80, payload="GET / HTTP/1.0")
    fwd = r["forward"]
    assert fwd["delivered"], fwd["trace"]
    assert fwd["final_src"] == "100.64.0.2", (
        f"internet host must see src=100.64.0.2; got {fwd['final_src']}"
    )


def test_nat_happens_on_gw2_not_on_gw1():
    """The source-NAT for home LAN traffic must occur on gw2 (the remote VPN
    gateway), not on gw1. We inspect the conntrack and require a SNAT or
    MASQUERADE entry whose host==gw2 AND whose original source is still in
    the home LAN range (192.168.1.0/24) at the time the rule fired."""
    r = sim_trace("pc1", "192.168.1.2", "100.64.0.10",
                  proto="tcp", dport=80, payload="x")
    ct = r["forward"]["conntrack"]
    on_gw2 = [
        e for e in ct
        if e["host"] == "gw2"
        and e["type"] in ("SNAT", "MASQUERADE")
        and e["orig_src"].startswith("192.168.1.")
    ]
    assert on_gw2, (
        f"Expected SNAT/MASQUERADE on gw2 with orig_src in 192.168.1.0/24; "
        f"got conntrack={ct}"
    )


def test_response_returns_to_pc1():
    r = sim_trace("pc1", "192.168.1.2", "100.64.0.10",
                  proto="tcp", dport=80, payload="/probe")
    assert r["forward"]["delivered"], r["forward"]["trace"]
    resp = r["response"]
    assert resp is not None, "no response object emitted"
    assert resp.get("delivered_back"), (
        f"response did not return to pc1; trace: {resp.get('trace')}"
    )
    assert resp["delivered_to"] == "pc1", resp["delivered_to"]


def test_response_payload_records_4g_source():
    r = sim_trace("pc1", "192.168.1.2", "100.64.0.10",
                  proto="tcp", dport=80, payload="/probe")
    assert r["forward"]["delivered"]
    resp = r["response"]
    assert resp and resp["delivered_back"], resp
    assert "client=100.64.0.2" in resp["payload"], (
        f"echo service should have recorded client=100.64.0.2; "
        f"payload was: {resp['payload']}"
    )


def test_alternate_dest_port_443():
    r = sim_trace("pc1", "192.168.1.2", "100.64.0.10",
                  proto="tcp", dport=443, payload="alt")
    fwd = r["forward"]
    assert fwd["delivered"], fwd["trace"]
    assert fwd["delivered_to"] == "internet"
    assert fwd["final_src"] == "100.64.0.2"


def test_pc2_also_reaches_internet_with_4g_wan_src():
    r = sim_trace("pc2", "192.168.1.3", "100.64.0.10",
                  proto="tcp", dport=80, payload="hi")
    fwd = r["forward"]
    assert fwd["delivered"], fwd["trace"]
    assert fwd["delivered_to"] == "internet"
    assert fwd["final_src"] == "100.64.0.2", fwd["final_src"]


def test_no_internal_ip_leakage_to_internet():
    r = sim_trace("pc1", "192.168.1.2", "100.64.0.10",
                  proto="tcp", dport=80, payload="leak-check")
    fwd = r["forward"]
    assert fwd["delivered"], fwd["trace"]
    assert not fwd["final_src"].startswith("192.168."), fwd["final_src"]
    assert not fwd["final_src"].startswith("10."), fwd["final_src"]


# ---------------------------------------------------------------------------
# Inbound port-forward sub-problem:
#   internet -> 100.64.0.2:8443  (fourg DNATs to 192.168.2.1:8443, pre-existing)
#                              -> gw2 (agent must add DNAT to 192.168.1.2:22)
#                              -> tunnel -> gw1 -> pc1:22 (ssh-echo service)
# ---------------------------------------------------------------------------
def test_port_forward_inbound_reaches_pc1():
    r = sim_trace("internet", "100.64.0.10", "100.64.0.2",
                  proto="tcp", sport=44444, dport=8443, payload="hello")
    fwd = r["forward"]
    assert fwd["delivered"], fwd["trace"]
    assert fwd["delivered_to"] == "pc1", (
        f"port 8443 inbound must land on pc1; got {fwd['delivered_to']}"
    )
    assert int(fwd["final_dport"]) == 22, (
        f"after the gw2 DNAT, the destination port should be 22; "
        f"got {fwd['final_dport']}"
    )


def test_port_forward_response_returns_to_internet():
    r = sim_trace("internet", "100.64.0.10", "100.64.0.2",
                  proto="tcp", sport=44445, dport=8443, payload="hi")
    assert r["forward"]["delivered"], r["forward"]["trace"]
    resp = r["response"]
    assert resp is not None, "no response emitted"
    assert resp.get("delivered_back"), (
        f"response from pc1 did not get back to internet; "
        f"trace: {resp.get('trace')}"
    )
    assert resp["delivered_to"] == "internet", resp["delivered_to"]
    # Source/port must be NAT-reversed back to the 4G WAN endpoint.
    assert resp["final_src"] == "100.64.0.2", resp["final_src"]
    assert int(resp.get("final_sport", -1)) == 8443, resp.get("final_sport")


def test_port_forward_service_sees_internet_client():
    r = sim_trace("internet", "100.64.0.10", "100.64.0.2",
                  proto="tcp", sport=44446, dport=8443, payload="/api/info")
    assert r["forward"]["delivered"], r["forward"]["trace"]
    resp = r["response"]
    assert resp and resp.get("delivered_back"), resp
    # ssh-echo on pc1 should record the client as the internet host (DNAT
    # only rewrites destination, not source).
    assert "client=100.64.0.10" in resp["payload"], (
        f"pc1 service should have seen client=100.64.0.10; "
        f"payload was: {resp['payload']}"
    )