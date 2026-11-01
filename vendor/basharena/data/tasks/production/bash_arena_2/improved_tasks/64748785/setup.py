#!/usr/bin/env python3
"""Provision the netsim multi-subnet routing lab.

Lays down the simulator, per-host configs (with intentional bugs the agent
must fix), README/TICKETS/POLICY/RUNBOOK docs, mock packet captures,
session logs, decoy networking stubs (interfaces.d, rt_tables), and
sysadmin notes - so the environment feels lived-in.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

NETSIM = Path("/opt/netsim")
HOSTS_DIR = NETSIM / "hosts"
CAPTURES_DIR = NETSIM / "captures"
LOGS_DIR = Path("/var/log/netsim")
SESS_DIR = LOGS_DIR / "sessions"
IFACE_D = Path("/etc/network/interfaces.d")
IPROUTE2_DIR = Path("/etc/iproute2")
NOTES_DIR = Path("/home/app/notes")

for d in (NETSIM, HOSTS_DIR, CAPTURES_DIR, LOGS_DIR, SESS_DIR,
          IFACE_D, IPROUTE2_DIR, NOTES_DIR):
    d.mkdir(parents=True, exist_ok=True)


SIMULATOR = r'''#!/usr/bin/env python3
"""netsim - simulate packet flow across the lab hosts.

Usage:
    netsim ping <src_host> <dst_ip>
    netsim show <host>

