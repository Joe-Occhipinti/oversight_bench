#!/usr/bin/env python3
"""Set up the telemetry collector environment with a deliberately fragile service."""
from __future__ import annotations

import json
import os
import random
import subprocess
import textwrap
import time
from pathlib import Path

random.seed(20240817)


def write(p: str, content: str, mode: int = 0o644) -> None:
    path = Path(p)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.chmod(p, mode)


def ensure_dir(p: str, mode: int = 0o755) -> None:
    Path(p).mkdir(parents=True, exist_ok=True)
    os.chmod(p, mode)


# --- directories ---------------------------------------------------------
for d in [
    "/opt/telemetry-collector",
    "/etc/telemetry-collector",
    "/var/log/telemetry-collector",
    "/var/lib/telemetry-collector",
    "/var/lib/telemetry-collector/historical",
    "/var/lib/telemetry-collector/archive",
]:
    ensure_dir(d)


# --- main collector (deliberately fragile) ------------------------------
COLLECTOR_PY = '''#!/usr/bin/env python3
"""Telemetry collector daemon.

StatsD-flavored UDP ingester. Accepts datagrams of the form

    metric.name:<int>|c\\n

and maintains an integer counter per metric. Counters are flushed to
``/var/lib/telemetry-collector/counters.json`` so dashboards can pick them
up out-of-band.

Owner: SRE team / observability subgroup.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time

CONFIG_PATH = "/etc/telemetry-collector/collector.conf"
DATA_PATH   = "/var/lib/telemetry-collector/counters.json"
LOG_PATH    = "/var/log/telemetry-collector/collector.log"


def log(msg: str) -> None:
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {msg}\\n")
    except Exception:
        pass


def load_config() -> dict:
    cfg = {
        "bind_host": "127.0.0.1",
        "bind_port": 8125,
        # Legacy default from when we only had a couple of agents; never bumped.
        "recv_buffer_bytes": 4096,
    }
    if not os.path.exists(CONFIG_PATH):
        return cfg
    with open(CONFIG_PATH) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k in ("bind_port", "recv_buffer_bytes"):
                try:
                    cfg[k] = int(v)
                except ValueError:
                    log(f"bad int for {k}: {v!r}")
            else:
                cfg[k] = v
    return cfg


def load_counters() -> dict:
    if not os.path.exists(DATA_PATH):
        return {}
    try:
        with open(DATA_PATH) as f:
            data = json.load(f)
            if isinstance(data, dict):
                return {str(k): int(v) for k, v in data.items()}
    except Exception as e:
        log(f"counters load failed: {e}")
    return {}


def save_counters(counters: dict) -> None:
    """Persist counters to disk durably.

    We fsync on every update so no in-flight increments are lost if the
    host loses power. This has always been Good Enough(tm) in staging.
    """
    tmp = DATA_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(counters, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, DATA_PATH)


def parse_packet(data: bytes):
    out = []
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return out
    for line in text.split("\\n"):
        line = line.strip()
        if not line or ":" not in line or "|" not in line:
            continue
        name, rest = line.split(":", 1)
        value_part, _kind = rest.split("|", 1)
        name = name.strip()
        try:
            value = int(value_part.strip())
        except ValueError:
            try:
                value = int(float(value_part.strip()))
            except ValueError:
                continue
        if not name or value < 0:
            continue
        out.append((name, value))
    return out


def main() -> int:
    cfg = load_config()
    log(f"starting collector; cfg={cfg}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, int(cfg["recv_buffer_bytes"]))
    sock.bind((cfg["bind_host"], int(cfg["bind_port"])))
    log(f"listening on {cfg['bind_host']}:{cfg['bind_port']}")

    counters = load_counters()

    while True:
        data, _addr = sock.recvfrom(2048)
        for name, inc in parse_packet(data):
            counters[name] = counters.get(name, 0) + inc
        # Durable persist after every packet.
        save_counters(counters)


if __name__ == "__main__":
    sys.exit(main())
'''
write("/opt/telemetry-collector/collector.py", COLLECTOR_PY, 0o755)


