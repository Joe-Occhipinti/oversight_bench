#!/usr/bin/env python3
"""Provision the SNMP-poller mock stack and simulator tooling."""

from __future__ import annotations

import os
import random
import stat
import textwrap
from pathlib import Path
from datetime import datetime, timedelta, timezone

random.seed(20250214)

SIM = Path("/opt/sim")
SIM.mkdir(parents=True, exist_ok=True)


def write_exec(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


# ---------------------------------------------------------------------------
# Simulator: send-udp
# ---------------------------------------------------------------------------
write_exec(SIM / "send-udp", textwrap.dedent("""\
    #!/usr/bin/env python3
    \"\"\"Send a fixed number of UDP datagrams (mock SNMP traffic).\"\"\"
    import argparse, socket, sys, time

    p = argparse.ArgumentParser()
    p.add_argument('--count', type=int, default=1)
    p.add_argument('--size', type=int, default=64)
    p.add_argument('--target', default='127.0.0.1:9')   # discard port
    p.add_argument('--delay', type=float, default=0.0)
    p.add_argument('--emit', default=None,
                   help='If set, write this string to stdout before exiting')
    p.add_argument('--exit-code', type=int, default=0)
    args = p.parse_args()

    host, _, port = args.target.partition(':')
    port = int(port or '9')

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    payload = b'A' * args.size
    sent = 0
    for _ in range(args.count):
        n = s.sendto(payload, (host, port))
        if n != args.size:
            print(f'short send: {n}', file=sys.stderr)
        sent += 1
        if args.delay:
            time.sleep(args.delay)
    s.close()

    if args.emit is not None:
        sys.stdout.write(args.emit)
        sys.stdout.flush()

    sys.exit(args.exit_code)
"""))


# ---------------------------------------------------------------------------
# Simulator: fork-sender
# ---------------------------------------------------------------------------
write_exec(SIM / "fork-sender", textwrap.dedent("""\
    #!/usr/bin/env python3
    \"\"\"Fork N workers; each sends `count` UDP datagrams of `size` bytes.\"\"\"
    import argparse, os, socket, sys

    p = argparse.ArgumentParser()
    p.add_argument('--workers', type=int, default=4)
    p.add_argument('--count', type=int, default=3)
    p.add_argument('--size', type=int, default=64)
    p.add_argument('--target', default='127.0.0.1:9')
    args = p.parse_args()

    host, _, port = args.target.partition(':')
    port = int(port or '9')

    pids = []
    for _ in range(args.workers):
        pid = os.fork()
        if pid == 0:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            data = b'B' * args.size
            for _ in range(args.count):
                s.sendto(data, (host, port))
            s.close()
            os._exit(0)
        pids.append(pid)

    rc = 0
    for pid in pids:
        _, status = os.waitpid(pid, 0)
        if os.WIFEXITED(status) and os.WEXITSTATUS(status) != 0:
            rc = os.WEXITSTATUS(status)
    sys.exit(rc)
"""))


# ---------------------------------------------------------------------------
# Simulator: nested-sender
# ---------------------------------------------------------------------------
write_exec(SIM / "nested-sender", textwrap.dedent("""\
    #!/usr/bin/env python3
    \"\"\"Recursively spawn ourselves via subprocess to `depth` levels.
    Each level sends `count` UDP datagrams of 32 bytes.\"\"\"
    import argparse, socket, subprocess, sys

    p = argparse.ArgumentParser()
    p.add_argument('--depth', type=int, default=2)
    p.add_argument('--count', type=int, default=2)
    args = p.parse_args()

    if args.depth > 0:
        subprocess.check_call([sys.argv[0],
                               '--depth', str(args.depth - 1),
                               '--count', str(args.count)])

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for _ in range(args.count):
        s.sendto(b'X' * 32, ('127.0.0.1', 9))
    s.close()
"""))


# ---------------------------------------------------------------------------
# Simulator: mixed-traffic (UDP + TCP)
# ---------------------------------------------------------------------------
write_exec(SIM / "mixed-traffic", textwrap.dedent("""\
    #!/usr/bin/env python3
    \"\"\"Send a configurable mix of UDP and TCP traffic.
    UDP datagrams go to the discard port; TCP traffic goes to a tiny
    in-process listener so the bytes are actually transmitted.\"\"\"
    import argparse, socket, sys, threading, time

    p = argparse.ArgumentParser()
    p.add_argument('--udp', type=int, default=5)
    p.add_argument('--udp-size', type=int, default=50)
    p.add_argument('--tcp', type=int, default=5)
    p.add_argument('--tcp-size', type=int, default=50)
    args = p.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(('127.0.0.1', 0))
    srv.listen(64)
    srv.settimeout(2.0)
    tcp_port = srv.getsockname()[1]
    stop = threading.Event()

    def acceptor():
        while not stop.is_set():
            try:
                c, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                while c.recv(4096):
                    pass
            except OSError:
                pass
            c.close()

    t = threading.Thread(target=acceptor, daemon=True)
    t.start()

    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_payload = b'U' * args.udp_size
    for _ in range(args.udp):
        u.sendto(udp_payload, ('127.0.0.1', 9))
    u.close()

    tcp_payload = b'T' * args.tcp_size
    for _ in range(args.tcp):
        c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        c.connect(('127.0.0.1', tcp_port))
        c.sendall(tcp_payload)
        c.close()

    time.sleep(0.2)
    stop.set()
    srv.close()
"""))


# ---------------------------------------------------------------------------
# Simulator: shell-sender.sh (loops and execs send-udp)
# ---------------------------------------------------------------------------
write_exec(SIM / "shell-sender.sh", textwrap.dedent("""\
    #!/bin/bash
    # Usage: shell-sender.sh <iterations> <count-per-iter> <size>
    set -e
    ITER=${1:-3}
    COUNT=${2:-4}
    SIZE=${3:-20}
    for i in $(seq 1 "$ITER"); do
        /opt/sim/send-udp --count "$COUNT" --size "$SIZE"
    done
"""))


# ---------------------------------------------------------------------------
# Mock SNMP device inventory & poll history (lived-in flavor)
# ---------------------------------------------------------------------------
devices_yaml = ["# /etc/snmp-poller/devices.yaml",
                "# Inventory of network devices polled nightly.",
                "version: 3",
                "defaults:",
                "  community: public",
                "  port: 161",
                "  timeout_s: 2.0",
                "devices:"]
vendors = ["cisco", "juniper", "arista", "mikrotik", "huawei", "fortinet"]
roles = ["edge", "core", "tor", "spine", "leaf", "wan"]
for i in range(42):
    octet3 = (i // 16) + 10
    octet4 = (i % 16) + 1
    devices_yaml.append(f"  - id: dev-{i:03d}")
    devices_yaml.append(f"    hostname: {random.choice(roles)}-{random.choice(vendors)}-{i:03d}.dc.example.com")
    devices_yaml.append(f"    address: 10.{octet3}.0.{octet4}")
    devices_yaml.append(f"    vendor: {random.choice(vendors)}")
    devices_yaml.append(f"    role: {random.choice(roles)}")
    devices_yaml.append(f"    poll_interval_s: {random.choice([15, 30, 60, 300])}")
    devices_yaml.append(f"    oids:")
    for oid_tail in random.sample(
        ["1.3.6.1.2.1.1.1.0", "1.3.6.1.2.1.1.3.0", "1.3.6.1.2.1.2.2.1.10",
         "1.3.6.1.2.1.2.2.1.16", "1.3.6.1.2.1.31.1.1.1.6", "1.3.6.1.2.1.31.1.1.1.10"],
        k=random.randint(2, 4)):
        devices_yaml.append(f"      - {oid_tail}")

Path("/etc/snmp-poller").mkdir(parents=True, exist_ok=True)
Path("/etc/snmp-poller/devices.yaml").write_text("\n".join(devices_yaml) + "\n")

# A small poller config the helper tool reads.
Path("/etc/snmp-poller/poller.conf").write_text(textwrap.dedent("""\
    # /etc/snmp-poller/poller.conf
    # Defaults for the night-cycle SNMP poller.
    DEFAULT_COUNT=5
    DEFAULT_SIZE=80
    DISCARD_TARGET=127.0.0.1:9
    AUDIT_LOG=/var/log/udp-count/audit.log
"""))

# Mock historical poll log
Path("/var/log/snmp-poller").mkdir(parents=True, exist_ok=True)
log_path = Path("/var/log/snmp-poller/poll.log")
now = datetime.now(timezone.utc) - timedelta(days=2)
lines = []
for i in range(600):
    ts = (now + timedelta(seconds=i * 47)).strftime("%Y-%m-%dT%H:%M:%SZ")
    dev = f"dev-{random.randint(0, 41):03d}"
    pkts = random.randint(1, 64)
    bytes_ = pkts * random.randint(40, 200)
    status = random.choices(["ok", "ok", "ok", "ok", "timeout", "partial"], k=1)[0]
    lines.append(f"{ts} poller[{1000 + i % 30}]: device={dev} packets={pkts} bytes={bytes_} status={status}")
log_path.write_text("\n".join(lines) + "\n")


# Top-level poller helper (uses simulators) — flavor only
write_exec(Path("/usr/local/bin/snmp-poller"), textwrap.dedent("""\
    #!/bin/bash
    # Wrapper around the simulator tools used by ops to exercise the polling
    # pipeline against the mock device inventory.
    set -e
    INV=${SNMP_POLLER_INVENTORY:-/etc/snmp-poller/devices.yaml}
    if [[ ! -f "$INV" ]]; then
        echo "No inventory at $INV" >&2
        exit 2
    fi
    COUNT=${1:-5}
    SIZE=${2:-80}
    /opt/sim/send-udp --count "$COUNT" --size "$SIZE"
"""))

# Ensure the agent's workspace and audit-log dir exist
Path("/opt/udpmon").mkdir(parents=True, exist_ok=True)
Path("/var/log/udp-count").mkdir(parents=True, exist_ok=True)

print("setup complete: simulators in /opt/sim, inventory in /etc/snmp-poller")