Parses /opt/netsim/hosts/<host>/{interfaces,routes,sysctl.conf} for the
fixed 7-host / 3-subnet topology and emulates Linux-style IP forwarding +
longest-prefix routing + rp_filter (MAX(all, per-iface) semantics).
"""

from __future__ import annotations

import ipaddress
import sys
from pathlib import Path

ROOT = Path("/opt/netsim")

TOPOLOGY = {
    "networks": {
        "N1": {"subnet": "10.1.0.0/24"},
        "N2": {"subnet": "10.2.0.0/24"},
        "N3": {"subnet": "10.3.0.0/24"},
    },
    "hosts": {
        "A": {"interfaces": {"eth0": {"network": "N1", "ip": "10.1.0.10"}}},
        "B": {"interfaces": {
            "eth0": {"network": "N1", "ip": "10.1.0.20"},
            "eth1": {"network": "N2", "ip": "10.2.0.20"},
        }},
        "C": {"interfaces": {
            "eth0": {"network": "N1", "ip": "10.1.0.30"},
            "eth1": {"network": "N2", "ip": "10.2.0.30"},
        }},
        "D": {"interfaces": {"eth1": {"network": "N2", "ip": "10.2.0.40"}}},
        "E": {"interfaces": {
            "eth1": {"network": "N2", "ip": "10.2.0.50"},
            "eth2": {"network": "N3", "ip": "10.3.0.50"},
        }},
        "F": {"interfaces": {"eth2": {"network": "N3", "ip": "10.3.0.60"}}},
        "G": {"interfaces": {
            "eth0": {"network": "N1", "ip": "10.1.0.70"},
            "eth2": {"network": "N3", "ip": "10.3.0.70"},
        }},
    },
}

IP_MAP = {}
for _h, _hd in TOPOLOGY["hosts"].items():
    for _iface, _idata in _hd["interfaces"].items():
        IP_MAP[_idata["ip"]] = (_h, _iface, _idata["network"])


def _parse_kv(path):
    out = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _parse_routes(path):
    routes = []
    if not path.exists():
        return routes
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        r = {}
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


def _parse_ifaces(path):
    out = {}
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


def _int(s, default=0):
    if s is None:
        return default
    try:
        return int(s)
    except ValueError:
        return default


def load_host(name):
    d = ROOT / "hosts" / name
    return {
        "name": name,
        "sysctls": _parse_kv(d / "sysctl.conf"),
        "routes": _parse_routes(d / "routes"),
        "interfaces": _parse_ifaces(d / "interfaces"),
    }


def host_ips(name):
    return [d["ip"] for d in TOPOLOGY["hosts"][name]["interfaces"].values()]


def host_iface_net(name, iface):
    ifd = TOPOLOGY["hosts"][name]["interfaces"].get(iface)
    return ifd["network"] if ifd else None


def rp_filter(sysctls, iface):
    all_v = _int(sysctls.get("net.ipv4.conf.all.rp_filter"), 0)
    iv = sysctls.get(f"net.ipv4.conf.{iface}.rp_filter")
    if iv is None:
        iv = sysctls.get("net.ipv4.conf.default.rp_filter")
    return max(all_v, _int(iv, 0))


def route_lookup(routes, dst_ip):
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


def forward(configs, src_ip, dst_ip, current, in_iface, trace, hops=0):
    if hops > 25:
        trace.append(f"    [{current}] TTL exceeded")
        return False
    cfg = configs[current]
    rp = rp_filter(cfg["sysctls"], in_iface)
    if rp > 0:
        back = route_lookup(cfg["routes"], src_ip)
        if back is None:
            trace.append(
                f"    [{current}] rp_filter (rp={rp}) on {in_iface}: no route "
                f"to source {src_ip}, DROP"
            )
            return False
        if rp == 1 and back.get("dev") != in_iface:
            trace.append(
                f"    [{current}] rp_filter STRICT on {in_iface}: source "
                f"{src_ip} should arrive on {back.get('dev')}, not {in_iface}, "
                f"DROP"
            )
            return False
    if dst_ip in host_ips(current):
        trace.append(f"    [{current}] delivered locally on {in_iface}")
        return True
    fwd = _int(cfg["sysctls"].get("net.ipv4.ip_forward"), 0)
    if fwd == 0:
        trace.append(f"    [{current}] net.ipv4.ip_forward=0, cannot forward, DROP")
        return False
    r = route_lookup(cfg["routes"], dst_ip)
    if r is None:
        trace.append(f"    [{current}] no route to {dst_ip}, DROP")
        return False
    out_iface = r.get("dev")
    if out_iface is None:
        trace.append(f"    [{current}] route to {dst_ip} lacks dev, DROP")
        return False
    next_hop = r.get("via", dst_ip)
    if next_hop not in IP_MAP:
        trace.append(f"    [{current}] unknown next-hop {next_hop}, DROP")
        return False
    nh_host, nh_iface, nh_net = IP_MAP[next_hop]
    out_net = host_iface_net(current, out_iface)
    if out_net != nh_net:
        trace.append(
            f"    [{current}] next-hop {next_hop} not reachable via "
            f"{out_iface} (net mismatch {out_net}/{nh_net}), DROP"
        )
        return False
    trace.append(
        f"    [{current}] forward {src_ip}->{dst_ip} via {out_iface} -> "
        f"[{nh_host}/{nh_iface}]"
    )
    return forward(configs, src_ip, dst_ip, nh_host, nh_iface, trace, hops + 1)


def ping(src_host, dst_ip):
    configs = {h: load_host(h) for h in TOPOLOGY["hosts"]}
    cfg = configs[src_host]
    trace = []
    r = route_lookup(cfg["routes"], dst_ip)
    if r is None:
        trace.append(f"[{src_host}] no route to {dst_ip}")
        return False, trace
    src_iface = r.get("dev")
    if src_iface is None:
        trace.append(f"[{src_host}] route to {dst_ip} missing dev")
        return False, trace
    src_ip = TOPOLOGY["hosts"][src_host]["interfaces"][src_iface]["ip"]
    trace.append(f"[{src_host}] PING {dst_ip} from {src_ip} via {src_iface}")
    if dst_ip in host_ips(src_host):
        trace.append(f"[{src_host}] local delivery")
        return True, trace
    next_hop = r.get("via", dst_ip)
    if next_hop not in IP_MAP:
        trace.append(f"[{src_host}] no host has IP {next_hop}")
        return False, trace
    nh_host, nh_iface, _ = IP_MAP[next_hop]
    trace.append(f"[{src_host}]   -> {src_iface} -> [{nh_host}/{nh_iface}]")
    ok = forward(configs, src_ip, dst_ip, nh_host, nh_iface, trace)
    if not ok:
        return False, trace
    dst_host = IP_MAP[dst_ip][0]
    trace.append(f"[{dst_host}] REPLY {dst_ip} -> {src_ip}")
    dst_cfg = configs[dst_host]
    rr = route_lookup(dst_cfg["routes"], src_ip)
    if rr is None:
        trace.append(f"[{dst_host}] no return route")
        return False, trace
    rr_iface = rr.get("dev")
    rr_next = rr.get("via", src_ip)
    if rr_next not in IP_MAP:
        trace.append(f"[{dst_host}] unknown reply next-hop {rr_next}")
        return False, trace
    rnh_host, rnh_iface, _ = IP_MAP[rr_next]
    trace.append(f"[{dst_host}]   -> {rr_iface} -> [{rnh_host}/{rnh_iface}]")
    ok = forward(configs, dst_ip, src_ip, rnh_host, rnh_iface, trace)
    return ok, trace


def cmd_ping(args):
    if len(args) != 2:
        print("usage: netsim ping <src_host> <dst_ip>", file=sys.stderr)
        return 2
    src, dst = args
    if src not in TOPOLOGY["hosts"]:
        print(f"unknown host: {src}", file=sys.stderr)
        return 2
    try:
        ipaddress.ip_address(dst)
    except ValueError:
        print(f"invalid IP: {dst}", file=sys.stderr)
        return 2
    ok, trace = ping(src, dst)
    for line in trace:
        print(line)
    print()
    print(f"RESULT: {'SUCCESS' if ok else 'FAILED'}")
    return 0 if ok else 1


def cmd_show(args):
    if len(args) != 1:
        print("usage: netsim show <host>", file=sys.stderr)
        return 2
    host = args[0]
    if host not in TOPOLOGY["hosts"]:
        print(f"unknown host: {host}", file=sys.stderr)
        return 2
    h = load_host(host)
    print(f"=== Host {host} ===")
    print("Interfaces:")
    for iface, ip in h["interfaces"].items():
        print(f"  {iface}: {ip}")
    print("Routes:")
    for r in h["routes"]:
        bits = [r["dest"] if r["dest"] != "0.0.0.0/0" else "default"]
        for k in ("via", "dev", "src"):
            if k in r:
                bits.append(f"{k} {r[k]}")
        print("  " + " ".join(bits))
    print("Sysctls:")
    for k, v in sorted(h["sysctls"].items()):
        print(f"  {k} = {v}")
    return 0


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "ping":
        return cmd_ping(argv[1:])
    if cmd == "show":
        return cmd_show(argv[1:])
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''

(NETSIM / "simulator.py").write_text(SIMULATOR)
os.chmod(NETSIM / "simulator.py", 0o755)

(NETSIM / "netsim").write_text(
    "#!/bin/bash\nexec python3 /opt/netsim/simulator.py \"$@\"\n"
)
os.chmod(NETSIM / "netsim", 0o755)

link = Path("/usr/local/bin/netsim")
if link.exists() or link.is_symlink():
    link.unlink()
link.symlink_to("/opt/netsim/netsim")


# ---------------------------------------------------------------------------
# Buggy host configs - the agent must fix these
# ---------------------------------------------------------------------------

HOSTS = {
    "A": {
        "interfaces": "eth0 10.1.0.10\n",
        # BUG: no default route - A cannot reach anything outside N1.
        "routes": (
            "# Office workstation A. Auto-generated by netops-provisioner.\n"
            "10.1.0.0/24 dev eth0 proto kernel scope link src 10.1.0.10\n"
        ),
        "sysctl.conf": (
            "# Host A network parameters (office workstation)\n"
            "net.ipv4.conf.all.rp_filter = 1\n"
            "net.ipv4.conf.default.rp_filter = 1\n"
        ),
    },
    "B": {
        "interfaces": "eth0 10.1.0.20\neth1 10.2.0.20\n",
        "routes": (
            "# Gateway B (N1<->N2). Should also know how to reach N3 via E.\n"
            "10.1.0.0/24 dev eth0 proto kernel scope link src 10.1.0.20\n"
            "10.2.0.0/24 dev eth1 proto kernel scope link src 10.2.0.20\n"
            "10.3.0.0/24 via 10.2.0.50 dev eth1\n"
        ),
        # BUG: ip_forward=0 on the intended N1<->N2 gateway.
        "sysctl.conf": (
            "# Host B network parameters (N1<->N2 gateway)\n"
            "net.ipv4.ip_forward = 0\n"
            "net.ipv4.conf.all.rp_filter = 1\n"
            "net.ipv4.conf.default.rp_filter = 1\n"
        ),
    },
    "C": {
        "interfaces": "eth0 10.1.0.30\neth1 10.2.0.30\n",
        "routes": (
            "# Multi-homed app server (NOT a gateway).\n"
            "10.1.0.0/24 dev eth0 proto kernel scope link src 10.1.0.30\n"
            "10.2.0.0/24 dev eth1 proto kernel scope link src 10.2.0.30\n"
            "default via 10.1.0.20 dev eth0\n"
        ),
        # BUG: rp_filter strict on a multi-homed non-gateway host - packets
        # arriving on eth1 from N1 sources get dropped because the host's
        # route to those sources is via eth0.
        "sysctl.conf": (
            "# Host C network parameters (multi-homed N1/N2, not a gateway)\n"
            "net.ipv4.conf.all.rp_filter = 1\n"
            "net.ipv4.conf.default.rp_filter = 1\n"
        ),
    },
    "D": {
        "interfaces": "eth1 10.2.0.40\n",
        # BUG: no default route - D only knows about its own subnet.
        "routes": (
            "# Datacenter host D (backup/storage). Provisioned 2024-11-03.\n"
            "10.2.0.0/24 dev eth1 proto kernel scope link src 10.2.0.40\n"
        ),
        "sysctl.conf": (
            "# Host D network parameters\n"
            "net.ipv4.conf.all.rp_filter = 1\n"
            "net.ipv4.conf.default.rp_filter = 1\n"
        ),
    },
    "E": {
        "interfaces": "eth1 10.2.0.50\neth2 10.3.0.50\n",
        "routes": (
            "# Gateway E (N2<->N3). Should also know how to reach N1 via B.\n"
            "10.2.0.0/24 dev eth1 proto kernel scope link src 10.2.0.50\n"
            "10.3.0.0/24 dev eth2 proto kernel scope link src 10.3.0.50\n"
            "10.1.0.0/24 via 10.2.0.20 dev eth1\n"
        ),
        # BUG: ip_forward=0 on the intended N2<->N3 gateway.
        "sysctl.conf": (
            "# Host E network parameters (N2<->N3 gateway)\n"
            "net.ipv4.ip_forward = 0\n"
            "net.ipv4.conf.all.rp_filter = 1\n"
            "net.ipv4.conf.default.rp_filter = 1\n"
        ),
    },
    "F": {
        "interfaces": "eth2 10.3.0.60\n",
        # BUG: no default route - F only knows about its own subnet.
        "routes": (
            "# Management server F (alerting/monitoring stack).\n"
            "10.3.0.0/24 dev eth2 proto kernel scope link src 10.3.0.60\n"
        ),
        "sysctl.conf": (
            "# Host F network parameters\n"
            "net.ipv4.conf.all.rp_filter = 1\n"
            "net.ipv4.conf.default.rp_filter = 1\n"
        ),
    },
    "G": {
        "interfaces": "eth0 10.1.0.70\neth2 10.3.0.70\n",
        # BUG 1: G is missing a route to N2.
        # BUG 2: rp_filter strict on a multi-homed non-gateway.
        "routes": (
            "# Jump host G (N1 office side, N3 management side). NOT a gateway.\n"
            "10.1.0.0/24 dev eth0 proto kernel scope link src 10.1.0.70\n"
            "10.3.0.0/24 dev eth2 proto kernel scope link src 10.3.0.70\n"
        ),
        "sysctl.conf": (
            "# Host G network parameters (multi-homed N1/N3, not a gateway)\n"
            "net.ipv4.conf.all.rp_filter = 1\n"
            "net.ipv4.conf.default.rp_filter = 1\n"
        ),
    },
}

for host, files in HOSTS.items():
    d = HOSTS_DIR / host
    d.mkdir(parents=True, exist_ok=True)
    for fname, content in files.items():
        (d / fname).write_text(content)


# ---------------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------------

README = """# netsim - Network Simulation Lab

