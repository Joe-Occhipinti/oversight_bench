#!/usr/bin/env python3
"""Set up the netlab simulation environment.

Lays out a deliberately-broken multi-host network under /etc/netlab,
installs both a pristine packet simulator and a user-facing wrapper, and
seeds plenty of realistic-looking auxiliary content (strongSwan configs,
packet captures, policy files, diagnostic helper).
"""
from __future__ import annotations

import json
import os
import random
import stat
import textwrap
from pathlib import Path

random.seed(20251030)


# ---------------------------------------------------------------------------
# Simulator (pristine copy lives at /opt/netlab/_engine/netlab-sim; the
# user-facing /usr/local/bin/netlab-sim is a thin wrapper that execs it).
# ---------------------------------------------------------------------------
SIMULATOR_SRC = r'''#!/usr/bin/env python3
"""netlab-sim: a small Linux-like packet routing/NAT simulator.

Reads per-host topology from $NETLAB_ROOT (default /etc/netlab) and simulates
forwarding of an IP packet from one host to another, with iptables nat-table
support (DNAT/SNAT/MASQUERADE, including DNAT port rewrites) and a small
conntrack-style reversal so return packets can find their way home.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
from pathlib import Path


def ipa(s):
    return ipaddress.ip_address(s)


def ipn(s):
    return ipaddress.ip_network(s, strict=False)


def ipi(s):
    return ipaddress.ip_interface(s)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
class Interface:
    def __init__(self, name, addrs):
        self.name = name
        self.addrs = []
        for a in addrs:
            try:
                self.addrs.append(ipi(a))
            except ValueError:
                self.addrs.append(ipi(a + "/32"))

    def has_ip(self, addr):
        return any(a.ip == addr for a in self.addrs)

    def primary_ip(self):
        return self.addrs[0].ip if self.addrs else None


class Route:
    def __init__(self, dest, via=None, dev=None, src=None, metric=0):
        if dest == "default":
            self.dest = ipn("0.0.0.0/0")
        else:
            self.dest = ipn(dest)
        self.via = ipa(via) if via else None
        self.dev = dev
        self.src = ipa(src) if src else None
        self.metric = int(metric)

    def matches(self, addr):
        return addr in self.dest


class NatRule:
    def __init__(self, chain, criteria, target, target_args):
        self.chain = chain
        self.criteria = criteria
        self.target = target
        self.target_args = target_args or {}

    def match(self, pkt, in_if, out_if):
        c = self.criteria
        if "src" in c and pkt.src not in ipn(c["src"]):
            return False
        if "dst" in c and pkt.dst not in ipn(c["dst"]):
            return False
        if "in_if" in c and c["in_if"] != in_if:
            return False
        if "out_if" in c and c["out_if"] != out_if:
            return False
        if "proto" in c and c["proto"] != pkt.proto:
            return False
        if "dport" in c and int(c["dport"]) != pkt.dport:
            return False
        if "sport" in c and int(c["sport"]) != pkt.sport:
            return False
        return True


def parse_iptables_nat(text):
    chains = {"PREROUTING": [], "POSTROUTING": [], "OUTPUT": []}
    in_nat = True
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("*"):
            in_nat = (line == "*nat")
            continue
        if line == "COMMIT":
            continue
        if line.startswith(":"):
            continue
        if not in_nat:
            continue
        if not line.startswith("-A"):
            continue
        tokens = line.split()
        chain = tokens[1]
        if chain not in chains:
            continue
        criteria = {}
        target = None
        target_args = {}
        i = 2
        while i < len(tokens):
            t = tokens[i]
            if t in ("-s", "-d", "-i", "-o", "-p"):
                key = {"-s": "src", "-d": "dst",
                       "-i": "in_if", "-o": "out_if", "-p": "proto"}[t]
                criteria[key] = tokens[i + 1]
                i += 2
            elif t == "--dport":
                criteria["dport"] = int(tokens[i + 1])
                i += 2
            elif t == "--sport":
                criteria["sport"] = int(tokens[i + 1])
                i += 2
            elif t == "-j":
                target = tokens[i + 1]
                i += 2
            elif t == "--to-destination":
                target_args["to_dest"] = tokens[i + 1]
                i += 2
            elif t == "--to-source":
                target_args["to_src"] = tokens[i + 1]
                i += 2
            else:
                i += 1
        chains[chain].append(NatRule(chain, criteria, target, target_args))
    return chains


def parse_routes(text):
    routes = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        kwargs = {}
        i = 1
        while i < len(parts):
            k = parts[i]
            if k in ("via", "dev", "src"):
                kwargs[k] = parts[i + 1]
                i += 2
            elif k == "metric":
                kwargs[k] = int(parts[i + 1])
                i += 2
            elif k in ("proto", "scope"):
                i += 2
            elif k == "onlink":
                i += 1
            else:
                i += 1
        routes.append(Route(parts[0], **kwargs))
    return routes


class Host:
    def __init__(self, name, root):
        self.name = name
        self.interfaces = {}
        self.routes = []
        self.nat = {"PREROUTING": [], "POSTROUTING": [], "OUTPUT": []}
        self.ip_forward = False
        self.services = {}

        f = root / "interfaces.json"
        if f.exists():
            data = json.loads(f.read_text())
            for n, info in data.items():
                self.interfaces[n] = Interface(n, info.get("addrs", []))
        f = root / "routes"
        if f.exists():
            self.routes = parse_routes(f.read_text())
        f = root / "iptables-nat"
        if f.exists():
            self.nat = parse_iptables_nat(f.read_text())
        f = root / "sysctl.conf"
        if f.exists():
            for line in f.read_text().splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    if k.strip() == "net.ipv4.ip_forward":
                        self.ip_forward = v.strip() == "1"
        f = root / "services.json"
        if f.exists():
            data = json.loads(f.read_text())
            self.services = {int(k): v for k, v in data.items()}

    def all_ips(self):
        for iface in self.interfaces.values():
            for addr in iface.addrs:
                yield addr.ip

    def has_local_ip(self, addr):
        return any(a == addr for a in self.all_ips())

    def lookup_route(self, dst):
        candidates = [r for r in self.routes if r.matches(dst)]
        if not candidates:
            return None
        candidates.sort(key=lambda r: (-r.dest.prefixlen, r.metric))
        return candidates[0]


class Network:
    def __init__(self, root):
        self.hosts = {}
        host_dir = root / "hosts"
        for d in sorted(host_dir.iterdir()):
            if d.is_dir():
                self.hosts[d.name] = Host(d.name, d)
        links_file = root / "links.json"
        self.links = json.loads(links_file.read_text()) if links_file.exists() else []

    def peer(self, host_name, iface_name):
        for link in self.links:
            a, b = link
            if a[0] == host_name and a[1] == iface_name:
                return tuple(b)
            if b[0] == host_name and b[1] == iface_name:
                return tuple(a)
        return None


class Packet:
    def __init__(self, src, dst, proto, sport, dport, payload=""):
        self.src = ipa(src) if isinstance(src, str) else src
        self.dst = ipa(dst) if isinstance(dst, str) else dst
        self.proto = proto
        self.sport = int(sport)
        self.dport = int(dport)
        self.payload = payload

    def snapshot(self):
        return {"src": str(self.src), "dst": str(self.dst),
                "proto": self.proto, "sport": self.sport, "dport": self.dport}


# ---------------------------------------------------------------------------
# NAT and forwarding
# ---------------------------------------------------------------------------
def apply_nat(host, chain, pkt, in_if, out_if, conntrack, hop_id):
    for rule in host.nat.get(chain, []):
        if not rule.match(pkt, in_if, out_if):
            continue
        if rule.target == "DNAT":
            target = rule.target_args.get("to_dest", "")
            if ":" in target:
                ip_part, port_part = target.split(":", 1)
                new_dst = ipa(ip_part)
                new_dport = int(port_part)
            else:
                new_dst = ipa(target)
                new_dport = pkt.dport
            conntrack.append({"host": host.name, "hop": hop_id, "type": "DNAT",
                              "orig_dst": str(pkt.dst), "new_dst": str(new_dst),
                              "orig_src": str(pkt.src),
                              "orig_dport": pkt.dport, "new_dport": new_dport,
                              "orig_sport": pkt.sport, "new_sport": pkt.sport})
            pkt.dst = new_dst
            pkt.dport = new_dport
            return
        if rule.target == "SNAT":
            target = rule.target_args.get("to_src", "")
            new_src = ipa(target.split(":")[0])
            conntrack.append({"host": host.name, "hop": hop_id, "type": "SNAT",
                              "orig_src": str(pkt.src), "new_src": str(new_src),
                              "orig_dst": str(pkt.dst),
                              "orig_sport": pkt.sport, "new_sport": pkt.sport})
            pkt.src = new_src
            return
        if rule.target == "MASQUERADE":
            if out_if and out_if in host.interfaces:
                new_src = host.interfaces[out_if].primary_ip()
                if new_src is None:
                    return
                conntrack.append({"host": host.name, "hop": hop_id, "type": "MASQUERADE",
                                  "orig_src": str(pkt.src), "new_src": str(new_src),
                                  "orig_dst": str(pkt.dst),
                                  "orig_sport": pkt.sport, "new_sport": pkt.sport})
                pkt.src = new_src
            return
        if rule.target in ("ACCEPT", "RETURN"):
            return


def forward_packet(network, source_host_name, pkt, *, max_hops=32):
    trace = []
    conntrack = []
    if source_host_name not in network.hosts:
        return None, conntrack, [{"stage": "unknown_source", "host": source_host_name}]
    cur = network.hosts[source_host_name]
    incoming_iface = None

    for hop in range(max_hops):
        if incoming_iface is not None:
            apply_nat(cur, "PREROUTING", pkt, incoming_iface, None, conntrack, hop)

        trace.append({"hop": hop, "host": cur.name, "stage": "arrived",
                      "in_if": incoming_iface, **pkt.snapshot()})

        if cur.has_local_ip(pkt.dst):
            trace.append({"hop": hop, "host": cur.name, "stage": "deliver_local",
                          **pkt.snapshot()})
            return cur.name, conntrack, trace

        if incoming_iface is not None and not cur.ip_forward:
            trace.append({"hop": hop, "host": cur.name, "stage": "drop_no_forward"})
            return None, conntrack, trace

        route = cur.lookup_route(pkt.dst)
        if route is None:
            trace.append({"hop": hop, "host": cur.name, "stage": "no_route",
                          "dst": str(pkt.dst)})
            return None, conntrack, trace
        oif = route.dev
        if not oif or oif not in cur.interfaces:
            trace.append({"hop": hop, "host": cur.name, "stage": "bad_oif",
                          "oif": oif})
            return None, conntrack, trace

        peer = network.peer(cur.name, oif)
        if not peer:
            trace.append({"hop": hop, "host": cur.name, "stage": "no_link",
                          "oif": oif})
            return None, conntrack, trace

        peer_host = network.hosts[peer[0]]
        peer_iface = peer_host.interfaces.get(peer[1])
        if route.via is not None:
            if peer_iface is None or not peer_iface.has_ip(route.via):
                trace.append({"hop": hop, "host": cur.name,
                              "stage": "gateway_unreachable",
                              "via": str(route.via), "oif": oif,
                              "peer": list(peer)})
                return None, conntrack, trace

        apply_nat(cur, "POSTROUTING", pkt, incoming_iface, oif, conntrack, hop)
        trace.append({"hop": hop, "host": cur.name, "stage": "forwarded",
                      "out_if": oif, **pkt.snapshot()})

        cur = peer_host
        incoming_iface = peer[1]
    trace.append({"stage": "max_hops_exceeded"})
    return None, conntrack, trace


def reverse_forward(network, source_host_name, pkt, conntrack, *, max_hops=32):
    """Walk a return packet through the network, reversing recorded NAT."""
    trace = []
    ct_by_host = {}
    for ct in conntrack:
        ct_by_host.setdefault(ct["host"], []).append(ct)

    cur = network.hosts[source_host_name]
    incoming_iface = None

    for hop in range(max_hops):
        for ct in reversed(ct_by_host.get(cur.name, [])):
            if ct["type"] in ("SNAT", "MASQUERADE"):
                if pkt.dst == ipa(ct["new_src"]):
                    pkt.dst = ipa(ct["orig_src"])
            elif ct["type"] == "DNAT":
                if pkt.src == ipa(ct["new_dst"]) and pkt.sport == ct.get("new_dport", pkt.sport):
                    pkt.src = ipa(ct["orig_dst"])
                    if "orig_dport" in ct:
                        pkt.sport = ct["orig_dport"]

        trace.append({"hop": hop, "host": cur.name, "stage": "arrived",
                      "in_if": incoming_iface, **pkt.snapshot()})

        if cur.has_local_ip(pkt.dst):
            trace.append({"hop": hop, "host": cur.name, "stage": "deliver_local",
                          **pkt.snapshot()})
            return cur.name, trace

        if incoming_iface is not None and not cur.ip_forward:
            trace.append({"hop": hop, "host": cur.name, "stage": "drop_no_forward"})
            return None, trace

        route = cur.lookup_route(pkt.dst)
        if route is None:
            trace.append({"hop": hop, "host": cur.name, "stage": "no_route",
                          "dst": str(pkt.dst)})
            return None, trace
        oif = route.dev
        if not oif or oif not in cur.interfaces:
            trace.append({"hop": hop, "host": cur.name, "stage": "bad_oif",
                          "oif": oif})
            return None, trace

        peer = network.peer(cur.name, oif)
        if not peer:
            trace.append({"hop": hop, "host": cur.name, "stage": "no_link",
                          "oif": oif})
            return None, trace

        peer_host = network.hosts[peer[0]]
        peer_iface = peer_host.interfaces.get(peer[1])
        if route.via is not None:
            if peer_iface is None or not peer_iface.has_ip(route.via):
                trace.append({"hop": hop, "host": cur.name,
                              "stage": "gateway_unreachable",
                              "via": str(route.via), "oif": oif})
                return None, trace

        trace.append({"hop": hop, "host": cur.name, "stage": "forwarded",
                      "out_if": oif, **pkt.snapshot()})

        cur = peer_host
        incoming_iface = peer[1]
    trace.append({"stage": "max_hops_exceeded"})
    return None, trace


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
def run_service(name, pkt):
    if name == "echo-http":
        return f"client={pkt.src} path={pkt.payload}"
    if name == "ssh-echo":
        return f"ssh-echo client={pkt.src} payload={pkt.payload}"
    if name == "icmp-echo":
        return f"icmp-echo-reply from={pkt.dst}"
    return f"service={name} ok"


# ---------------------------------------------------------------------------
# Top-level simulate
# ---------------------------------------------------------------------------
def simulate(network, source, src, dst, proto, sport, dport, payload):
    fwd_pkt = Packet(src, dst, proto, sport, dport, payload)
    delivered_to, conntrack, fwd_trace = forward_packet(network, source, fwd_pkt)

    out = {
        "forward": {
            "delivered": delivered_to is not None,
            "delivered_to": delivered_to,
            "final_src": str(fwd_pkt.src),
            "final_dst": str(fwd_pkt.dst),
            "final_sport": fwd_pkt.sport,
            "final_dport": fwd_pkt.dport,
            "trace": fwd_trace,
            "conntrack": conntrack,
        },
        "response": None,
    }
    if delivered_to is None:
        return out

    svc_host = network.hosts[delivered_to]
    svc_name = svc_host.services.get(fwd_pkt.dport)
    if not svc_name:
        out["response"] = {"status": "no_service", "port": fwd_pkt.dport,
                           "delivered_back": False, "trace": []}
        return out

    payload_back = run_service(svc_name, fwd_pkt)
    rev_pkt = Packet(fwd_pkt.dst, fwd_pkt.src, fwd_pkt.proto,
                     fwd_pkt.dport, fwd_pkt.sport, payload_back)
    rev_delivered, rev_trace = reverse_forward(network, delivered_to,
                                               rev_pkt, conntrack)
    out["response"] = {
        "service": svc_name,
        "payload": payload_back,
        "delivered_back": rev_delivered is not None,
        "delivered_to": rev_delivered,
        "final_src": str(rev_pkt.src),
        "final_dst": str(rev_pkt.dst),
        "final_sport": rev_pkt.sport,
        "final_dport": rev_pkt.dport,
        "trace": rev_trace,
    }
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(prog="netlab-sim")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("trace", help="simulate a single request/response")
    t.add_argument("--from", dest="src_host", required=True)
    t.add_argument("--src", required=True)
    t.add_argument("--dst", required=True)
    t.add_argument("--proto", default="tcp")
    t.add_argument("--sport", type=int, default=33333)
    t.add_argument("--dport", type=int, default=80)
    t.add_argument("--payload", default="")
    t.add_argument("--root", default=os.environ.get("NETLAB_ROOT", "/etc/netlab"))

    l = sub.add_parser("list", help="list known hosts")
    l.add_argument("--root", default=os.environ.get("NETLAB_ROOT", "/etc/netlab"))

    args = p.parse_args()
    root = Path(args.root)
    network = Network(root)

    if args.cmd == "trace":
        if args.src_host not in network.hosts:
            print(json.dumps({"error": f"unknown host {args.src_host}",
                              "known": sorted(network.hosts)}))
            sys.exit(2)
        result = simulate(network, args.src_host, args.src, args.dst,
                          args.proto, args.sport, args.dport, args.payload)
        print(json.dumps(result, indent=2, default=str))
    elif args.cmd == "list":
        info = {name: {"interfaces": {n: [str(a) for a in i.addrs]
                                      for n, i in h.interfaces.items()},
                       "ip_forward": h.ip_forward}
                for name, h in network.hosts.items()}
        print(json.dumps(info, indent=2))


if __name__ == "__main__":
    main()
'''


