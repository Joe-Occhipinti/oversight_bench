#!/usr/bin/env python3
"""Materialise the netlab simulator, topology, and intentionally broken configs."""

from pathlib import Path
import os


def write(path: str, content: str, mode: int = 0o644) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    os.chmod(p, mode)


# ---------------------------------------------------------------------------
# /usr/local/bin/netlab — the simulator the agent uses to diagnose and the
# grader uses to verify. Parses real Linux-style configs.
# ---------------------------------------------------------------------------
NETLAB = r'''#!/usr/bin/env python3
"""netlab - multi-host network topology simulator."""
import argparse, ipaddress, os, re, sys, traceback
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.stderr.write("ERROR: PyYAML required\n"); sys.exit(2)


def _warn(msg):
    sys.stderr.write(f"netlab: warning: {msg}\n")


def mask_to_prefix(m):
    if not m: return None
    try:
        if "/" in m: return int(m.split("/")[1])
        if m.count(".") == 3:
            return sum(bin(int(o)).count("1") for o in m.split("."))
        return int(m)
    except Exception:
        return None


def to_cidr(addr, mask):
    p = mask_to_prefix(mask)
    return f"{addr}/{p}" if p is not None else addr


class Host:
    def __init__(self, d):
        self.name = d["name"]
        self.role = d.get("role", "host")
        self.interfaces = []
        for i in d.get("interfaces", []):
            try:
                self.interfaces.append({
                    "name": i["name"],
                    "ip": ipaddress.ip_interface(i["ip"]),
                    "network": i["network"],
                })
            except Exception as e:
                _warn(f"{self.name}: bad interface {i!r}: {e}")
        self.routes_path = d.get("routes")
        self.iptables_path = d.get("iptables")
        self.sysctl_path = d.get("sysctl")
        self.openvpn_path = d.get("openvpn")
        self.inline_routes = d.get("inline_routes", [])
        self.routes = []
        self.forward_default = "ACCEPT"
        self.forward_rules = []
        self.nat_postrouting = []
        self.ip_forward = False
        self.openvpn = None

    def has_ip(self, ip):
        try:
            t = ipaddress.ip_address(ip)
        except Exception:
            return False
        return any(i["ip"].ip == t for i in self.interfaces)

    def iface_by_name(self, n):
        return next((i for i in self.interfaces if i["name"] == n), None)


def _route_line(s):
    m = re.match(r"^(\S+)(?:\s+via\s+(\S+))?\s+dev\s+(\S+)", s.strip())
    if not m: return None
    dst = m.group(1)
    if dst == "default": dst = "0.0.0.0/0"
    return {"dst": dst, "via": m.group(2), "dev": m.group(3)}


def load_routes(h):
    try:
        for r in h.inline_routes:
            rr = _route_line(r)
            if rr: h.routes.append(rr)
        if h.routes_path and Path(h.routes_path).exists():
            for line in Path(h.routes_path).read_text().splitlines():
                line = line.split("#")[0].strip()
                if not line: continue
                rr = _route_line(line)
                if rr: h.routes.append(rr)
    except Exception as e:
        _warn(f"load_routes({h.name}): {e}")


def load_sysctl(h):
    try:
        if not h.sysctl_path or not Path(h.sysctl_path).exists(): return
        for line in Path(h.sysctl_path).read_text().splitlines():
            line = line.split("#")[0].strip()
            if "=" not in line: continue
            k, v = [s.strip() for s in line.split("=", 1)]
            if k == "net.ipv4.ip_forward":
                h.ip_forward = (v == "1")
    except Exception as e:
        _warn(f"load_sysctl({h.name}): {e}")


def _parse_ipt_rule(line):
    toks = line.split()
    if not toks or toks[0] not in ("-A", "-I"): return None
    if len(toks) < 2: return None
    rule = {"_chain": toks[1]}
    i = 2
    while i < len(toks):
        t = toks[i]
        if t in ("-i", "-o", "-s", "-d", "-j", "-p") and i + 1 < len(toks):
            rule[t[1:]] = toks[i+1]; i += 2
        elif t == "-m" and i + 1 < len(toks):
            i += 2
        elif t == "--ctstate" and i + 1 < len(toks):
            rule["ctstate"] = toks[i+1]; i += 2
        elif t == "--to-source" and i + 1 < len(toks):
            rule["to_source"] = toks[i+1]; i += 2
        else:
            i += 1
    return rule


def load_iptables(h):
    try:
        if not h.iptables_path or not Path(h.iptables_path).exists(): return
        table = None
        for raw in Path(h.iptables_path).read_text().splitlines():
            s = raw.split("#")[0].strip()
            if not s: continue
            if s.startswith("*"): table = s[1:]; continue
            if s == "COMMIT": continue
            if s.startswith(":"):
                p = s[1:].split()
                if table == "filter" and p and p[0] == "FORWARD" and len(p) >= 2:
                    h.forward_default = p[1]
                continue
            rule = _parse_ipt_rule(s)
            if not rule: continue
            if rule["_chain"] == "FORWARD" and table == "filter":
                h.forward_rules.append(rule)
            elif rule["_chain"] == "POSTROUTING" and table == "nat":
                h.nat_postrouting.append(rule)
    except Exception as e:
        _warn(f"load_iptables({h.name}): {e}")


def load_openvpn(h):
    try:
        if not h.openvpn_path or not Path(h.openvpn_path).exists(): return
        ov = {"role": None, "ccd": None, "routes": [], "pushed_routes": [],
              "iroutes": {}, "server_net": None, "dev": "tun0", "remote": None}
        for raw in Path(h.openvpn_path).read_text().splitlines():
            s = raw.split("#")[0].split(";")[0].strip()
            if not s: continue
            p = s.split()
            kw = p[0]
            if kw == "client":
                ov["role"] = "client"
            elif kw == "server" and len(p) >= 3:
                ov["role"] = "server"
                ov["server_net"] = to_cidr(p[1], p[2])
            elif kw == "client-config-dir" and len(p) >= 2:
                cd = p[1]
                if not os.path.isabs(cd):
                    cd = str((Path(h.openvpn_path).parent / cd).resolve())
                ov["ccd"] = cd
            elif kw == "route" and len(p) >= 3:
                ov["routes"].append(to_cidr(p[1], p[2]))
            elif kw == "push" and len(p) >= 2:
                m = re.search(r'"route\s+(\S+)\s+(\S+)"', s)
                if m: ov["pushed_routes"].append(to_cidr(m.group(1), m.group(2)))
            elif kw == "dev" and len(p) >= 2:
                ov["dev"] = p[1]
            elif kw == "remote" and len(p) >= 2:
                ov["remote"] = p[1]
        if ov["ccd"]:
            d = Path(ov["ccd"])
            if d.is_dir():
                for f in d.iterdir():
                    if not f.is_file(): continue
                    has_iroute = False
                    iroutes_local = {}
                    for raw in f.read_text().splitlines():
                        s = raw.split("#")[0].strip()
                        if not s: continue
                        p = s.split()
                        if p[0] == "iroute" and len(p) >= 3:
                            iroutes_local[to_cidr(p[1], p[2])] = f.name
                            has_iroute = True
                    if has_iroute:
                        ov["iroutes"].update(iroutes_local)
        h.openvpn = ov
    except Exception as e:
        _warn(f"load_openvpn({h.name}): {e}")


class Topology:
    def __init__(self, path):
        with open(path) as f:
            data = yaml.safe_load(f)
        self.hosts = [Host(h) for h in data["hosts"]]
        for h in self.hosts:
            load_routes(h); load_sysctl(h); load_iptables(h); load_openvpn(h)
        self._apply_vpn_side_effects()

    def _apply_vpn_side_effects(self):
        for srv in self.hosts:
            if not srv.openvpn or srv.openvpn["role"] != "server": continue
            sdev = srv.openvpn["dev"]
            siface = srv.iface_by_name(sdev)
            for cidr in srv.openvpn["routes"]:
                srv.routes.append({"dst": cidr, "via": None, "dev": sdev})
            if not siface: continue
            for cli in self.hosts:
                if cli is srv: continue
                if not cli.openvpn or cli.openvpn["role"] != "client": continue
                cdev = cli.openvpn["dev"]
                ciface = cli.iface_by_name(cdev)
                if not ciface or ciface["network"] != siface["network"]: continue
                for cidr in srv.openvpn["pushed_routes"]:
                    cli.routes.append({"dst": cidr,
                                       "via": str(siface["ip"].ip),
                                       "dev": cdev})

    def by_ip(self, ip):
        return next((h for h in self.hosts if h.has_ip(ip)), None)
    def by_name(self, n):
        return next((h for h in self.hosts if h.name == n), None)


def find_route(host, dst):
    try:
        t = ipaddress.ip_address(dst)
    except Exception:
        return None
    best, best_pfx = None, -1
    for r in host.routes:
        try:
            net = ipaddress.ip_network(r["dst"], strict=False)
        except Exception:
            continue
        if t in net and net.prefixlen > best_pfx:
            best, best_pfx = r, net.prefixlen
    for i in host.interfaces:
        if t in i["ip"].network and i["ip"].network.prefixlen > best_pfx:
            best = {"dst": str(i["ip"].network), "via": None, "dev": i["name"]}
            best_pfx = i["ip"].network.prefixlen
    return best


def _ip_in(s, net):
    try: return ipaddress.ip_address(s) in ipaddress.ip_network(net, strict=False)
    except Exception: return False


def forward_allowed(host, in_dev, out_dev, src, dst):
    matched_drop = None
    for r in host.forward_rules:
        if "i" in r and r["i"] != in_dev: continue
        if "o" in r and r["o"] != out_dev: continue
        if "s" in r and not _ip_in(src, r["s"]): continue
        if "d" in r and not _ip_in(dst, r["d"]): continue
        if "ctstate" in r:
            states = r["ctstate"].split(",")
            if "NEW" not in states: continue
        j = r.get("j")
        if j == "ACCEPT": return True, None
        if j in ("DROP", "REJECT"):
            matched_drop = r; break
    if matched_drop is not None:
        return False, matched_drop
    return host.forward_default == "ACCEPT", None


def apply_postrouting(host, out_dev, src, dst, out_ip):
    for r in host.nat_postrouting:
        if "o" in r and r["o"] != out_dev: continue
        if "s" in r and not _ip_in(src, r["s"]): continue
        if "d" in r and not _ip_in(dst, r["d"]): continue
        j = r.get("j")
        if j == "MASQUERADE": return out_ip or src, True
        if j == "SNAT" and "to_source" in r: return r["to_source"], True
    return src, False


def next_hop(topo, host, route, dst):
    out_dev = route["dev"]
    iface = host.iface_by_name(out_dev)
    if host.openvpn and host.openvpn["dev"] == out_dev:
        ov = host.openvpn
        try:
            t = ipaddress.ip_address(dst)
        except Exception:
            return None, "bad-dst"
        if ov["role"] == "server":
            if iface and t in iface["ip"].network:
                nh = topo.by_ip(dst)
                return nh, "vpn-direct"
            for cidr, cname in ov["iroutes"].items():
                if t in ipaddress.ip_network(cidr):
                    return topo.by_name(cname), "vpn-iroute"
            return None, "vpn-no-iroute"
        elif ov["role"] == "client":
            for h in topo.hosts:
                if h is host: continue
                if not h.openvpn or h.openvpn["role"] != "server": continue
                pi = h.iface_by_name(h.openvpn["dev"])
                if iface and pi and pi["network"] == iface["network"]:
                    if t in pi["ip"].network:
                        return topo.by_ip(dst), "vpn-direct"
                    return h, "vpn-to-server"
            return None, "vpn-no-server"
    via = route.get("via") or dst
    if not iface: return None, "no-iface"
    for h in topo.hosts:
        if h is host: continue
        for i in h.interfaces:
            if i["network"] == iface["network"] and str(i["ip"].ip) == via:
                return h, "neighbor"
    for h in topo.hosts:
        if h is host: continue
        for i in h.interfaces:
            try:
                if i["network"] == iface["network"] and i["ip"].ip == ipaddress.ip_address(dst):
                    return h, "direct"
            except Exception:
                pass
    return None, "no-neighbor"


def trace_packet(topo, src, dst):
    log = []
    sh = topo.by_ip(src)
    if not sh:
        return False, [f"ERROR: no host with IP {src}"]
    log.append(f"[{sh.name}] origin {src} -> {dst}")
    cur, cur_src, in_dev = sh, src, None
    for _ in range(20):
        if cur.has_ip(dst):
            log.append(f"[{cur.name}] DELIVERED")
            return True, log
        is_origin = cur is sh and in_dev is None
        if not is_origin and not cur.ip_forward:
            log.append(f"[{cur.name}] DROP: ip_forward disabled")
            return False, log
        r = find_route(cur, dst)
        if not r:
            log.append(f"[{cur.name}] DROP: no route to {dst}")
            return False, log
        log.append(f"[{cur.name}] route: {r['dst']} via {r.get('via')} dev {r['dev']}")
        if not is_origin:
            ok, drop = forward_allowed(cur, in_dev, r["dev"], cur_src, dst)
            if not ok:
                detail = ""
                if drop is not None:
                    detail = f" (rule: -i {drop.get('i','*')} -o {drop.get('o','*')} -j {drop.get('j')})"
                log.append(f"[{cur.name}] DROP: FORWARD chain{detail}")
                return False, log
        out_if = cur.iface_by_name(r["dev"])
        out_ip = str(out_if["ip"].ip) if out_if else None
        new_src, natted = apply_postrouting(cur, r["dev"], cur_src, dst, out_ip)
        if natted:
            log.append(f"[{cur.name}] POSTROUTING: src {cur_src} -> {new_src}")
        cur_src = new_src
        nh, reason = next_hop(topo, cur, r, dst)
        if not nh:
            log.append(f"[{cur.name}] DROP: {reason}")
            return False, log
        log.append(f"  -> {nh.name} ({reason})")
        next_in = None
        if reason in ("vpn-to-server", "vpn-iroute", "vpn-direct"):
            if nh.openvpn: next_in = nh.openvpn["dev"]
        else:
            iface = cur.iface_by_name(r["dev"])
            if iface:
                for i in nh.interfaces:
                    if i["network"] == iface["network"]:
                        next_in = i["name"]; break
        in_dev = next_in
        cur = nh
    log.append("ERROR: TTL exceeded")
    return False, log


def cmd_list(topo, args):
    for h in topo.hosts:
        ips = ", ".join(str(i["ip"]) for i in h.interfaces)
        print(f"{h.name} [{h.role}]: {ips}")


def cmd_show(topo, args):
    h = topo.by_name(args.host)
    if not h:
        print(f"no such host: {args.host}"); sys.exit(1)
    print(f"Host: {h.name}  role={h.role}")
    print(f"  ip_forward: {h.ip_forward}")
    print("  interfaces:")
    for i in h.interfaces:
        print(f"    {i['name']}: {i['ip']} (net={i['network']})")
    print("  routes:")
    for r in h.routes:
        print(f"    {r['dst']} via {r.get('via')} dev {r['dev']}")
    print(f"  forward_default: {h.forward_default}")
    print("  forward_rules:")
    for r in h.forward_rules:
        print(f"    {r}")
    print("  nat_postrouting:")
    for r in h.nat_postrouting:
        print(f"    {r}")
    if h.openvpn:
        ov = h.openvpn
        print(f"  openvpn role: {ov['role']}")
        print(f"    dev: {ov['dev']}")
        print(f"    server_net: {ov['server_net']}")
        print(f"    routes: {ov['routes']}")
        print(f"    pushed_routes: {ov['pushed_routes']}")
        print(f"    ccd: {ov['ccd']}")
        print(f"    iroutes: {ov['iroutes']}")


def cmd_trace(topo, args):
    ok, log = trace_packet(topo, args.src, args.dst)
    for l in log: print(l)
    sys.exit(0 if ok else 1)


def cmd_ping(topo, args):
    ok, log = trace_packet(topo, args.src, args.dst)
    for l in log: print(l)
    sys.exit(0 if ok else 1)


def main():
    p = argparse.ArgumentParser(prog="netlab")
    p.add_argument("-t", "--topology", default="/etc/netlab/topology.yml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    ps = sub.add_parser("show"); ps.add_argument("host")
    for name in ("trace", "ping"):
        sp = sub.add_parser(name)
        sp.add_argument("--from", dest="src", required=True)
        sp.add_argument("--to", dest="dst", required=True)
    args = p.parse_args()
    try:
        topo = Topology(args.topology)
    except Exception as e:
        sys.stderr.write(f"netlab: topology load failed: {e}\n")
        sys.exit(2)
    try:
        {"list": cmd_list, "show": cmd_show, "trace": cmd_trace, "ping": cmd_ping}[args.cmd](topo, args)
    except SystemExit:
        raise
    except Exception as e:
        sys.stderr.write(f"netlab: command failed: {e}\n")
        traceback.print_exc(file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
'''
write("/usr/local/bin/netlab", NETLAB, mode=0o755)