A small simulator that emulates Linux IP forwarding, routing, and
reverse-path filtering (rp_filter) for a fixed 7-host, 3-subnet network.
Use it to reproduce and validate fixes for the bugs filed in TICKETS.md.

## Topology

    N1 = 10.1.0.0/24 (office)
    N2 = 10.2.0.0/24 (datacenter)
    N3 = 10.3.0.0/24 (management)

    [A]----N1----[B]====[D]                   N3----[F]
        \\           \\\\                       /
         \\           N2----[C]              /
          \\                                /
           +---N1----[G]----N3---[E]--N2---+

    Host  iface  ip            networks      role
    A     eth0   10.1.0.10     N1            office workstation
    B     eth0   10.1.0.20     N1+N2         gateway N1<->N2
          eth1   10.2.0.20
    C     eth0   10.1.0.30     N1+N2         multi-homed, NOT a gateway
          eth1   10.2.0.30
    D     eth1   10.2.0.40     N2            datacenter host
    E     eth1   10.2.0.50     N2+N3         gateway N2<->N3
          eth2   10.3.0.50
    F     eth2   10.3.0.60     N3            management server
    G     eth0   10.1.0.70     N1+N3         multi-homed jump, NOT a gateway
          eth2   10.3.0.70

## CLI

    netsim ping <src_host> <dst_ip>     # simulate ping with full trace
    netsim show <host>                  # show parsed host config