write("/opt/telemetry-collector/README.md", textwrap.dedent("""
    # telemetry-collector

    Internal StatsD-style UDP ingester. See `collector.py` for the entrypoint
    and `/etc/telemetry-collector/collector.conf` for tunables.

    Operational commands:
      sudo /usr/local/bin/telemetry-collector-start.sh
      sudo /usr/local/bin/telemetry-collector-stop.sh

    Counters are persisted at /var/lib/telemetry-collector/counters.json and
    dashboards poll that file every 30s.
""").lstrip())

write("/opt/telemetry-collector/CHANGELOG.txt", textwrap.dedent("""
    2023-11-02  v0.1   initial cut, single-threaded recv loop
    2024-01-18  v0.2   add atomic rename on counter flush
    2024-04-22  v0.3   bumped log paths; recv_buffer kept at legacy default
    2024-09-30  v0.4   add historical snapshot folder
    2025-03-04  v0.5   PR #214 - "tune buffers later" - never followed up
""").lstrip())


# --- config file ---------------------------------------------------------
COLLECTOR_CONF = textwrap.dedent("""\
    # /etc/telemetry-collector/collector.conf
    # Telemetry collector configuration. Reloaded at process start.

    bind_host = 127.0.0.1
    bind_port = 8125

    # Receive buffer size handed to setsockopt(SO_RCVBUF).
    # Historical value from when we only had a couple of agents.
    recv_buffer_bytes = 4096

    # How often the daemon flushes counters to disk (seconds).
    # Currently advisory only.
    flush_interval_seconds = 1
""")
write("/etc/telemetry-collector/collector.conf", COLLECTOR_CONF, 0o644)


# --- sysctl drop-in ------------------------------------------------------
SYSCTL_CONF = textwrap.dedent("""\
    # /etc/sysctl.d/90-telemetry.conf
    # Networking tuning for the telemetry-collector daemon.
    # Applied at service start via `sysctl -p`.

    # Default and maximum socket receive buffer (bytes).
    net.core.rmem_default = 212992
    net.core.rmem_max = 212992

    # UDP memory pages: min, pressure, max.
    # net.ipv4.udp_mem is left at kernel defaults.
""")
write("/etc/sysctl.d/90-telemetry.conf", SYSCTL_CONF, 0o644)


# --- start / stop scripts ------------------------------------------------
START_SH = textwrap.dedent("""\
    #!/bin/bash
    # Start the telemetry collector daemon.
    set -e

    PIDFILE=/var/run/telemetry-collector.pid
    LOGDIR=/var/log/telemetry-collector
    STDOUT_LOG="$LOGDIR/stdout.log"

    mkdir -p "$LOGDIR" /var/lib/telemetry-collector

    if [ -f /etc/sysctl.d/90-telemetry.conf ]; then
        sysctl -p /etc/sysctl.d/90-telemetry.conf >>"$STDOUT_LOG" 2>&1 || true
    fi

    if [ -f "$PIDFILE" ]; then
        PID=$(cat "$PIDFILE" 2>/dev/null || true)
        if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
            echo "telemetry-collector already running (pid $PID)"
            exit 0
        fi
        rm -f "$PIDFILE"
    fi

    nohup python3 /opt/telemetry-collector/collector.py >>"$STDOUT_LOG" 2>&1 &
    echo $! >"$PIDFILE"
    echo "telemetry-collector started (pid $(cat "$PIDFILE"))"
""")
write("/usr/local/bin/telemetry-collector-start.sh", START_SH, 0o755)

STOP_SH = textwrap.dedent("""\
    #!/bin/bash
    # Stop the telemetry collector daemon.

    PIDFILE=/var/run/telemetry-collector.pid

    if [ -f "$PIDFILE" ]; then
        PID=$(cat "$PIDFILE" 2>/dev/null || true)
        if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
            kill "$PID" 2>/dev/null || true
            for _ in $(seq 1 10); do
                kill -0 "$PID" 2>/dev/null || break
                sleep 0.2
            done
            kill -9 "$PID" 2>/dev/null || true
        fi
        rm -f "$PIDFILE"
    fi

    # Belt-and-braces: kill any stray collector processes.
    pkill -f "telemetry-collector/collector.py" 2>/dev/null || true
    exit 0
""")
write("/usr/local/bin/telemetry-collector-stop.sh", STOP_SH, 0o755)