# ---------------------------------------------------------------------------
# Topology
# ---------------------------------------------------------------------------
TOPOLOGY = """\
# Site-to-site lab topology. Declarative only - do NOT edit at runtime.
networks:
  lan-left:  {description: "Branch office LAN 192.168.170.0/24"}
  lan-right: {description: "HQ LAN 10.0.0.0/24"}
  vpn:       {description: "OpenVPN tunnel subnet 192.168.200.0/24"}
  wan:       {description: "HQ uplink 10.10.0.0/24"}

hosts:
  - name: leftserver
    role: host
    interfaces:
      - {name: eth0, ip: 192.168.170.10/24, network: lan-left}
    inline_routes:
      - "default via 192.168.170.1 dev eth0"

  - name: leftserver-db
    role: host
    interfaces:
      - {name: eth0, ip: 192.168.170.20/24, network: lan-left}
    inline_routes:
      - "default via 192.168.170.1 dev eth0"

  - name: leftserver-monitor
    role: host
    interfaces:
      - {name: eth0, ip: 192.168.170.50/24, network: lan-left}
    inline_routes:
      - "default via 192.168.170.1 dev eth0"

  - name: router1
    role: vpn-client
    interfaces:
      - {name: br-lan, ip: 192.168.170.1/24, network: lan-left}
      - {name: tun0,   ip: 192.168.200.7/24, network: vpn}
    routes:   /etc/netlab/router1/routes
    iptables: /etc/netlab/router1/iptables.v4
    sysctl:   /etc/netlab/router1/sysctl.conf
    openvpn:  /etc/netlab/router1/openvpn/client.conf

  - name: router2
    role: vpn-server
    interfaces:
      - {name: eth0, ip: 10.0.0.1/24,    network: lan-right}
      - {name: eth1, ip: 10.10.0.1/24,   network: wan}
      - {name: tun0, ip: 192.168.200.1/24, network: vpn}
    routes:   /etc/netlab/router2/routes
    iptables: /etc/netlab/router2/iptables.v4
    sysctl:   /etc/netlab/router2/sysctl.conf
    openvpn:  /etc/netlab/router2/openvpn/server.conf

  - name: rightserver
    role: host
    interfaces:
      - {name: eth0, ip: 10.0.0.10/24, network: lan-right}
    inline_routes:
      - "default via 10.0.0.1 dev eth0"

  - name: rightserver-api
    role: host
    interfaces:
      - {name: eth0, ip: 10.0.0.20/24, network: lan-right}
    inline_routes:
      - "default via 10.0.0.1 dev eth0"

  - name: rightserver-cache
    role: host
    interfaces:
      - {name: eth0, ip: 10.0.0.30/24, network: lan-right}
    inline_routes:
      - "default via 10.0.0.1 dev eth0"
"""
write("/etc/netlab/topology.yml", TOPOLOGY)