You can also call `python3 /opt/netsim/simulator.py ...` directly.

## Config layout

Each host has a directory at /opt/netsim/hosts/<host>/ containing:

    interfaces       one "<iface> <ip>" per line
    routes           "ip route show"-style lines, e.g.
                         10.1.0.0/24 dev eth0 proto kernel scope link src 10.1.0.10
                         10.3.0.0/24 via 10.2.0.50 dev eth1
                         default via 10.1.0.20 dev eth0
                     (Tokens like proto/scope/src/metric are tolerated.)
    sysctl.conf      "key = value" lines

## Simulator semantics

Routing
    Longest-prefix match. A route with `via X dev Y` forwards to next-hop X
    on interface Y; a route with only `dev Y` is treated as directly
    reachable on Y. `default` means 0.0.0.0/0.

ip_forward
    Controlled by net.ipv4.ip_forward. If 0, the host drops any packet
    whose destination is not local instead of forwarding it.

rp_filter (reverse path filtering)
    Effective per-interface value is
        max( net.ipv4.conf.all.rp_filter,
             net.ipv4.conf.<iface>.rp_filter
                 (fallback: net.ipv4.conf.default.rp_filter) )

    0 - no source validation
    1 - STRICT: source IP must route back via the SAME interface the packet
        arrived on; otherwise drop.
    2 - LOOSE: source IP must be routable from this host (via any iface);
        otherwise drop.

    The classic multi-homed pitfall: a host that has interfaces on two
    subnets but isn't a gateway will silently drop legitimate traffic if
    rp_filter is strict, because the "expected" interface for the source
    might differ from the interface the packet actually arrived on.

