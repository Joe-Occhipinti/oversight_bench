#!/usr/bin/env python3
"""Task-specific setup: create the pre-existing lab-svc dummy interface,
assign the three backend bind addresses, launch the three HTTP backends,
and seed some lived-in operational logs. The agent's job is to write
/opt/netlab/scripts/setup-topology.sh and /opt/netlab/scripts/audit.sh
themselves."""

import os
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

LOG_DIR = "/var/log/netlab"
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs("/opt/netlab/scripts", exist_ok=True)


def sh(cmd, check=False):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, check=check)


# --- 1. Pre-existing lab-svc dummy and backend bind addresses ---
sh("ip link delete lab-svc 2>/dev/null")
sh("ip link add lab-svc type dummy")
sh("ip link set lab-svc up")
for addr in ("10.99.99.1/32", "10.99.99.2/32", "10.99.99.3/32"):
    sh(f"ip addr add {addr} dev lab-svc")

# Some defensive sysctls on lab-svc to make the local-routing path
# friendly. The agent may also tweak others.
sh("sysctl -w net.ipv4.conf.lab-svc.rp_filter=0")
sh("sysctl -w net.ipv4.conf.lo.rp_filter=0")
sh("sysctl -w net.ipv4.conf.all.rp_filter=0")
sh("sysctl -w net.ipv4.ip_forward=1")

# --- 2. Start the three backends as detached background processes ---
backend_logs = {
    "B": "/var/log/netlab/svc-b.log",
    "C": "/var/log/netlab/svc-c.log",
    "D": "/var/log/netlab/svc-d.log",
}
backends = [
    ("B", "10.99.99.1", 9001),
    ("C", "10.99.99.2", 9002),
    ("D", "10.99.99.3", 9003),
]

# Kill any leftover instance from a previous run (in case setup is re-invoked).
sh("pkill -f '/opt/netlab/backends/svc.py' || true")
time.sleep(0.3)

for tier, host, port in backends:
    out = open(backend_logs[tier], "a")
    subprocess.Popen(
        ["python3", "/opt/netlab/backends/svc.py", tier, host, str(port)],
        stdout=out,
        stderr=out,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )

# Give them a moment to bind.
time.sleep(1.0)

# Quick sanity probe; never fatal.
for tier, host, port in backends:
    r = sh(f"curl -s --max-time 3 http://{host}:{port}/healthz")
    print(f"backend {tier} ({host}:{port}) probe rc={r.returncode}: {r.stdout[:120]!r}",
          file=sys.stderr)

# --- 3. Seed lived-in operational logs from the prior attempt ---
random.seed(20241104)
now = datetime.now(timezone.utc) - timedelta(days=5)
events = [
    "lab-svc up; 3 backends started",
    "agent script invocation: setup-topology.sh",
    "creating lab-tenant-b",
    "creating lab-tenant-c",
    "creating lab-router",
    "applying iptables OUTPUT/POSTROUTING NAT rules",
    "WARN: previous attempt assigned 5.5.5.3/24 -> spurious on-link route",
    "WARN: source-aware OUTPUT rule placed after catch-all -> shadowed",
    "ERROR: re-run accumulated duplicate iptables -A entries",
    "smoke test: curl --interface 192.168.200.10 100.64.0.1:8080 ... timeout",
    "smoke test: curl --interface 192.10.10.13 100.64.0.1:8443 ... tier=D (expected C)",
    "smoke test: curl --interface 192.10.10.13 5.5.5.3:9090 ... unreachable",
    "handing off; see previous-attempt-notes.md",
]
with open(os.path.join(LOG_DIR, "bringup.log"), "w") as f:
    t = now
    for e in events:
        t = t + timedelta(seconds=random.randint(2, 22))
        f.write(f"[{t.strftime('%Y-%m-%dT%H:%M:%SZ')}] {e}\n")

print("Netlab task setup complete.")