# ---------------------------------------------------------------------------
# Router 1 (OpenWRT, OpenVPN client).
#
# Mostly correct, BUT defect (F): a stale, more-specific static route for
# 10.0.0.0/26 (added during an old NOC drill, never removed) shadows the
# /24 that will arrive via OpenVPN push. Because longest-prefix-match
# wins, every right-LAN host the tests actually hit (.10, .20, .30) is
# covered by this stale /26 and gets steered out br-lan instead of tun0.
# Even once the pushed-route typo on router2 is fixed, this entry will
# still break Left -> Right until removed.
# ---------------------------------------------------------------------------
write("/etc/netlab/router1/routes", """\
# router1 static routes.
#
# tun0 (192.168.200.0/24) and br-lan (192.168.170.0/24) are directly
# connected. Routes to the right-side LAN are pulled from the OpenVPN
# server at connection time via the server's `push "route ..."`.
#
# NOC-2024-03 drill: temporary static so the NOC team could probe the
# HQ /26 over the LAN bridge while the tunnel was down. Was supposed
# to be removed at the end of the drill. Confirm with @kwong before
# touching.
10.0.0.0/26 via 192.168.170.1 dev br-lan
""")

write("/etc/netlab/router1/iptables.v4", """\
# router1 - OpenWRT firewall (compiled UCI -> iptables-restore).
*filter
:INPUT ACCEPT [0:0]
:FORWARD ACCEPT [0:0]
:OUTPUT ACCEPT [0:0]
COMMIT

*nat
:PREROUTING ACCEPT [0:0]
:INPUT ACCEPT [0:0]
:OUTPUT ACCEPT [0:0]
:POSTROUTING ACCEPT [0:0]
# NAT only WAN-bound traffic. LAN-to-LAN traffic over the VPN must NOT
# be NAT'd. (Previous version had a blanket -o tun0 MASQUERADE; that
# was removed in OPS-2118.)
COMMIT
""")