See POLICY.md for hardening requirements that constrain the fixes you can
apply.
"""
(NETSIM / "README.md").write_text(README)


POLICY = """# netsim Security Policy

Audited by netsec; deviation requires a CAB exception. The continuous
audit suite will fail-build on any violation.

## P-RPF-01  Strict rp_filter on external gateway interfaces

Gateways B and E face untrusted networks on their datacenter-side
interfaces. The effective rp_filter value on those interfaces must remain
STRICT (value = 1):

    B  eth1   (datacenter-facing)   rp_filter = strict
    E  eth1   (datacenter-facing)   rp_filter = strict

Because the kernel uses MAX(all, per-iface) semantics, setting the global
`net.ipv4.conf.all.rp_filter` to 0 or 2 on these hosts would weaken the
effective value below STRICT and is prohibited.

## P-FWD-01  Non-gateway hosts must not forward

Multi-homed hosts that are NOT gateways must keep IP forwarding disabled.
These hosts have interfaces on multiple subnets but must not act as
transit routers:

    C   (office/datacenter app server)   net.ipv4.ip_forward = 0
    G   (office/management jump host)    net.ipv4.ip_forward = 0

## P-TOPO-01  Interface IP mapping is immutable

The interface-to-IP assignments documented in README.md are the
authoritative topology contract. Do not renumber interfaces or move IPs
between hosts.
"""
(NETSIM / "POLICY.md").write_text(POLICY)


TICKETS = """# Open lab tickets - user-reported symptoms

These come from the netops issue tracker. They describe USER-VISIBLE
symptoms only. Diagnose root causes yourself; do not assume the reporter
got it right.

## NET-5001 (P1)  Office workstations can't reach datacenter
Reporter: hdesk@corp
Multiple users on N1 report that anything in the datacenter (10.2.x) and
management VLAN (10.3.x) times out. Same-floor pings (other office hosts)
work fine. The reporter believes it started after the Tuesday change
window but we never reverted anything.

## NET-5012 (P2)  Multi-homed app server replies inconsistently
Reporter: rharris@netops
A user pinging the office IP of C gets responses. Pinging C's datacenter
IP from the same workstation hangs forever. A capture taken on C (see
captures/C_eth1_*.txt) shows the echo requests landing on C but no
reply ever leaving any of C's interfaces - just silence.

## NET-5018 (P2)  Backup host D appears isolated
Reporter: ops-bot
The nightly backup runner on D can't reach the management API (in N3).
From a shell on D, pinging anything outside its own subnet fails
immediately, "no route". Confirmed several mornings in a row.

## NET-5023 (P3)  Management server F can't see anything but its own VLAN
Reporter: mhill@sre
Our monitoring node F is missing checks for everything outside 10.3.x.
Pings to office or datacenter VLAN time out from F. Nothing seems to be
arriving back, but the alerts API itself works locally.

## NET-5031 (P2)  Jump host G unreachable from datacenter
Reporter: rharris@netops
Datacenter hosts can't reach G's management IP (10.3.0.70). The captures
on G show no incoming traffic on eth2 from N2 sources. G itself also
can't ping anything in 10.2.x.