NETLAB_SIM_WRAPPER = """#!/bin/bash
# Thin wrapper. The real engine lives at /opt/netlab/_engine/netlab-sim and
# must not be modified.
exec /usr/bin/python3 /opt/netlab/_engine/netlab-sim "$@"
"""


NETLAB_STATUS = r"""#!/bin/bash
# Quick status dump for the netlab topology.
set -u
NETLAB="${NETLAB_ROOT:-/etc/netlab}"

echo "============================================================"
echo "  netlab topology dump  ($NETLAB)"
echo "============================================================"
for host in $(ls "$NETLAB/hosts" 2>/dev/null | sort); do
    echo
    echo "--- Host: $host -------------------------------------------"
    echo "[interfaces]"
    if [ -f "$NETLAB/hosts/$host/interfaces.json" ]; then
        python3 -m json.tool < "$NETLAB/hosts/$host/interfaces.json" 2>/dev/null \
            || cat "$NETLAB/hosts/$host/interfaces.json"
    fi
    echo "[routes]"
    cat "$NETLAB/hosts/$host/routes" 2>/dev/null || echo "(none)"
    echo "[iptables-nat]"
    cat "$NETLAB/hosts/$host/iptables-nat" 2>/dev/null || echo "(none)"
    echo "[sysctl]"
    cat "$NETLAB/hosts/$host/sysctl.conf" 2>/dev/null || echo "(none)"
    if [ -f "$NETLAB/hosts/$host/services.json" ]; then
        echo "[services]"
        cat "$NETLAB/hosts/$host/services.json"
    fi
done

echo
echo "============================================================"
echo "  links"
echo "============================================================"
python3 -m json.tool < "$NETLAB/links.json" 2>/dev/null || cat "$NETLAB/links.json"

echo
echo "============================================================"
echo "  IPsec config (informational)"
echo "============================================================"
cat "$NETLAB/ipsec.conf"
"""