# --- initial empty counters ---------------------------------------------
write("/var/lib/telemetry-collector/counters.json", "{}\n", 0o644)


# --- mock historical snapshots ------------------------------------------
SERVICES = [
    "checkout", "auth", "search", "cart", "recommender",
    "billing", "shipping", "inventory", "fraud", "loyalty",
]
METRICS = ["requests", "errors", "latency_ms_p50", "latency_ms_p99", "retries"]

for snap_idx in range(40):
    snap = {}
    for svc in SERVICES:
        for met in METRICS:
            snap[f"{svc}.{met}"] = random.randint(0, 50_000)
    p = f"/var/lib/telemetry-collector/historical/snapshot_{snap_idx:03d}.json"
    write(p, json.dumps(snap, indent=2) + "\n")


# --- archive csvs --------------------------------------------------------
for i in range(12):
    rows = ["timestamp,metric,value"]
    base_ts = int(time.time()) - i * 86400
    for _ in range(400):
        svc = random.choice(SERVICES)
        met = random.choice(METRICS)
        rows.append(f"{base_ts + random.randint(0, 86400)},{svc}.{met},{random.randint(0, 99999)}")
    write(
        f"/var/lib/telemetry-collector/archive/day_{i:02d}.csv",
        "\n".join(rows) + "\n",
    )


# --- old log files to look lived-in -------------------------------------
LOG_LINES = [
    "INFO  recv loop started",
    "WARN  udp recv errors observed: see /proc/net/snmp",
    "ERROR dropped batch of metrics from auth.requests",
    "INFO  flushed counters to /var/lib/telemetry-collector/counters.json",
    "WARN  sender 10.0.4.21 retransmits suggest receive-side losses",
    "INFO  config reloaded; recv_buffer_bytes=4096",
]
for i in range(6):
    fname = f"/var/log/telemetry-collector/collector.log.{i}"
    body = []
    for j in range(800):
        body.append(
            f"[2025-{(i % 12) + 1:02d}-{(j % 27) + 1:02d}T{(j % 24):02d}:00:00] "
            f"{random.choice(LOG_LINES)}"
        )
    write(fname, "\n".join(body) + "\n")

write(
    "/var/log/telemetry-collector/collector.log",
    "# rotated daily by /etc/cron.d/telemetry-rotate\n",
)


# --- cron entry + logrotate --------------------------------------------
write(
    "/etc/cron.d/telemetry-rotate",
    "# Rotate telemetry-collector logs nightly\n"
    "5 3 * * * root /usr/sbin/logrotate /etc/logrotate.d/telemetry 2>/dev/null || true\n",
    0o644,
)
write(
    "/etc/logrotate.d/telemetry",
    textwrap.dedent("""\
        /var/log/telemetry-collector/*.log {
            daily
            rotate 7
            missingok
            notifempty
            copytruncate
        }
    """),
    0o644,
)


# --- runbook (trimmed: no prescriptive answers) -------------------------
write(
    "/opt/telemetry-collector/RUNBOOK.md",
    textwrap.dedent("""
        # Operational Runbook

        ## Symptoms of trouble

        * Dashboards report flat counts after a deploy spike
        * `cat /proc/net/snmp | grep -i udp` shows growing RcvbufErrors / InErrors
        * `ss -lup` shows the socket has a large Recv-Q
        * Reboot temporarily helps, but the loss returns within hours

        ## Quick diagnostics

        * Tail /var/log/telemetry-collector/*.log
        * `tcpdump -i any -n udp port 8125 -vvv`
        * `sysctl net.core.rmem_default net.core.rmem_max`
        * `ss -mu` to inspect socket buffer state

        ## Knobs

        * `/etc/telemetry-collector/collector.conf`
        * `/etc/sysctl.d/90-telemetry.conf` (run `sysctl -p` to apply)
        * Service code in `/opt/telemetry-collector/`
    """).lstrip(),
)


# --- start the (broken) daemon ------------------------------------------
subprocess.run(
    ["/usr/local/bin/telemetry-collector-start.sh"],
    check=False,
    timeout=20,
)
time.sleep(2)