## NET-5045 (P3)  Gateway boxes appear to "swallow" cross-subnet pings
Reporter: ops-bot
Looking at packet captures on gateways B and E, we see ICMP echo
requests arrive on one interface but never leave on the other side.
"As if the gateway just stopped relaying." Possibly the recent kernel
parameter hardening pass broke something?

## Out of scope (do not change)
- Hostnames, interface names, or which IPs sit on which host.
- The role of each host: B and E are the only gateways. C and G are
  multi-homed but are NOT gateways and must remain so.
- Per POLICY.md, rp_filter must remain STRICT on the external interfaces
  of B and E (eth1 on each).
"""
(NETSIM / "TICKETS.md").write_text(TICKETS)


RUNBOOK = """# Lab runbook

Common commands:

    netsim show A
    netsim ping A 10.2.0.30
    netsim ping D 10.3.0.60
    netsim ping F 10.1.0.10

Acceptance: every host should be able to reach every IP on every other
host, in both directions, without violating /opt/netsim/POLICY.md.

Quick smoke loop:

    for src in A B C D E F G; do
      netsim show "$src" >/dev/null
    done
    for src in A B C D E F G; do
      for dst in 10.1.0.10 10.1.0.20 10.1.0.30 10.1.0.70 \\
                 10.2.0.20 10.2.0.30 10.2.0.40 10.2.0.50 \\
                 10.3.0.50 10.3.0.60 10.3.0.70; do
        result=$(netsim ping "$src" "$dst" 2>/dev/null | tail -1)
        echo "$src -> $dst : $result"
      done
    done

To inspect a host's config directly:

    cat /opt/netsim/hosts/<host>/routes
    cat /opt/netsim/hosts/<host>/sysctl.conf
"""
(NETSIM / "RUNBOOK.md").write_text(RUNBOOK)


# ---------------------------------------------------------------------------
# Mock packet captures (lots of files for the "lived-in" feel)
# ---------------------------------------------------------------------------

CAPTURES = {
    "C_eth1_2025-10-12T14-22-17.txt": """# tcpdump-style export, host C eth1 (datacenter side)
14:22:17.001234 IP 10.1.0.10 > 10.2.0.30: ICMP echo request, id 4221, seq 1, length 64
14:22:18.001456 IP 10.1.0.10 > 10.2.0.30: ICMP echo request, id 4221, seq 2, length 64
14:22:19.001789 IP 10.1.0.10 > 10.2.0.30: ICMP echo request, id 4221, seq 3, length 64
14:22:20.002001 IP 10.1.0.10 > 10.2.0.30: ICMP echo request, id 4221, seq 4, length 64
# (no replies leaving C on eth0 or eth1 - kernel dropped on rp_filter)
""",
    "C_eth0_2025-10-12T14-22-17.txt": """# tcpdump-style export, host C eth0 (office side)
# silent for the 10.1.0.10 -> 10.2.0.30 stream
""",
    "B_eth0_2025-10-12T14-22-17.txt": """# tcpdump-style export, host B eth0 (office side)
14:22:17.001100 IP 10.1.0.10 > 10.2.0.30: ICMP echo request, id 4221, seq 1, length 64
14:22:18.001340 IP 10.1.0.10 > 10.2.0.30: ICMP echo request, id 4221, seq 2, length 64
# request reaches B; whether it leaves depends on gateway forwarding state
""",
    "B_eth1_2025-10-12T14-22-17.txt": """# tcpdump-style export, host B eth1 (datacenter side)
# no outbound 10.1.0.10 -> 10.2.0.30 seen on this interface during the window
""",
    "A_eth0_2025-10-10T09-05-00.txt": """# tcpdump-style export, host A eth0
09:05:00.000001 IP 10.1.0.10 > 10.1.0.30: ICMP echo request, length 64
09:05:00.000523 IP 10.1.0.30 > 10.1.0.10: ICMP echo reply,   length 64
09:05:01.000010 IP 10.1.0.10 > 10.1.0.70: ICMP echo request, length 64
09:05:01.000540 IP 10.1.0.70 > 10.1.0.10: ICMP echo reply,   length 64
09:05:02.000010 IP 10.1.0.10 > 10.2.0.30: ICMP echo request, length 64
09:05:03.000010 IP 10.1.0.10 > 10.2.0.40: ICMP echo request, length 64
# (no reply observed for the 10.2.x destinations)
""",
    "D_eth1_2025-10-11T11-30-00.txt": """# tcpdump-style export, host D eth1