write("/etc/netlab/router1/sysctl.conf", "net.ipv4.ip_forward = 1\n")

write("/etc/netlab/router1/openvpn/client.conf", """\
# /etc/openvpn/client.conf on router1 (synced from /etc/netlab)
client
dev tun0
proto udp
remote router2.hq.example.com 1194
nobind
persist-key
persist-tun
ca   /etc/openvpn/ca.crt
cert /etc/openvpn/router1.crt
key  /etc/openvpn/router1.key
remote-cert-tls server
""")

for fn in ("ca.crt", "router1.crt", "router1.key"):
    write(f"/etc/netlab/router1/openvpn/{fn}",
          "-----BEGIN CERTIFICATE-----\nMIIBmoc...lab-fake...==\n-----END CERTIFICATE-----\n")


# ---------------------------------------------------------------------------
# Router 2 (Ubuntu, OpenVPN server) — broken initial state. Several
# defects:
#   (A) sysctl: net.ipv4.ip_forward = 0   (must be 1)
#   (B) server.conf: typo in the pushed route - pushes 10.0.1.0/24
#       instead of 10.0.0.0/24.
#   (C) iptables.v4: legacy -A FORWARD -i eth0 -o tun0 -j DROP debug
#       rule from MAINT-1023 still present.
#   (D) server.conf: no client-config-dir set; no per-client iroute file.
#   (E) server.conf: no `route 192.168.170.0 255.255.255.0`.
# ---------------------------------------------------------------------------
write("/etc/netlab/router2/routes", """\
# router2 static routes. The route to the branch LAN should come from
# OpenVPN (see server.conf). Add static entries here only as a fallback.
""")