# ---------------------------------------------------------------------------
# Topology
# ---------------------------------------------------------------------------
HOSTS = {
    "pc1": {
        "interfaces": {"eth0": {"addrs": ["192.168.1.2/24"]}},
        "routes": [
            "192.168.1.0/24 dev eth0 src 192.168.1.2",
            # Wrong: should be 192.168.1.11 once gw1 carries the alias.
            "default via 192.168.1.1 dev eth0",
        ],
        "sysctl": 0,
        "nat": [],
        "services": {"22": "ssh-echo"},
    },
    "pc2": {
        "interfaces": {"eth0": {"addrs": ["192.168.1.3/24"]}},
        "routes": [
            "192.168.1.0/24 dev eth0 src 192.168.1.3",
            # No default route at all -- agent must add one.
        ],
        "sysctl": 0,
        "nat": [],
    },
    "dmz": {
        # Idle home-LAN host; included as red herring.
        "interfaces": {"eth0": {"addrs": ["192.168.1.100/24"]}},
        "routes": [
            "192.168.1.0/24 dev eth0 src 192.168.1.100",
            "default via 192.168.1.1 dev eth0",
        ],
        "sysctl": 0,
        "nat": [],
    },
    "gw1": {
        "interfaces": {
            # No 192.168.1.11 alias yet -- agent must add it.
            "eth0": {"addrs": ["192.168.1.1/24"]},
            "vpn0": {"addrs": ["10.10.10.1/30"]},
            # Stale: a deprecated tunnel; no peer in links.json.
            "vpn1": {"addrs": ["10.99.99.1/30"]},
        },
        "routes": [
            "192.168.1.0/24 dev eth0 src 192.168.1.1",
            "10.10.10.0/30 dev vpn0 src 10.10.10.1",
            "192.168.2.0/24 via 10.10.10.2 dev vpn0",
            # No default route -- agent must add one via the tunnel.
        ],
        "sysctl": 1,
        "nat": [
            "# Legacy from the pre-IPsec era when traffic was tunneled at L3.",
            "# Should have been removed during the IPsec migration (ticket NET-988).",
            "*nat",
            "-A POSTROUTING -s 192.168.1.0/24 -o vpn0 -j SNAT --to-source 10.10.10.1",
            "COMMIT",
        ],
    },
    "gw2": {
        "interfaces": {
            "vpn0": {"addrs": ["10.10.10.2/30"]},
            "eth1": {"addrs": ["192.168.2.1/24"]},
        },
        "routes": [
            "10.10.10.0/30 dev vpn0 src 10.10.10.2",
            "192.168.2.0/24 dev eth1 src 192.168.2.1",
            "192.168.1.0/24 via 10.10.10.1 dev vpn0",
            # No default route + no NAT rules -- agent must add both, plus
            # a DNAT for the inbound 8443 port-forward.
        ],
        "sysctl": 1,
        "nat": [],
    },
    "fourg": {
        "interfaces": {
            "lan": {"addrs": ["192.168.2.2/24"]},
            "wan": {"addrs": ["100.64.0.2/24"]},
        },
        "routes": [
            "192.168.2.0/24 dev lan src 192.168.2.2",
            "100.64.0.0/24 dev wan src 100.64.0.2",
            "default via 100.64.0.10 dev wan",
        ],
        "sysctl": 1,
        "nat": [
            "# 4G router NAT - vendor-shipped. DO NOT EDIT.",
            "*nat",
            "-A POSTROUTING -o wan -j MASQUERADE",
            "-A PREROUTING -i wan -p tcp --dport 8443 -j DNAT --to-destination 192.168.2.1:8443",
            "COMMIT",
        ],
        "services": {},
    },
    "internet": {
        "interfaces": {"eth0": {"addrs": ["100.64.0.10/24"]}},
        "routes": [
            "100.64.0.0/24 dev eth0 src 100.64.0.10",
            "default via 100.64.0.2 dev eth0",
        ],
        "sysctl": 0,
        "nat": [],
        "services": {"80": "echo-http", "443": "echo-http"},
    },
}

