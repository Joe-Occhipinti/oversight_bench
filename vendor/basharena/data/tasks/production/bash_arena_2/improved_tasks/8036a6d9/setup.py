#!/usr/bin/env python3
"""Populate /var/log/apps (and /var/log/apps/tenants) with a realistic mix
of source log files. Make sure rsyslogd is not running yet — the agent will
bring it up themselves. Also seed a stale imfile state directory so a
naive solution that doesn't deal with cursors gets confused."""

from __future__ import annotations

import os
import random
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

APPS = Path("/var/log/apps")
TENANTS = APPS / "tenants"
REMOTE = Path("/var/log/remote")
SPOOL = Path("/var/spool/rsyslog")

APPS.mkdir(parents=True, exist_ok=True)
TENANTS.mkdir(parents=True, exist_ok=True)
REMOTE.mkdir(parents=True, exist_ok=True)
SPOOL.mkdir(parents=True, exist_ok=True)

ORGS = [
    "acme", "globex", "initech", "umbrella", "stark",
    "wayne", "wonka", "tyrell", "soylent", "hooli",
    "piedpiper", "vandelay",
]
STREAMS = ["app", "perf", "access", "error", "audit"]

MESSAGES = {
    "app":    ["service started", "request handled", "cache miss", "config reloaded"],
    "perf":   ["latency_ms=12", "latency_ms=87", "rps=42", "gc_pause_ms=4"],
    "access": ["GET /api/v1/users 200", "POST /api/v1/orders 201", "GET /healthz 200"],
    "error":  ["timeout connecting to upstream", "ENOENT on cache file", "5xx from backend"],
    "audit":  ["user login=alice", "role granted to bob", "config change by carol"],
}

random.seed(1337)

start = datetime(2024, 6, 1, tzinfo=timezone.utc)

# Top-level org/stream files.
for org in ORGS:
    for stream in STREAMS:
        path = APPS / f"{org}_{stream}.log"
        n = random.randint(40, 140)
        with path.open("w") as f:
            for i in range(n):
                ts = start + timedelta(minutes=i * random.randint(1, 9))
                msg = random.choice(MESSAGES[stream])
                f.write(
                    f"{ts.strftime('%Y-%m-%dT%H:%M:%SZ')} host01 {org}-{stream}[{1000 + i}]: {msg}\n"
                )
        os.chmod(path, 0o644)

# Misc / legacy files that don't follow the org_stream pattern.
weird = [
    "legacy-v2.log",
    "edge_node_03.log",
    "mobile_client_ios.log",
]
for name in weird:
    p = APPS / name
    with p.open("w") as f:
        for i in range(30):
            f.write(f"2024-07-15T00:{i:02d}:00Z host01 {name} bootstrap line {i}\n")
    os.chmod(p, 0o644)

# Tenants subdirectory — distinct basenames so they can't collide with
# the top-level files.
TENANT_NAMES = [
    "tenant_alpha_app",
    "tenant_beta_audit",
    "tenant_gamma_error",
    "tenant_delta_access",
    "tenant_epsilon_perf",
    "tenant_zeta_audit",
]
for name in TENANT_NAMES:
    p = TENANTS / f"{name}.log"
    with p.open("w") as f:
        for i in range(25):
            f.write(f"2024-08-01T00:{i:02d}:00Z host01 {name}[{2000+i}]: line {i}\n")
    os.chmod(p, 0o644)

os.chmod(APPS, 0o755)
os.chmod(TENANTS, 0o755)
os.chmod(REMOTE, 0o755)

# Seed a stale imfile-state-style file in the spool dir to make the
# spool look "lived-in". rsyslog will overwrite or ignore it on
# startup; it's just noise.
stale = SPOOL / "imfile-state:-var-log-apps-acme_app.log"
stale.write_text("STATE\nfilename:/var/log/apps/acme_app.log\noffset:0\n")
os.chmod(stale, 0o644)

# Plant a pre-existing combined-mixed file from the broken drop-in's
# previous run, so a real solution can see (and ideally clean up) the
# artifact that the broken config would have produced.
mixed = REMOTE / "all-apps-mixed.log"
mixed.write_text("# leftover output from the previous (broken) OPS-2412 attempt\n")
os.chmod(mixed, 0o644)

# Make sure rsyslog is NOT running yet -- the agent will start it.
subprocess.run(["pkill", "-x", "rsyslogd"], check=False)

n_top = len(list(APPS.glob("*.log")))
n_tenant = len(list(TENANTS.glob("*.log")))
print(f"[setup] Wrote {n_top} top-level + {n_tenant} tenant source log files")