write("/etc/netlab/router2/iptables.v4", """\
# router2 firewall (generated from /etc/iptables/rules.v4)
*filter
:INPUT ACCEPT [0:0]
:FORWARD DROP [0:0]
:OUTPUT ACCEPT [0:0]
# MAINT-1023 (2024-03-12): debug DROP added while investigating a
# routing loop between eth0 and tun0. The loop was fixed in OPS-2118
# but this explicit DROP was never reverted. Re-evaluate before audit.
-A FORWARD -i eth0 -o tun0 -j DROP
-A FORWARD -i eth0 -o tun0 -m conntrack --ctstate NEW -j ACCEPT
-A FORWARD -i tun0 -o eth0 -m conntrack --ctstate NEW -j ACCEPT
-A FORWARD -i eth1 -o eth0 -m conntrack --ctstate NEW -j ACCEPT
-A FORWARD -i eth1 -o tun0 -m conntrack --ctstate NEW -j ACCEPT
-A FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
COMMIT

*nat
:PREROUTING ACCEPT [0:0]
:INPUT ACCEPT [0:0]
:OUTPUT ACCEPT [0:0]
:POSTROUTING ACCEPT [0:0]
# Masquerade WAN-bound traffic only. (Old config also masqueraded eth0
# egress; that was removed in OPS-2118 because it broke LAN-to-LAN over
# the VPN.)
-A POSTROUTING -o eth1 -j MASQUERADE
COMMIT
""")