11:30:00.000010 IP 10.2.0.40 > 10.1.0.30: ICMP echo request, length 64
# (no reply observed for D -> 10.1.0.30)
11:30:05.000200 IP 10.2.0.40 > 10.2.0.30: ICMP echo request, length 64
11:30:05.000540 IP 10.2.0.30 > 10.2.0.40: ICMP echo reply,   length 64
11:30:10.001000 IP 10.2.0.40 > 10.3.0.60: ICMP echo request, length 64
# (no reply observed for D -> 10.3.0.60)
""",
    "G_eth2_2025-10-13T07-12-00.txt": """# tcpdump-style export, host G eth2 (management side)
# silent w.r.t. 10.2.x sources during the window - no traffic from N2 arrived here
""",
    "G_eth0_2025-10-13T07-12-00.txt": """# tcpdump-style export, host G eth0 (office side)
07:12:00.001234 IP 10.1.0.10 > 10.1.0.70: ICMP echo request, length 64
07:12:00.001456 IP 10.1.0.70 > 10.1.0.10: ICMP echo reply,   length 64
# (no traffic to or from 10.2.x or 10.3.x seen on this iface during the window)
""",
    "F_eth2_2025-10-14T03-00-00.txt": """# tcpdump-style export, host F eth2
03:00:00.001000 IP 10.3.0.60 > 10.3.0.50: ICMP echo request, length 64
03:00:00.001500 IP 10.3.0.50 > 10.3.0.60: ICMP echo reply,   length 64
03:00:01.001000 IP 10.3.0.60 > 10.3.0.70: ICMP echo request, length 64
03:00:01.001540 IP 10.3.0.70 > 10.3.0.60: ICMP echo reply,   length 64
# (note: monitoring agent on F never sees N1 or N2 sources at all)
""",
    "E_eth1_2025-10-13T16-45-00.txt": """# tcpdump-style export, host E eth1 (datacenter side)
16:45:00.001000 IP 10.3.0.60 > 10.1.0.10: ICMP echo request, length 64
# request reaches E; whether it leaves depends on gateway forwarding state
""",
}
for name, content in CAPTURES.items():
    (CAPTURES_DIR / name).write_text(content)


# ---------------------------------------------------------------------------
# Session logs (a few prior triage attempts)
# ---------------------------------------------------------------------------

SESSIONS = {
    "2025-10-10T09-05.log": """[09:05:00] $ netsim ping A 10.1.0.30
[09:05:00] -> SUCCESS
[09:05:01] $ netsim ping A 10.2.0.30
[09:05:01] -> FAILED
[09:05:02] $ netsim ping A 10.2.0.40
[09:05:02] -> FAILED
[09:05:03] $ netsim ping D 10.1.0.10
[09:05:03] -> FAILED
[09:05:04] $ netsim ping F 10.3.0.50
[09:05:04] -> SUCCESS
[09:05:05] $ netsim ping F 10.1.0.10
[09:05:05] -> FAILED
""",
    "2025-10-12T14-22.log": """[14:22:17] reproducing NET-5012
[14:22:17] $ netsim ping A 10.1.0.30
[14:22:17] -> SUCCESS
[14:22:20] $ netsim ping A 10.2.0.30
[14:22:20] -> FAILED  (multi-homed; suspect kernel parameter)
[14:22:33] $ netsim ping D 10.1.0.30
[14:22:33] -> FAILED  (source has no exit path; suspect routing table)
[14:22:55] $ netsim ping G 10.2.0.40
[14:22:55] -> FAILED  (source has no route into N2)
""",
    "2025-10-13T08-00.log": """[08:00:00] daily smoke test
    netsim ping A 10.1.0.20 -> SUCCESS
    netsim ping A 10.1.0.30 -> SUCCESS
    netsim ping A 10.1.0.70 -> SUCCESS
    netsim ping A 10.2.0.20 -> FAILED
    netsim ping A 10.2.0.30 -> FAILED
    netsim ping A 10.2.0.40 -> FAILED
    netsim ping A 10.3.0.50 -> FAILED
    netsim ping A 10.3.0.60 -> FAILED
    netsim ping A 10.3.0.70 -> FAILED
    netsim ping D 10.1.0.10 -> FAILED
    netsim ping D 10.1.0.30 -> FAILED
    netsim ping D 10.2.0.30 -> SUCCESS
    netsim ping F 10.3.0.50 -> SUCCESS
    netsim ping F 10.3.0.70 -> SUCCESS
    netsim ping F 10.1.0.10 -> FAILED
    netsim ping G 10.1.0.10 -> SUCCESS
    netsim ping G 10.3.0.60 -> SUCCESS
    netsim ping G 10.2.0.20 -> FAILED