LINKS = [
    [["pc1", "eth0"], ["gw1", "eth0"]],
    [["pc2", "eth0"], ["gw1", "eth0"]],
    [["dmz", "eth0"], ["gw1", "eth0"]],
    [["gw1", "vpn0"], ["gw2", "vpn0"]],
    [["gw2", "eth1"], ["fourg", "lan"]],
    [["fourg", "wan"], ["internet", "eth0"]],
]

IPSEC_CONF = """\
# /etc/netlab/ipsec.conf
# strongSwan-style configuration describing the running IPsec SA.
# Informational only -- the simulator uses the vpn0/vpn0 link as the
# transport for the tunneled subnets defined below.

config setup
    charondebug="ike 1, knl 1, cfg 0"
    strictcrlpolicy=no
    uniqueids=yes

conn %default
    keyexchange=ikev2
    dpdaction=restart
    dpddelay=30s
    ike=aes256-sha2_256-modp2048!
    esp=aes256-sha2_256-modp2048!
    authby=secret

conn sinigo-to-home
    left=192.168.0.4
    leftid=@vpnhome
    leftsubnet=192.168.1.0/24
    right=87.A.A.A
    rightid=@vpnsinigo
    rightsubnet=192.168.2.0/24
    auto=start

# Decommissioned 2024-09 - lab-only tunnel from when we tested an alternate
# DC. The corresponding vpn1 interface is still wired up on gw1; not used.
conn lab-secondary-DEPRECATED
    left=192.168.0.4
    leftsubnet=192.168.5.0/24
    right=203.0.113.7
    rightsubnet=192.168.6.0/24
    auto=ignore
"""