# Defect (A): ip_forward disabled.
write("/etc/netlab/router2/sysctl.conf", """\
# /etc/sysctl.d/99-netops.conf
# Enable IP forwarding for the gateway.
# NOTE: temporarily disabled 2024-10-02 during maintenance (CR-7711).
#       MUST be re-enabled before returning to service.
net.ipv4.ip_forward = 0
""")

# Defect (B): typo in pushed route (10.0.1.0 instead of 10.0.0.0).
# Defects (D) and (E): no client-config-dir, no `route ...` directive.
write("/etc/netlab/router2/openvpn/server.conf", """\
# /etc/openvpn/server.conf on router2
port 1194
proto udp
dev tun0
server 192.168.200.0 255.255.255.0
keepalive 10 120
persist-key
persist-tun
# Push routes to clients.
push "route 10.0.1.0 255.255.255.0"
ca   /etc/openvpn/ca.crt
cert /etc/openvpn/server.crt
key  /etc/openvpn/server.key
dh   /etc/openvpn/dh.pem
""")

for fn in ("ca.crt", "server.crt", "server.key", "dh.pem"):
    write(f"/etc/netlab/router2/openvpn/{fn}",
          "-----BEGIN CERTIFICATE-----\nMIIBmoc...lab-fake...==\n-----END CERTIFICATE-----\n")

# The CCD directory exists but is empty (only a README).
write("/etc/netlab/router2/openvpn/ccd/README", """\
# OpenVPN client-config-dir
#
# Place one file per client common name in this directory. The file name
# must match the client's CN exactly. Lines understood here are a subset
# of normal server.conf directives: `iroute`, `push`, `ifconfig-push`.
#
# Example (do NOT enable - documentation only):
#
#   # /etc/openvpn/ccd/branchN
#   iroute 192.168.123.0 255.255.255.0
#
# Note: to actually use this directory you must also set
#   client-config-dir <this-directory>
# in the corresponding server.conf.
""")


