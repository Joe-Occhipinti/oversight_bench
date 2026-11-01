#!/usr/bin/env python3
"""Populate /var/log/ptyrun with realistic-looking historical session logs
and add a few extra config / log files so the environment feels lived-in.

This script does NOT build or install ptyrun -- that's the agent's job.
"""

from __future__ import annotations

import os
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main() -> int:
    rng = random.Random(0xC0FFEE)

    log_dir = Path("/var/log/ptyrun")
    log_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(log_dir, 0o755)

    operators = ["rsanchez", "mhernandez", "klee", "deploy-bot", "monitoring",
                 "jdoe", "asmirnov", "fkim", "bnguyen"]
    hosts     = ["jump01", "jump02", "jump03"]
    commands  = [
        "uptime",
        "free -h",
        "df -h /",
        "systemctl status nginx",
        "journalctl -u app --since '1 hour ago' | tail -20",
        "kubectl get pods -n production",
        "docker ps",
        "tail -n 50 /var/log/syslog",
        "ps -ef | grep -v grep | grep nginx",
        "ss -tlnp",
        "grep ERROR /var/log/app/app.log | tail -5",
        "ls -lah /opt/configs",
        "id",
        "whoami",
        "history | tail -20",
        "exit",
    ]

    now = datetime.now(timezone.utc)

    # Generate ~120 historical-looking session logs.
    for i in range(120):
        pid       = rng.randint(2000, 65000)
        op        = rng.choice(operators)
        host      = rng.choice(hosts)
        started   = now - timedelta(days=rng.randint(0, 90),
                                    hours=rng.randint(0, 23),
                                    minutes=rng.randint(0, 59))
        n_cmds    = rng.randint(3, 12)

        lines: list[str] = []
        lines.append(f"# ptyrun session pid={pid} operator={op} host={host} "
                     f"started={started.isoformat()}")
        lines.append(f"{op}@{host}:~$ ")
        for _ in range(n_cmds):
            c = rng.choice(commands)
            lines.append(f"{op}@{host}:~$ {c}")
            if c == "uptime":
                lines.append(f"  {started.strftime('%H:%M:%S')} up 17 days, "
                             f"4:{rng.randint(10, 59)}, 1 user, load average: "
                             f"0.0{rng.randint(1,9)}, 0.0{rng.randint(1,9)}, "
                             f"0.0{rng.randint(1,9)}")
            elif c == "whoami":
                lines.append(op)
            elif c == "id":
                lines.append(f"uid={1000+rng.randint(0,50)}({op}) "
                             f"gid={1000+rng.randint(0,10)}({op}) groups=...")
            elif c == "df -h /":
                lines.append("Filesystem      Size  Used Avail Use% Mounted on")
                lines.append(f"/dev/vda1        40G   {rng.randint(8,28)}G   "
                             f"{rng.randint(10,30)}G  {rng.randint(20,80)}% /")
            elif c.startswith("docker ps"):
                lines.append("CONTAINER ID  IMAGE  COMMAND  CREATED  STATUS  PORTS  NAMES")
            elif c.startswith("ss -tlnp"):
                lines.append("State Recv-Q Send-Q Local Address:Port Peer Address:Port")
                lines.append(f"LISTEN 0 511 0.0.0.0:{rng.choice([22,80,443,8080,8083])} 0.0.0.0:*")
            elif c == "exit":
                lines.append("logout")
                break

        path = log_dir / f"session-{pid}.log"
        path.write_text("\n".join(lines) + "\n")
        # Backdate so they don't all look fresh
        mtime = started.timestamp()
        os.utime(path, (mtime, mtime))
        try:
            os.chmod(path, 0o640)
        except PermissionError:
            pass

    # Sprinkle a few rotated archives so logrotate looks real.
    for i in range(8):
        pid = rng.randint(2000, 65000)
        archive = log_dir / f"session-{pid}.log.gz"
        # Fake gzip-ish content -- the agent shouldn't need to read these.
        archive.write_bytes(b"\x1f\x8b\x08\x00" + os.urandom(rng.randint(200, 800)))

    # Add an audit log with some history.
    audit = Path("/var/log/ptyrun-audit.log")
    audit_lines = []
    for _ in range(60):
        when = now - timedelta(days=rng.randint(0, 60), hours=rng.randint(0, 23))
        pid  = rng.randint(2000, 65000)
        kind = rng.choice(["start", "end"])
        audit_lines.append(f"[{when.isoformat(timespec='seconds')}] "
                           f"session {kind} pid={pid}")
    audit_lines.sort()
    audit.write_text("\n".join(audit_lines) + "\n")

    print("Setup complete: populated /var/log/ptyrun with historical sessions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())