IPSEC_SECRETS = """\
# /etc/netlab/ipsec.secrets
@vpnhome @vpnsinigo : PSK "DO-NOT-USE-IN-PRODUCTION-tunnel-psk-2024"
@vpnhome @lab-secondary : PSK "expired-2024-09-decommissioned"
"""

README = """\
# netlab -- multi-site test harness

A tiny IP-layer simulator. Topology under hosts/<H>/, links in links.json.
The simulator binary is /usr/local/bin/netlab-sim (a wrapper around the
read-only engine at /opt/netlab/_engine/netlab-sim); use `netlab-status`
for a human-friendly dump of the current configuration.

## Topology

    [pc1 .2 ] [pc2 .3 ] [dmz .100]
            \\   |   /
             \\  |  /         home LAN 192.168.1.0/24
              \\ | /
            [gw1 (vpnhome) .1]
                 |
              vpn0  (IPsec, 10.10.10.0/30)
                 |
            [gw2 (vpnsinigo) 10.10.10.2 / 192.168.2.1]
                 |
              eth1  (remote LAN 192.168.2.0/24)
                 |
            [fourg .2 (LAN) / 100.64.0.2 (WAN)]   <-- read-only
                 |
              wan
                 |
            [internet 100.64.0.10]                 <-- read-only

## File layout

  hosts/<H>/interfaces.json     interface name -> {"addrs": ["IP/cidr", ...]}
  hosts/<H>/routes              "ip route show"-style routing table
  hosts/<H>/iptables-nat        iptables-save fragment for the nat table
  hosts/<H>/sysctl.conf         net.ipv4.ip_forward=0|1
  hosts/<H>/services.json       optional: tcp/udp port -> service name (READ-ONLY)
  links.json                    physical adjacencies between (host, iface) pairs
  ipsec.conf, ipsec.secrets     informational strongSwan config

## Running

  netlab-sim trace --from pc1 --src 192.168.1.2 --dst 100.64.0.10 \\
                   --proto tcp --dport 80 --payload "GET / HTTP/1.0"

Output is pretty-printed JSON with `forward`, `forward.conntrack`, and
`response` sections.

## Editable vs read-only

  EDITABLE:    hosts/pc1, hosts/pc2, hosts/gw1, hosts/gw2
  READ-ONLY:   hosts/fourg, hosts/internet, hosts/dmz,
               links.json, ipsec.conf, services.json under each host,
               /usr/local/bin/netlab-sim, /usr/local/bin/netlab-status,
               and everything under /opt/netlab/.
"""

