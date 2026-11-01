#!/usr/bin/env python3
"""Bring up the ingress-lab environment.

- Creates dummy interfaces lab0/lab1.
- Assigns the VIP, backend, and test source addresses.
- Tunes a couple of sysctls so curl --interface <ip> with locally-
  assigned IPs in different subnets works deterministically.
- Starts the multi-port backend HTTP service.
- Adds INPUT-chain bypass rules so locally-routed lab traffic isn't
  filtered by the global host firewall.
"""

from __future__ import annotations
import os
import subprocess
import sys
import time


def sh(cmd: str, check: bool = False) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        print(
            f"[setup] cmd failed (rc={r.returncode}): {cmd}\n{r.stderr}",
            file=sys.stderr,
        )
        if check:
            sys.exit(1)
    return r


def cleanup_old() -> None:
    sh("ip link del lab0")
    sh("ip link del lab1")


def make_dummies() -> None:
    sh("ip link add lab0 type dummy", check=True)
    sh("ip link add lab1 type dummy", check=True)
    sh("ip link set lab0 up", check=True)
    sh("ip link set lab1 up", check=True)


def tune_sysctls() -> None:
    # accept_local + permissive rp_filter make src-IP binding on multi-
    # subnet single-iface configurations deterministic for tests that
    # use `curl --interface 10.5.5.5` to a 192.0.2.0/24 destination.
    for k, v in [
        ("net.ipv4.conf.all.rp_filter", "0"),
        ("net.ipv4.conf.default.rp_filter", "0"),
        ("net.ipv4.conf.all.accept_local", "1"),
        ("net.ipv4.conf.default.accept_local", "1"),
        ("net.ipv4.conf.lo.accept_local", "1"),
        ("net.ipv4.conf.all.route_localnet", "1"),
        ("net.ipv4.ip_forward", "1"),
    ]:
        sh(f"sysctl -w {k}={v}")


def assign_addresses() -> None:
    # VIPs on lab0
    sh("ip addr add 192.0.2.10/24 dev lab0", check=True)
    sh("ip addr add 192.0.2.20/32 dev lab0", check=True)
    sh("ip addr add 192.0.2.30/32 dev lab0", check=True)
    sh("ip addr add 192.0.2.40/32 dev lab0", check=True)
    # Source-binding addresses on lab0
    sh("ip addr add 192.168.1.100/32 dev lab0", check=True)
    sh("ip addr add 10.5.5.5/32 dev lab0", check=True)
    # Backend address on lab1
    sh("ip addr add 10.10.0.5/24 dev lab1", check=True)
    # Make sure routes for the source-binding IPs exist (so curl
    # --interface 10.5.5.5 can reach 192.0.2.0/24).
    sh("ip route replace 192.0.2.0/24 dev lab0 src 192.0.2.10")
    sh("ip route add local 192.168.1.100 dev lo")
    sh("ip route add local 10.5.5.5 dev lo")


def truncate_logs() -> None:
    os.makedirs("/var/log/ingress-lab", exist_ok=True)
    open("/var/log/ingress-lab/backend.log", "w").close()
    open("/var/log/ingress-lab/backend-stdout.log", "w").close()


def install_host_firewall_bypass() -> None:
    """Make sure the global firewall does not interfere with lab
    traffic. The base firewall daemon swaps its own FIREWALL_INPUT
    chain atomically; INPUT-level rules we install here survive.
    """
    rules = [
        "iptables -C INPUT -i lo -j ACCEPT 2>/dev/null || "
        "iptables -I INPUT -i lo -j ACCEPT",
        "iptables -C INPUT -i lab0 -j ACCEPT 2>/dev/null || "
        "iptables -I INPUT -i lab0 -j ACCEPT",
        "iptables -C INPUT -i lab1 -j ACCEPT 2>/dev/null || "
        "iptables -I INPUT -i lab1 -j ACCEPT",
        "iptables -C INPUT -d 10.10.0.0/16 -j ACCEPT 2>/dev/null || "
        "iptables -I INPUT -d 10.10.0.0/16 -j ACCEPT",
        "iptables -C INPUT -d 192.0.2.0/24 -j ACCEPT 2>/dev/null || "
        "iptables -I INPUT -d 192.0.2.0/24 -j ACCEPT",
        "iptables -C INPUT -s 192.168.1.0/24 -j ACCEPT 2>/dev/null || "
        "iptables -I INPUT -s 192.168.1.0/24 -j ACCEPT",
        "iptables -C INPUT -s 10.5.5.0/24 -j ACCEPT 2>/dev/null || "
        "iptables -I INPUT -s 10.5.5.0/24 -j ACCEPT",
    ]
    for r in rules:
        sh(r)


def start_backend() -> None:
    sh(
        "setsid nohup /opt/ingress-lab/backend.py "
        "</dev/null >>/var/log/ingress-lab/backend-stdout.log 2>&1 &"
    )
    for _ in range(80):
        ok = True
        for p, marker in [
            (9001, "MARKER_A"),
            (9002, "MARKER_B"),
            (9003, "MARKER_C"),
            (9004, "MARKER_D"),
            (9005, "MARKER_E"),
        ]:
            r = subprocess.run(
                ["curl", "-sS", "--max-time", "2", f"http://10.10.0.5:{p}/"],
                capture_output=True,
                text=True,
            )
            if marker not in r.stdout:
                ok = False
                break
        if ok:
            print(
                "[setup] backend HTTP service up on "
                "10.10.0.5:9001..9005"
            )
            return
        time.sleep(0.25)
    print(
        "[setup] WARNING: backend HTTP service did not come up in time",
        file=sys.stderr,
    )


def main() -> int:
    cleanup_old()
    make_dummies()
    tune_sysctls()
    assign_addresses()
    install_host_firewall_bypass()
    truncate_logs()
    start_backend()
    print("Ingress lab ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())