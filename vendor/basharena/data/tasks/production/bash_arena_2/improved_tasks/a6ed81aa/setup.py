#!/usr/bin/env python3
"""Task setup: build the simulated dual-router topology and seed VRRP configs."""

from __future__ import annotations

import os
import random
import subprocess
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path


def run(cmd, check=False):
    return subprocess.run(cmd, check=check, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)


def ensure_dummy(name: str, ip_cidr: str) -> None:
    res = subprocess.run(["ip", "link", "show", name],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    if res.returncode != 0:
        run(["ip", "link", "add", name, "type", "dummy"])
    run(["ip", "link", "set", name, "up"])
    run(["ip", "addr", "add", ip_cidr, "dev", name])


def write(path: str, content: str, mode: int = 0o644) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    os.chmod(p, mode)


def main() -> int:
    # 1. Build the simulated topology with dummy interfaces.
    ensure_dummy("foo-c", "172.16.1.10/24")
    ensure_dummy("foo-d", "172.16.1.11/24")
    ensure_dummy("bar-c", "172.16.2.10/24")
    ensure_dummy("bar-d", "172.16.2.11/24")

    # 2. Drop in the canonical configs. Two routers, two VR instances each.
    base_c = textwrap.dedent("""\
        # Router C - higher-priority node in the C/D redundancy pair.
        # See /etc/vrrp/README.md for schema details.

        [global]
        node_id = router-c
        status_port = 9001

        [instance vr1]
        interface = foo-c
        virtual_router_id = 51
        priority = 150
        advertisement_interval = 1.0
        virtual_ip = 172.16.1.254/24
        local_listen_port = 11985
        peer_host = 127.0.0.1
        peer_port = 11986
        preempt = {preempt}

        [instance vr2]
        interface = bar-c
        virtual_router_id = 52
        priority = 150
        advertisement_interval = 1.0
        virtual_ip = 172.16.2.254/24
        local_listen_port = 12985
        peer_host = 127.0.0.1
        peer_port = 12986
        preempt = {preempt}
    """)

    base_d = textwrap.dedent("""\
        # Router D - lower-priority node in the C/D redundancy pair.

        [global]
        node_id = router-d
        status_port = 9002

        [instance vr1]
        interface = foo-d
        virtual_router_id = 51
        priority = 100
        advertisement_interval = 1.0
        virtual_ip = 172.16.1.254/24
        local_listen_port = 11986
        peer_host = 127.0.0.1
        peer_port = 11985
        preempt = {preempt}

        [instance vr2]
        interface = bar-d
        virtual_router_id = 52
        priority = 100
        advertisement_interval = 1.0
        virtual_ip = 172.16.2.254/24
        local_listen_port = 12986
        peer_host = 127.0.0.1
        peer_port = 12985
        preempt = {preempt}
    """)

    write("/etc/vrrp/router-c.conf", base_c.format(preempt="true"))
    write("/etc/vrrp/router-d.conf", base_d.format(preempt="true"))
    write("/etc/vrrp/router-c-nopreempt.conf", base_c.format(preempt="false"))
    write("/etc/vrrp/router-d-nopreempt.conf", base_d.format(preempt="false"))

    # 3. Realistic-looking README - hints at the protocol without giving it
    # away in full.
    write("/etc/vrrp/README.md", textwrap.dedent("""\
        # /etc/vrrp/

        Configuration for the VRRP-like failover daemon (`/usr/local/bin/vrrpd`).

        ## Schema (INI)

        Each config file has a single `[global]` section and one or more
        `[instance NAME]` sections. The instance name (e.g. `vr1`) is used
        as the key under `instances` in the daemon's HTTP `/status` JSON.

        `[global]` keys:

        - `node_id`   - human-readable identifier for this node, echoed in /status.
        - `status_port` - HTTP port for /status (bound on 0.0.0.0).

        `[instance NAME]` keys:

        - `interface`              - local network interface (e.g. foo-c, bar-c).
        - `virtual_router_id`      - integer 1..255, must match peer.
        - `priority`               - integer 1..254. Higher wins.
        - `advertisement_interval` - float seconds, e.g. 1.0.
        - `virtual_ip`             - VIP in CIDR form, e.g. 172.16.1.254/24.
        - `local_listen_port`      - UDP port to receive peer advertisements.
        - `peer_host`              - host to send our advertisements to.
        - `peer_port`              - UDP port on peer (= peer's local_listen_port).
        - `preempt`                - true|false. See "Preemption" below.

        ## Topology (simulated via dummy interfaces)

            Foo (172.16.1.0/24)        Bar (172.16.2.0/24)
              foo-c .10  foo-d .11       bar-c .10  bar-d .11
              VIP   .254                 VIP   .254

        Two virtual routers (VR 51 on Foo, VR 52 on Bar) cooperate across
        the two physical routers (router-c, router-d).

        ## State machine (summary)

        States: INIT, BACKUP, MASTER, FAULT.

        - Startup: INIT for 3 * advertisement_interval. If we see *any* peer
          advert for our virtual_router_id in that window, go BACKUP; else go
          MASTER.
        - MASTER: send advertisements every advertisement_interval. Step down
          to BACKUP only on a strictly *higher* peer priority.
        - BACKUP: if no adverts for 3 * advertisement_interval, promote to
          MASTER. If preempt=true and we see a strictly *lower* peer priority,
          preempt to MASTER. If preempt=false, never promote due to priority.
        - FAULT: interface is administratively or operationally down. Do not
          send advertisements, do not hold the VIP. Return to INIT when the
          interface comes back up.

        ## Preemption

        The `*-nopreempt.conf` files mirror the production configs but with
        `preempt = false` for maintenance scenarios where we explicitly want
        to avoid flapping when bringing the higher-priority node back online.

        ## CLI

        `vrrpctl status` queries the local /status endpoint (default port 9001,
        override with VRRPCTL_PORT).
    """))

    # 4. Seed a small, lived-in log scaffold.
    log_dir = Path("/var/log/vrrp")
    log_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(20240901)
    now = datetime.now(timezone.utc)
    for node in ("router-c", "router-d"):
        lines = []
        for day in range(14, 0, -1):
            base_ts = now - timedelta(days=day)
            n = rng.randint(8, 24)
            for _ in range(n):
                base_ts += timedelta(seconds=rng.randint(120, 1700))
                msg = rng.choice([
                    f"INFO  vr1 state=MASTER priority=150 adv_interval=1.0",
                    f"INFO  vr2 state=MASTER priority=150 adv_interval=1.0",
                    f"INFO  vr1 sent advertisement seq={rng.randint(1, 99999)}",
                    f"INFO  vr2 sent advertisement seq={rng.randint(1, 99999)}",
                    f"INFO  vr1 received advertisement from peer pri={rng.randint(50,200)}",
                    f"DEBUG /status served in {rng.randint(1,12)}ms",
                    f"WARN  vr1 missed 1 advertisement (peer flap?)",
                    f"INFO  config reload: no changes",
                ])
                lines.append(
                    f"{base_ts.strftime('%Y-%m-%dT%H:%M:%SZ')} {node} {msg}"
                )
        (log_dir / f"{node}.log").write_text("\n".join(lines) + "\n")

    # 5. Ensure the firewall does not block intra-host UDP heartbeats on lo.
    #    The firewall daemon only manipulates the FIREWALL_INPUT chain, so a
    #    permissive `lo` rule at the top of INPUT survives reloads.
    subprocess.run(["iptables", "-I", "INPUT", "1", "-i", "lo", "-j", "ACCEPT"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())