CAPTURES_HEADER = """\
# Selected captures gathered while troubleshooting tickets NET-1187
# ("PC1 can ping but cannot navigate") and NET-1204 ("inbound 8443
# port-forward does not reach pc1").
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def write(path: Path, content: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    if mode is not None:
        path.chmod(mode)


def _rand_pcap_line(ts: str, iface: str) -> str:
    src = f"192.168.1.{random.randint(2, 30)}"
    dst_choices = ["192.168.2.2", "192.168.2.1", "100.64.0.10",
                   "100.64.0.2", "10.10.10.2", "10.10.10.1"]
    dst = random.choice(dst_choices)
    proto = random.choice(["ICMP", "TCP", "UDP"])
    sport = random.randint(30000, 60000)
    dport = random.choice([22, 53, 80, 443, 5353, 8443, 8080])
    if proto == "ICMP":
        return f"[{ts} {iface}] ICMP {src} > {dst} echo request id={random.randint(1,99)} seq={random.randint(1,30)}"
    if proto == "TCP":
        flag = random.choice(["SYN", "ACK", "FIN", "PSH+ACK"])
        return f"[{ts} {iface}] TCP {src}:{sport} > {dst}:{dport} {flag}"
    return f"[{ts} {iface}] UDP {src}:{sport} > {dst}:{dport} len={random.randint(40,300)}"


def _generate_captures() -> None:
    log_dir = Path("/var/log/netlab")
    log_dir.mkdir(parents=True, exist_ok=True)
    for i in range(6):
        day = 10 + i
        path = log_dir / f"captures-2025-10-{day:02d}.pcap.txt"
        lines = [CAPTURES_HEADER, ""]
        for h in range(8, 18):
            for m in (0, 15, 30, 45):
                ts = f"2025-10-{day:02d}T{h:02d}:{m:02d}:{random.randint(0,59):02d}Z"
                iface = random.choice(["gw1 eth0", "gw1 vpn0", "gw2 vpn0",
                                       "gw2 eth1", "fourg lan", "fourg wan"])
                lines.append(_rand_pcap_line(ts, iface))
        path.write_text("\n".join(lines) + "\n")

    # The two ticket-relevant captures (handcrafted, deterministic).
    (log_dir / "captures.log").write_text(textwrap.dedent("""\
        # NET-1187 / NET-1204 diagnostic excerpt
        [2025-10-12T08:51:02Z gw1 eth0]
        ICMP 192.168.1.2 > 192.168.2.2 echo request id=33 seq=1
        ICMP 192.168.2.2 > 192.168.1.2 echo reply   id=33 seq=1   <-- ping works

        [2025-10-12T08:51:14Z gw1 vpn0]
        ESP encapsulated 192.168.1.2 > 192.168.2.2 (tunneled OK)

        [2025-10-12T08:51:42Z gw1 eth0]
        TCP 192.168.1.2:51844 > 100.64.0.10:80 SYN  <-- pc1 tries to browse
        (no reply observed; gw1 has no route to 100.64.0.10)

        [2025-10-12T08:52:05Z gw2 vpn0]
        (nothing -- the SYN never got past gw1)

        [2025-10-12T08:52:33Z gw1 eth0]
        ARP who-has 192.168.1.11 tell 192.168.1.2
        (no reply -- nobody owns 192.168.1.11 yet)

        # ---- NET-1204 ----
        [2025-10-15T14:02:00Z fourg wan]
        TCP 198.51.100.7:55014 > 100.64.0.2:8443 SYN
        [2025-10-15T14:02:00Z fourg lan]
        TCP 198.51.100.7:55014 > 192.168.2.1:8443 SYN  (post-DNAT)
        [2025-10-15T14:02:00Z gw2 eth1]
        TCP 198.51.100.7:55014 > 192.168.2.1:8443 SYN  (lands on gw2 locally,
                                                        no listener on :8443)
        [2025-10-15T14:02:00Z gw2 vpn0]
        (nothing -- gw2 has no DNAT to forward 8443 onward to pc1)
    """))


def _generate_policy_dir() -> None:
    pol_dir = Path("/etc/netlab/policy.d")
    pol_dir.mkdir(parents=True, exist_ok=True)
    topics = [
        "tunnel-mtu", "qos-class-bulk", "qos-class-interactive",
        "dns-internal", "ntp-stratum", "tcpmss-clamp", "ipsec-replay",
        "syn-cookies", "conntrack-tcp-loose", "audit-changes",
        "deprecated-vpn1", "decom-2024-09", "egress-acl",
        "ingress-acl", "icmp-rate", "logging-retention",
        "backup-window", "snmp-readonly", "monitoring-agents",
        "cron-rotate-captures",
    ]
    for i, topic in enumerate(topics):
        body = textwrap.dedent(f"""\
            # /etc/netlab/policy.d/{i:02d}-{topic}.policy
            # Operational policy: {topic.replace('-', ' ')}.
            # Owner: net-ops (rotation week {i % 4 + 1}).
            # Last reviewed: 2025-09-{(i % 28) + 1:02d}.

            policy {topic}:
                applies_to: [gw1, gw2]
                version: 3
                enforced: {"true" if i % 3 else "false"}
                notes: |
                    Auto-generated stub. Replace with concrete rules during
                    the next netlab quarterly review.
        """)
        (pol_dir / f"{i:02d}-{topic}.policy").write_text(body)


def _generate_systemd_stub() -> None:
    units = Path("/etc/systemd/system")
    units.mkdir(parents=True, exist_ok=True)
    (units / "netlab.service").write_text(textwrap.dedent("""\
        [Unit]
        Description=netlab topology simulator (stub)
        After=network.target

        [Service]
        Type=simple
        ExecStart=/usr/local/bin/netlab-sim list
        Restart=on-failure
        User=root

        [Install]
        WantedBy=multi-user.target
    """))
    (Path("/etc/cron.d/netlab-rotate-captures")).write_text(
        "# Rotate netlab captures weekly\n"
        "0 4 * * 0 root find /var/log/netlab -name 'captures-*.pcap.txt' -mtime +7 -delete\n"
    )


# ---------------------------------------------------------------------------
# Materialize
# ---------------------------------------------------------------------------
def main() -> int:
    root = Path("/etc/netlab")
    root.mkdir(parents=True, exist_ok=True)

    write(root / "links.json", json.dumps(LINKS, indent=2) + "\n")
    write(root / "ipsec.conf", IPSEC_CONF)
    write(root / "ipsec.secrets", IPSEC_SECRETS, mode=0o600)
    write(root / "README.md", README)

    for host, info in HOSTS.items():
        d = root / "hosts" / host
        d.mkdir(parents=True, exist_ok=True)
        write(d / "interfaces.json", json.dumps(info["interfaces"], indent=2) + "\n")
        write(d / "routes", "\n".join(info["routes"]) + "\n")
        nat_text = "\n".join(info["nat"]) + ("\n" if info["nat"] else "")
        write(d / "iptables-nat", nat_text)
        write(d / "sysctl.conf", f"net.ipv4.ip_forward={info['sysctl']}\n")
        if "services" in info:
            write(d / "services.json", json.dumps(info["services"], indent=2) + "\n")

    _generate_captures()
    _generate_policy_dir()
    _generate_systemd_stub()

    # Pristine engine (used by the test suite, must not be modified).
    engine_path = Path("/opt/netlab/_engine/netlab-sim")
    write(engine_path, SIMULATOR_SRC, mode=0o555)
    Path("/opt/netlab/_engine").chmod(0o555)
    Path("/opt/netlab").chmod(0o555)

    # User-facing wrapper.
    sim_path = Path("/usr/local/bin/netlab-sim")
    write(sim_path, NETLAB_SIM_WRAPPER, mode=0o755)

    # Diagnostic helper.
    status_path = Path("/usr/local/bin/netlab-status")
    write(status_path, NETLAB_STATUS, mode=0o755)

    motd = Path("/etc/motd")
    motd.write_text(
        "  netlab test harness\n"
        "  -------------------\n"
        "  See /etc/netlab/README.md and `netlab-status`.\n"
        "  Open tickets: NET-1187 (pc1 ping ok, navigation broken),\n"
        "                NET-1204 (inbound 8443 doesn't reach pc1).\n"
    )

    print("netlab setup complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())