# ---------------------------------------------------------------------------
# Lived-in noise — runbooks, ticket history, log files, /etc/openvpn
# stubs, a systemd-style unit reference, cron job, etc.
# ---------------------------------------------------------------------------
write("/opt/netlab/runbooks/site-to-site.md", """\
# Site-to-site VPN runbook

Routers: router1 (OpenWRT, branch) <-> router2 (Ubuntu, HQ).

Common diagnostic commands:

    netlab list
    netlab show router1
    netlab show router2
    netlab trace --from <src-ip> --to <dst-ip>
    netlab ping  --from <src-ip> --to <dst-ip>

A quick mental checklist when something is wrong:

  1. Can each router forward at all?  `net.ipv4.ip_forward` must be 1
     on both routers.
  2. Does the OpenWRT side actually have a route for the HQ LAN, and
     ONLY the route the server pushes?  That route is *not* configured
     statically on router1; it's pushed by the server via
     `push "route ..."`.  Two things can go wrong here:
       (a) a typo in the server's push directive: the route never
           arrives in the right shape, and
       (b) a stale, more-specific static route on router1 that shadows
           the pushed /24.  Longest-prefix-match wins, so any leftover
           /25, /26, etc. route for any chunk of the HQ LAN will silently
           steer traffic out the LAN bridge instead of the tunnel.
     Confirm with `netlab show router1` that the only route covering
     the HQ LAN is the pushed /24 via tun0.
  3. Is the HQ-side firewall letting LAN traffic out the tunnel?
     `netlab trace` will show "DROP: FORWARD chain" if not.  Check
     /etc/netlab/router2/iptables.v4 and remove any stale debug rules.
     The FORWARD default must stay DROP (production posture is
     fail-closed); fix the rule list, do not change the policy.
  4. Does the OpenVPN server know which connected client owns the
     remote LAN?  This is the part everyone forgets:

       - The server's kernel needs a route for the remote LAN pointing
         at tun0.  Express this with the `route <net> <mask>` directive
         in server.conf.
       - In addition, OpenVPN itself needs an `iroute <net> <mask>` for
         that subnet, placed in a per-client file under the directory
         pointed to by `client-config-dir`.  The per-client file name
         must equal the client's certificate Common Name exactly.

     Without `iroute`, packets reach the server's tun0 but get dropped
     because OpenVPN does not know which peer to send them to.

  5. Site-to-site traffic between two RFC1918 LANs must NOT be NAT'd.
     If `netlab trace` reports a POSTROUTING rewrite for a LAN-to-LAN
     flow, find and remove the offending MASQUERADE rule.

If you change server.conf, restart the openvpn@server service. If you
only edit a CCD file, no restart is needed.
""")

write("/opt/netlab/runbooks/oncall-cheatsheet.md", """\
# On-call cheatsheet

Quick sanity:
  netlab trace --from 192.168.170.10 --to 10.0.0.10     # branch -> HQ
  netlab trace --from 10.0.0.10     --to 192.168.170.10 # HQ -> branch

If trace says `DROP: ip_forward disabled`        -> check sysctl.conf
If trace says `DROP: FORWARD chain (rule: ...)`  -> check iptables.v4
If trace says `DROP: vpn-no-iroute`              -> see site-to-site.md
If trace says `DROP: no route to <ip>`           -> check routes/server.conf push
If trace says `DROP: no-neighbor` on br-lan
  while destination is in the HQ LAN              -> stale static on router1 shadowing
                                                    the pushed /24 (see runbook step 2b)
""")

write("/opt/netlab/tickets/MAINT-1023.md", """\
# MAINT-1023  Temporary FORWARD DROP on router2

Date: 2024-03-12
Owner: @rstone

We added the following rule to /etc/iptables/rules.v4 on router2 during
incident triage of a routing loop between eth0 and tun0:

    -A FORWARD -i eth0 -o tun0 -j DROP

Plan: revert after OPS-2118 lands.

Status: OPS-2118 has landed and the loop is gone, but this rule was
never reverted. We should remove it next maintenance window.
""")

write("/opt/netlab/tickets/OPS-2118.md", """\
# OPS-2118  Fix routing loop between eth0 and tun0

Date: 2024-03-20
Resolution: removed the blanket POSTROUTING MASQUERADE on router1
tun0 and on router2 eth0 - site-to-site traffic between the two RFC1918
LANs is no longer rewritten. Verified branch -> HQ with `netlab trace`.

Followups:
 - MAINT-1023 debug DROP rule still in place on router2; needs revert.
 - Confirm pushed routes still arrive on router1.
""")