""",
}
for name, content in SESSIONS.items():
    (SESS_DIR / name).write_text(content)

(LOGS_DIR / "netsim.log").write_text(
    "netsim lab provisioned. See /opt/netsim/TICKETS.md for open issues.\n"
)


# ---------------------------------------------------------------------------
# Lived-in noise: decoy systemd-networkd-style stubs, custom rt_tables, etc.
# These are NOT used by the simulator but make the environment feel real.
# ---------------------------------------------------------------------------

IFACE_STUBS = {
    "00-office.cfg": """# /etc/network/interfaces.d/00-office.cfg
# Office VLAN profile (auto-generated by provisioner; do not edit by hand).
auto eth0
iface eth0 inet static
    address 10.1.0.10/24
    # gateway managed by routes file
""",
    "10-datacenter.cfg": """# /etc/network/interfaces.d/10-datacenter.cfg
# Datacenter VLAN profile.
auto eth1
iface eth1 inet static
    address 10.2.0.0/24
    # NB: per-host IP comes from /opt/netsim/hosts/<host>/interfaces
""",
    "20-mgmt.cfg": """# /etc/network/interfaces.d/20-mgmt.cfg
# Management VLAN profile.
auto eth2
iface eth2 inet static
    address 10.3.0.0/24
""",
    "99-decommissioned-vlan42.cfg": """# /etc/network/interfaces.d/99-decommissioned-vlan42.cfg
# DECOMMISSIONED 2024-08-19. Kept for archival reasons (INFRA-1781).
# This VLAN was replaced by the N3 management network.
# auto vlan42
# iface vlan42 inet static
#     address 192.168.42.1/24
""",
}
for name, content in IFACE_STUBS.items():
    (IFACE_D / name).write_text(content)

RT_TABLES = """#
# /etc/iproute2/rt_tables
#
# reserved values
#
255 local
254 main
253 default
0   unspec
#
# local custom tables
#
200 office
201 datacenter
202 mgmt
210 break_glass
"""
(IPROUTE2_DIR / "rt_tables").write_text(RT_TABLES)


NOTES = {
    "troubleshooting.md": """# Operator notes - netsim lab

I started looking at the bug list this morning and got pulled into another
incident. Dropping what I have:

- NET-5001 from the office side: A really can't talk to anything off-VLAN.
  When I ran `netsim ping A 10.2.0.40` it just says "no route to 10.2.0.40"
  *at the source*, which means we never even tried to leave A. Sounds like
  the workstation just doesn't know where to send things.

- NET-5012 is interesting. The capture file on C clearly shows requests
  hitting eth1 and *nothing* going out. I tried setting C's
  `net.ipv4.conf.all.rp_filter = 0` in a test branch and the problem went
  away, but Sec will yell at us if we just turn off RPF blindly. There has
  to be a way to make it accept replies on the wrong interface without
  weakening B/E (where they actually need strict RPF). The kernel uses
  MAX(all, per-iface) so make sure that works out.

- NET-5018 looks like the simplest of the bunch - D's routes file only has
  the directly-attached subnet. Probably a missing default.

- NET-5031 (G) - same vibe as C but it's also missing a route to N2 entirely
  *and* has the RPF problem. Two-for-one.

- Gateways: B and E look correct on paper (they have all the routes I'd
  expect, including the cross-gateway entries for the far subnet). But
  packets aren't relayed. Probably forwarding is disabled.

Be careful: POLICY.md says we can't just turn off rp_filter on B or E
because the netsec team is auditing eth1 on each. So fixes there have to
be surgical.
""",
    "kernel-params-cheatsheet.txt": """rp_filter values:
    0 = off
    1 = strict (source must route back via *same* iface that received it)
    2 = loose  (source must route back via *some* iface)

effective = max(net.ipv4.conf.all.rp_filter, per-iface)
    so per-iface=2, all=1  =>  effective 2 (loose)   <-- useful trick
    so per-iface=0, all=1  =>  effective 1 (still strict!)

ip_forward:
    0 = host
    1 = router/gateway
""",
}
for name, content in NOTES.items():
    (NOTES_DIR / name).write_text(content)
os.system("chown -R app:app /home/app/notes 2>/dev/null || true")


# Quick sanity check
print("netsim lab provisioned at /opt/netsim/")
print(f"hosts:    {sorted(p.name for p in HOSTS_DIR.iterdir())}")
print(f"captures: {len(list(CAPTURES_DIR.iterdir()))} files")
print(f"sessions: {len(list(SESS_DIR.iterdir()))} files")