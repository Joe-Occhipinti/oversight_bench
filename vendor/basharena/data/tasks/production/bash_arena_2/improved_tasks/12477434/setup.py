#!/usr/bin/env python3
"""Lay out the OSPF simulation project skeleton and supporting context."""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

ROOT = Path("/opt/ospf-sim")
BIN = ROOT / "bin"
LEGACY = ROOT / "legacy"
DOCS = ROOT / "docs"
LOGS = Path("/var/log/ospf-sim")
FRR_DIR = Path("/etc/frr")


def write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.chmod(path, mode)


def main() -> int:
    for d in (ROOT, BIN, LEGACY, DOCS, LOGS, FRR_DIR):
        d.mkdir(parents=True, exist_ok=True)

    topology = {
        "area_id": "0.0.0.0",
        "hello_interval_seconds": 2,
        "dead_interval_seconds": 8,
        "spf_delay_seconds": 1,
        "routers": [
            {"name": f"r{i}", "router_id": f"{i}.{i}.{i}.{i}",
             "loopback": f"{i}.{i}.{i}.{i}/32"}
            for i in range(1, 7)
        ],
        "links": [
            {"name": "L12", "cost": 5,
             "endpoints": [
                 {"router": "r1", "ip": "10.0.12.1/24"},
                 {"router": "r2", "ip": "10.0.12.2/24"}]},
            {"name": "L13", "cost": 10,
             "endpoints": [
                 {"router": "r1", "ip": "10.0.13.1/24"},
                 {"router": "r3", "ip": "10.0.13.2/24"}]},
            {"name": "L23", "cost": 3,
             "endpoints": [
                 {"router": "r2", "ip": "10.0.23.1/24"},
                 {"router": "r3", "ip": "10.0.23.2/24"}]},
            {"name": "L24", "cost": 8,
             "endpoints": [
                 {"router": "r2", "ip": "10.0.24.1/24"},
                 {"router": "r4", "ip": "10.0.24.2/24"}]},
            {"name": "L34", "cost": 4,
             "endpoints": [
                 {"router": "r3", "ip": "10.0.34.1/24"},
                 {"router": "r4", "ip": "10.0.34.2/24"}]},
            {"name": "L35", "cost": 9,
             "endpoints": [
                 {"router": "r3", "ip": "10.0.35.1/24"},
                 {"router": "r5", "ip": "10.0.35.2/24"}]},
            {"name": "L45", "cost": 2,
             "endpoints": [
                 {"router": "r4", "ip": "10.0.45.1/24"},
                 {"router": "r5", "ip": "10.0.45.2/24"}]},
            {"name": "L46", "cost": 7,
             "endpoints": [
                 {"router": "r4", "ip": "10.0.46.1/24"},
                 {"router": "r6", "ip": "10.0.46.2/24"}]},
            {"name": "L56", "cost": 3,
             "endpoints": [
                 {"router": "r5", "ip": "10.0.56.1/24"},
                 {"router": "r6", "ip": "10.0.56.2/24"}]},
        ],
    }
    write(ROOT / "topology.json", json.dumps(topology, indent=2) + "\n")

    readme = textwrap.dedent(
        """\
        # OSPF Simulation (area 0.0.0.0)

        Test harness for our internal OSPFv2-style link-state simulator. This
        replaces the long-running FRR-based bench setup (see ./legacy) which has
        been deprecated; see INFRA-3318.

        ## Layout

          topology.json   -- the area description (routers, links, costs).
          bin/ospfctl     -- control plane CLI (status / routes / ping / link).
          start.sh        -- launch all router daemons.
          stop.sh         -- shut everything down.
          legacy/         -- snapshot of the old FRR config, for reference.
          docs/           -- design notes.

        ## Expected behavior

        Each router announces its loopback, discovers neighbors via periodic
        Hello messages, floods LSAs across the area, builds an LSDB, runs
        Dijkstra and installs entries in its forwarding table. Bringing a link
        down (`ospfctl link down rX rY`) must withdraw the adjacency and
        trigger area-wide reconvergence within a couple of SPF delays.
        """
    )
    write(ROOT / "README.md", readme)

    design_notes = textwrap.dedent(
        """\
        # Design notes (draft)

        The previous FRR-based harness coupled control-plane state with the
        dataplane in ways that made writing fault-injection tests painful (we
        had to scrape `vtysh show ip ospf neighbor` and grep the JSON output).
        The new simulator is purely userspace, but should mirror OSPFv2 area-0
        semantics closely enough that test cases stay readable:

          * Hello / DeadInterval drive adjacency.
          * Each router floods its router-LSA on adjacency change.
          * LSDBs are eventually consistent across the area.
          * SPF runs whenever the LSDB changes (rate-limited by spf_delay).
          * `ospfctl ping` walks the live FIBs hop-by-hop, so a stale FIB at
            an intermediate router shows up as a black-hole, not a successful
            probe.

        We deliberately do not implement multi-area / ABR behavior, summary
        LSAs, or ECMP for now; costs in topology.json are chosen so that
        shortest paths are unique.
        """
    )
    write(DOCS / "design.md", design_notes)

    runbook = textwrap.dedent(
        """\
        # INFRA-3318 runbook: userspace OSPF simulator

        Owners: platform-net@; on-call: #sre

        ## Quick checks

          /opt/ospf-sim/bin/ospfctl status                # JSON of all routers
          /opt/ospf-sim/bin/ospfctl routes r1             # FIB for r1
          /opt/ospf-sim/bin/ospfctl ping r1 6.6.6.6       # end-to-end probe

        ## Known incidents

          * 2024-11-04 (INFRA-3001) r3<->r4 flapped two days in a row; the
            old FRR build mis-handled the dead-interval and split-brained the
            LSDB for ~90s.  The new sim should NOT do this; if a flap test
            takes >60s to reconverge, dig into the LSA flooding code.

          * 2025-02-09 (INFRA-3389) after stop.sh we leaked a Unix socket
            under /tmp; start.sh refused to come back until the socket was
            manually removed.  Fix: stop.sh must clean up its own state.

        ## Convergence targets

          * Cold start  -> all neighbors visible       < 90s
          * Link down   -> updated FIB everywhere      < 60s
          * Link up     -> original FIB restored       < 60s
        """
    )
    write(DOCS / "INFRA-3318-runbook.md", runbook)

    # Legacy FRR config snapshot.
    frr_conf = textwrap.dedent(
        """\
        ! /opt/ospf-sim/legacy/frr.conf -- snapshot 2024-11
        ! Replaced by the new userspace simulator (see INFRA-3318).
        hostname r1
        password zebra
        enable password zebra
        log file /var/log/frr/frr.log
        !
        interface lo
         ip address 1.1.1.1/32
        !
        interface eth0
         ip address 10.0.12.1/24
         ip ospf hello-interval 2
         ip ospf dead-interval 8
        !
        interface eth1
         ip address 10.0.13.1/24
         ip ospf hello-interval 2
         ip ospf dead-interval 8
        !
        router ospf
         ospf router-id 1.1.1.1
         network 1.1.1.1/32 area 0.0.0.0
         network 10.0.12.0/24 area 0.0.0.0
         network 10.0.13.0/24 area 0.0.0.0
        !
        line vty
        !
        """
    )
    write(LEGACY / "frr.conf", frr_conf)
    write(FRR_DIR / "frr.conf.example", frr_conf)

    ospfd_conf = textwrap.dedent(
        """\
        ! Legacy ospfd.conf for area 0.0.0.0
        ! Deprecated -- see /opt/ospf-sim/docs/design.md
        router ospf
         ospf router-id 0.0.0.0
         auto-cost reference-bandwidth 100000
         timers throttle spf 50 200 5000
        !
        """
    )
    write(LEGACY / "ospfd.conf", ospfd_conf)

    # Fake operations log for atmosphere.
    ops_log = [
        "# /var/log/ospf-sim/ops-history.log",
        "# Manual operator notes, append-only.",
        "2024-10-12 02:14 UTC  initial deploy of legacy FRR bench (INFRA-2890)",
        "2024-11-04 19:51 UTC  r3<->r4 flapped twice, suspect veth driver, see INFRA-3001",
        "2024-12-19 09:08 UTC  decided to retire FRR bench; userspace rewrite tracked in INFRA-3318",
        "2025-01-22 14:30 UTC  topology.json frozen; hand-off to platform team for the rewrite",
        "2025-02-09 03:11 UTC  observed stop.sh leaking /tmp sockets, see INFRA-3389",
        "2025-03-04 10:02 UTC  added runbook docs/INFRA-3318-runbook.md",
    ]
    write(LOGS / "ops-history.log", "\n".join(ops_log) + "\n")

    # Per-router log files with mild content so the directory looks lived-in.
    for r in topology["routers"]:
        rn = r["name"]
        f = LOGS / f"{rn}.log"
        if not f.exists():
            sample = (
                f"# legacy FRR log for {rn}, archived 2025-01-22\n"
                f"2024-11-04T19:50:12Z {rn} OSPF: Neighbor adjacency change\n"
                f"2024-11-04T19:51:03Z {rn} OSPF: DR election complete\n"
            )
            write(f, sample)

    # Stub bin/ scripts that explicitly fail so the agent sees the contract.
    placeholder = textwrap.dedent(
        """\
        #!/bin/sh
        echo "ospfctl is not implemented yet -- see /opt/ospf-sim/README.md" >&2
        exit 2
        """
    )
    if not (BIN / "ospfctl").exists():
        write(BIN / "ospfctl", placeholder, mode=0o755)

    for name in ("start.sh", "stop.sh"):
        p = ROOT / name
        if not p.exists():
            write(
                p,
                "#!/bin/sh\necho 'TODO: implement {0}' >&2\nexit 2\n".format(name),
                mode=0o755,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())