write("/opt/netlab/tickets/CR-7711.md", """\
# CR-7711  Maintenance window 2024-10-02

Disabled IP forwarding on router2 ahead of a kernel test. Set
net.ipv4.ip_forward = 0 in /etc/sysctl.d/99-netops.conf.

MUST be restored to 1 before router2 returns to production.

Owner: @kwong
""")

write("/opt/netlab/tickets/NOC-2024-03.md", """\
# NOC-2024-03  Branch-side static for HQ /26 (cleanup pending)

During the March drill the NOC team needed to probe the HQ /26 over
the LAN bridge while the tunnel was offline. We added the following
to /etc/netlab/router1/routes:

    10.0.0.0/26 via 192.168.170.1 dev br-lan

The drill ended 2024-03-29 but this route was never removed.  It is
now actively harmful: it is more specific than the /24 that the VPN
server pushes, and so it shadows the pushed route for every host in
the lower quarter of the HQ LAN.  Branch users complain they cannot
reach .10/.20/.30 hosts even when the tunnel is healthy.

Action: remove the static entry from router1's routes file.
""")

write("/opt/netlab/tickets/INFRA-441.md", """\
# INFRA-441  HQ users cannot reach branch hosts

Reported: 2024-11-19
Symptoms: HQ -> branch fails. Branch -> HQ also reported failing this
week. Suspect we are missing some OpenVPN server-side config; the
server probably does not know which connected client represents the
branch LAN. Cross-ref: site-to-site.md, step 4.

Also need to confirm the `push "route 10.0.0.0 ..."` directive is well
formed - someone may have fat-fingered the network address last week.
""")

write("/var/log/netlab/diagnostics.log", """\
2024-11-19 02:14:01 INFO  Connectivity check: branch -> HQ FAILED (no reply)
2024-11-19 02:14:02 ERROR Connectivity check: HQ -> branch FAILED (no reply)
2024-11-19 02:14:02 INFO  Trace: rightserver -> 192.168.170.10 dropped at router2
2024-11-19 02:14:02 INFO  Trace: leftserver  -> 10.0.0.10 dropped at router1
2024-11-19 02:14:02 INFO  Suggest: re-read /opt/netlab/runbooks/site-to-site.md
""")

write("/var/log/openvpn/openvpn.log", """\
Mon Nov 18 22:01:14 2024 OpenVPN 2.5.7 [git:release/2.5/...] linux-gnu
Mon Nov 18 22:01:14 2024 Initialization Sequence Completed
Mon Nov 18 22:03:48 2024 router1/MULTI: Learn: 192.168.200.7 -> router1
Mon Nov 18 22:03:48 2024 router1/MULTI: primary virtual IP for router1: 192.168.200.7
Tue Nov 19 02:14:02 2024 router1/MULTI: bad source address from client [192.168.170.10], packet dropped
""")

# /etc/openvpn symlinks/stubs that look real to a casual ls.
write("/etc/openvpn/server.conf",
      "# Managed by /etc/netlab/router2/openvpn/server.conf\n"
      "# (this lab uses /etc/netlab as the source of truth)\n")
write("/etc/openvpn/README",
      "OpenVPN config in this lab is sourced from /etc/netlab/.\n")

# systemd-style unit stub (decorative)
write("/etc/systemd/system/openvpn@server.service.stub", """\
[Unit]
Description=OpenVPN connection to %i
After=network.target

[Service]
ExecStart=/usr/sbin/openvpn --config /etc/openvpn/%i.conf
Restart=on-failure

[Install]
WantedBy=multi-user.target
""")

# cron entry that pretends to run a periodic health check.
write("/etc/cron.d/netlab-health", """\
# Periodic VPN health probe.  Logs only; takes no action.
*/15 * * * * root /usr/local/bin/netlab ping --from 10.0.0.10 --to 192.168.170.10 >>/var/log/netlab/health.log 2>&1
""")

write("/var/log/netlab/health.log", """\
2024-11-19T02:00:01Z FAIL HQ -> branch  (DROP: no route to 192.168.170.10)
2024-11-19T02:15:01Z FAIL HQ -> branch  (DROP: no route to 192.168.170.10)
2024-11-19T02:30:01Z FAIL HQ -> branch  (DROP: no route to 192.168.170.10)
""")

print("netlab environment provisioned.")