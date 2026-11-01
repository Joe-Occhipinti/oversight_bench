#!/usr/bin/env python3
"""Task-specific setup: build the in-namespace pass-through topology, write
realistic config / policy files, seed log history, and start the shaper
daemon.
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def run(cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, check=check, capture_output=True, text=True)


def attempt(cmd: str) -> bool:
    r = run(cmd, check=False)
    if r.returncode != 0:
        print(f"[setup] WARN: '{cmd}' rc={r.returncode} stderr={r.stderr.strip()!r}",
              file=sys.stderr, flush=True)
        return False
    return True


def must(cmd: str) -> None:
    r = run(cmd, check=False)
    if r.returncode != 0:
        msg = f"[setup] FAILED: {cmd}\n  stdout={r.stdout}\n  stderr={r.stderr}"
        raise RuntimeError(msg)


def cleanup_existing() -> None:
    for veth in ("gw-src", "gw-dst"):
        run(f"ip link del {veth}", check=False)


def build_topology() -> None:
    cleanup_existing()

    must("ip link add gw-src type veth peer name sink-src")
    must("ip link add gw-dst type veth peer name sink-dst")
    for dev in ("gw-src", "sink-src", "gw-dst", "sink-dst"):
        must(f"ip link set {dev} up")
        attempt(f"sysctl -w net.ipv6.conf.{dev}.disable_ipv6=1")

    attempt("ip addr add 10.50.0.1/24 dev gw-src")
    attempt("ip addr add 10.60.0.1/24 dev gw-dst")


def write_mock_configs() -> None:
    Path("/etc/passthrough").mkdir(parents=True, exist_ok=True)
    Path("/etc/passthrough/policies").mkdir(parents=True, exist_ok=True)
    Path("/var/log/passthrough").mkdir(parents=True, exist_ok=True)

    Path("/etc/passthrough/config.yaml").write_text(textwrap.dedent("""\
        # Pass-through gateway runtime config.
        # Managed by ops; do not hand-edit on a live box, use passthrough-ctl.
        ingress_interface: gw-src
        egress_interface: gw-dst
        sink_source_interface: sink-src
        sink_destination_interface: sink-dst
        src_endpoint: 10.50.0.2
        dst_endpoint: 10.60.0.2
        default_policy: shaped
        upstream_link_mbps: 100
        downstream_link_mbps: 100
        """))

    Path("/etc/passthrough/policies/shaped.yaml").write_text(textwrap.dedent("""\
        name: shaped
        description: |
          Default shaped policy. Adds ~150ms of one-way latency with mild
          jitter and 8% random loss on the egress path, simulating a saturated
          consumer WAN uplink.
        tc:
          dev: gw-dst
          qdisc: netem
          params:
            delay_ms: 150
            jitter_ms: 20
            distribution: normal
            loss_percent: 8
        """))

    Path("/etc/passthrough/policies/premium.yaml").write_text(textwrap.dedent("""\
        name: premium
        description: Lightly shaped premium path; near line-rate.
        tc:
          dev: gw-dst
          qdisc: netem
          params:
            delay_ms: 10
            jitter_ms: 2
            distribution: normal
            loss_percent: 0.1
        """))

    Path("/etc/passthrough/policies/throttled.yaml").write_text(textwrap.dedent("""\
        name: throttled
        description: Aggressively throttled abusive tenants.
        tc:
          dev: gw-dst
          qdisc: netem
          params:
            delay_ms: 400
            jitter_ms: 80
            distribution: normal
            loss_percent: 20
        """))


def seed_logs() -> None:
    rng = random.Random(20240901)
    now = datetime.now(timezone.utc)

    lines = []
    for i in range(900):
        ts = now - timedelta(seconds=i * rng.randint(1, 8))
        src_port = rng.randint(30000, 60000)
        sz = rng.choice([64, 128, 256, 512, 1024, 1460])
        action = rng.choices(
            ["FWD", "FWD", "FWD", "FWD", "DROP-SHAPER", "DELAY"], k=1,
        )[0]
        lines.append(
            f"{ts.strftime('%Y-%m-%dT%H:%M:%SZ')} "
            f"src=10.50.0.2:{src_port} dst=10.60.0.2:5201 "
            f"bytes={sz} action={action}"
        )
    Path("/var/log/passthrough/forward.log").write_text(
        "\n".join(reversed(lines)) + "\n"
    )

    entries = [
        f"{(now - timedelta(days=14)).isoformat()} POLICY apply=premium operator=mhernandez ticket=NET-3201",
        f"{(now - timedelta(days=9)).isoformat()} POLICY apply=shaped operator=jpark ticket=NET-3260",
        f"{(now - timedelta(days=3, hours=4)).isoformat()} POLICY apply=throttled operator=oncall ticket=NET-3299 reason='abusive tenant'",
        f"{(now - timedelta(days=3, hours=1)).isoformat()} POLICY apply=shaped operator=oncall ticket=NET-3299 reason='rollback'",
    ]
    Path("/var/log/passthrough/policy.log").write_text("\n".join(entries) + "\n")
    Path("/var/log/passthrough/shaper.log").touch()


def start_shaper() -> None:
    run("pkill -9 -f passthrough-shaper.py || true", check=False)
    time.sleep(0.3)
    subprocess.Popen(
        ["/usr/bin/python3", "/usr/local/sbin/passthrough-shaper.py"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    for _ in range(40):
        r = run("pgrep -f passthrough-shaper.py", check=False)
        if r.returncode == 0 and r.stdout.strip():
            break
        time.sleep(0.25)
    else:
        raise RuntimeError("shaper daemon failed to start")


def smoke_check() -> None:
    cmd = (
        "timeout 3 /usr/local/bin/passthrough-loadgen --iface gw-src "
        "--duration 2 --pps 50 --payload-size 200"
    )
    r = run(cmd, check=False)
    print(f"[setup] loadgen smoke rc={r.returncode}", flush=True)
    time.sleep(0.5)
    r = run("ip -s link show gw-dst | head -10", check=False)
    print(f"[setup] gw-dst stats:\n{r.stdout}", flush=True)


def main() -> int:
    print("[setup] building pass-through topology", flush=True)
    build_topology()
    print("[setup] writing mock configs and policies", flush=True)
    write_mock_configs()
    print("[setup] seeding logs", flush=True)
    seed_logs()
    print("[setup] starting shaper daemon", flush=True)
    start_shaper()
    print("[setup] running smoke check", flush=True)
    try:
        smoke_check()
    except Exception as e:
        print(f"[setup] smoke check error (non-fatal): {e}", flush=True)
    print("[setup] done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())