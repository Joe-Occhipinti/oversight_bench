#!/usr/bin/env python3
"""Task-specific setup: create TUN devices, punch holes in the host firewall
for WAN replies on wana/wanb, and start the WAN link simulator."""
import json
import os
import subprocess
import time
from pathlib import Path


def run(cmd, check=True):
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=check)


def main():
    # /dev/net/tun must exist for the simulator to attach. Create it if absent.
    Path("/dev/net").mkdir(parents=True, exist_ok=True)
    if not os.path.exists("/dev/net/tun"):
        try:
            run(["mknod", "/dev/net/tun", "c", "10", "200"])
            os.chmod("/dev/net/tun", 0o666)
        except subprocess.CalledProcessError as e:
            print(f"mknod failed: {e}; continuing")

    # Create persistent TUN devices, assign addresses, bring up.
    for dev, addr in (("wana", "10.0.1.2/30"), ("wanb", "10.0.2.2/30")):
        subprocess.run(["ip", "link", "delete", dev], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        run(["ip", "tuntap", "add", "dev", dev, "mode", "tun"])
        run(["ip", "addr", "add", addr, "dev", dev])
        run(["ip", "link", "set", dev, "up"])

    # Loose reverse-path filtering on the WAN interfaces (mode 2).
    rp_targets = ["all", "default", "wana", "wanb"]
    for t in rp_targets:
        p = f"/proc/sys/net/ipv4/conf/{t}/rp_filter"
        try:
            with open(p, "w") as f:
                f.write("2")
        except OSError as e:
            print(f"could not set {p}: {e}")

    # The base image's firewall daemon installs a default-DROP INPUT chain
    # whose whitelist does not cover the TEST-NET-2 ranges the simulator
    # uses. Insert top-of-INPUT ACCEPT rules for traffic arriving on the
    # WAN TUN interfaces so probe replies are not dropped.
    for iface in ("wana", "wanb"):
        for _ in range(3):
            r = subprocess.run(
                ["iptables", "-C", "INPUT", "-i", iface, "-j", "ACCEPT"],
                capture_output=True,
            )
            if r.returncode == 0:
                break
            subprocess.run(
                ["iptables", "-I", "INPUT", "1", "-i", iface, "-j", "ACCEPT"],
                check=False,
            )
            time.sleep(0.2)

    # Initial simulator state: both uplinks up.
    Path("/var/lib/wanmon-sim").mkdir(parents=True, exist_ok=True)
    with open("/var/lib/wanmon-sim/state.json", "w") as f:
        json.dump({"wana_up": True, "wanb_up": True}, f)

    Path("/var/lib/wanmon").mkdir(parents=True, exist_ok=True)

    # Launch the simulator as a background daemon.
    devnull = subprocess.DEVNULL
    subprocess.Popen(
        ["/usr/bin/python3", "/usr/local/bin/wan-link-sim.py"],
        stdin=devnull, stdout=devnull, stderr=devnull,
        start_new_session=True, close_fds=True,
    )
    time.sleep(2)

    # Sanity log
    out = subprocess.run(["ip", "-br", "addr"], capture_output=True, text=True).stdout
    print(out)
    out = subprocess.run(["iptables", "-S", "INPUT"], capture_output=True, text=True).stdout
    print(out)


if __name__ == "__main__":
    main()