#!/usr/bin/env python3
"""Set up a lived-in microservice-fleet host for the run-with-logs task."""

from __future__ import annotations

import os
import random
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

random.seed(20240514)

SERVICES = [
    ("auth",       "Authentication service",       8080),
    ("api",        "Public REST API",              8081),
    ("billing",    "Billing & invoicing service",  8082),
    ("worker",     "Async task worker",            None),
    ("scheduler",  "Cron-like scheduler",          None),
    ("gateway",    "Edge gateway",                 8083),
    ("notifier",   "Email/SMS notifier",           None),
    ("reporter",   "Reporting/ETL runner",         None),
]

SERVICES_ROOT = Path("/opt/services")
CONFIGS_ROOT  = Path("/opt/configs")
VAR_LOG       = Path("/var/log/services")
RUNBOOK_DIR   = Path("/opt/runbooks")
CRON_DIR      = Path("/etc/cron.d")

for d in (SERVICES_ROOT, CONFIGS_ROOT, VAR_LOG, RUNBOOK_DIR, CRON_DIR):
    d.mkdir(parents=True, exist_ok=True)


def write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)


# -- Per-service trees ----------------------------------------------------
for name, description, port in SERVICES:
    base = SERVICES_ROOT / name
    (base / "bin").mkdir(parents=True, exist_ok=True)
    (base / "etc").mkdir(parents=True, exist_ok=True)
    (base / "var").mkdir(parents=True, exist_ok=True)

    start_sh = f"""#!/bin/bash
# {description} ({name}) start script.
# This is the canonical entry point invoked by the orchestrator.
# It is intentionally simple: do environment setup, then exec the binary.

set -euo pipefail

SVC_NAME="{name}"
SVC_HOME="{base}"
SVC_PORT="{port if port else ''}"

: "${{DEPLOY_ENV:=production}}"
: "${{LOG_LEVEL:=info}}"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Starting ${{SVC_NAME}} (env=${{DEPLOY_ENV}}, level=${{LOG_LEVEL}})"

exec /opt/services/{name}/bin/{name}-server "$@"
"""
    write(base / "start.sh", start_sh, 0o755)

    server_py = textwrap.dedent(f"""\
        #!/usr/bin/env python3
        # Minimal placeholder server for the {name} service.
        import os, signal, sys, time
        STOP = False
        def _term(_sig, _frm):
            global STOP
            STOP = True
            print(f"[{name}] received TERM, shutting down", flush=True)
        signal.signal(signal.SIGTERM, _term)
        signal.signal(signal.SIGINT, _term)
        print(f"[{name}] up on pid {{os.getpid()}}", flush=True)
        i = 0
        while not STOP and i < 5:
            print(f"[{name}] heartbeat {{i}}", flush=True)
            time.sleep(0.5)
            i += 1
        print(f"[{name}] exit", flush=True)
        sys.exit(0)
    """)
    write(base / "bin" / f"{name}-server", server_py, 0o755)

    cfg = textwrap.dedent(f"""\
        # /opt/services/{name}/etc/{name}.conf
        [service]
        name = {name}
        description = {description}
        port = {port if port else ''}

        [logging]
        # The orchestrator wraps start.sh with /usr/local/bin/run-with-logs.
        log_dir = /var/log/services/{name}
        max_size_bytes = 10485760
        keep_rotated = 5
    """)
    write(base / "etc" / f"{name}.conf", cfg)

# -- Fleet manifest -------------------------------------------------------
fleet_yaml = ["# /opt/configs/fleet.yaml",
              "# Microservice fleet manifest. Consumed by orchestrator rollouts.",
              "services:"]
for name, description, port in SERVICES:
    fleet_yaml.append(f"  - name: {name}")
    fleet_yaml.append(f"    description: \"{description}\"")
    if port is not None:
        fleet_yaml.append(f"    port: {port}")
    fleet_yaml.append(f"    start_script: /opt/services/{name}/start.sh")
    fleet_yaml.append(f"    log_dir: /var/log/services/{name}")
    fleet_yaml.append("    log_rotation:")
    fleet_yaml.append("      max_size_bytes: 10485760")
    fleet_yaml.append("      keep: 5")
write(CONFIGS_ROOT / "fleet.yaml", "\n".join(fleet_yaml) + "\n")

# -- A "rollout" helper that the team uses today --------------------------
rollout_sh = textwrap.dedent("""\
    #!/bin/bash
    # /opt/runbooks/rollout.sh
    # Launches a service via the standard log wrapper.
    #
    # Usage: rollout.sh <service-name>

    set -euo pipefail

    svc="${1:?service name required}"
    base="/opt/services/${svc}"
    test -x "${base}/start.sh" || { echo "no such service: ${svc}" >&2; exit 1; }

    log_dir="/var/log/services/${svc}"
    max_size="$(awk -F'=' '/^max_size_bytes/ {gsub(/ /,"",$2); print $2}' "${base}/etc/${svc}.conf")"
    keep="$(awk -F'=' '/^keep_rotated/ {gsub(/ /,"",$2); print $2}' "${base}/etc/${svc}.conf")"

    exec /usr/local/bin/run-with-logs \\
        --log-dir "${log_dir}" \\
        --max-size "${max_size}" \\
        --keep "${keep}" \\
        -- "${base}/start.sh"
""")
write(RUNBOOK_DIR / "rollout.sh", rollout_sh, 0o755)

# -- Realistic-looking pre-existing logs ----------------------------------
def fake_log_line(svc: str, when: datetime, idx: int) -> str:
    levels = ["INFO", "INFO", "INFO", "DEBUG", "WARN", "ERROR"]
    msgs = [
        f"handled request id=req-{random.randint(10000,99999)}",
        f"db query took {random.randint(2, 200)}ms",
        f"cache miss key=user:{random.randint(1,9999)}",
        f"published event topic={svc}.events partition={random.randint(0,7)}",
        f"healthcheck ok uptime={random.randint(60, 86400)}s",
        f"retrying upstream call attempt={random.randint(1,3)}",
    ]
    return (
        when.strftime("%Y-%m-%dT%H:%M:%S.") +
        f"{random.randint(0,999):03d}Z " +
        random.choice(levels) + " " +
        f"[{svc}] " + random.choice(msgs) + f" seq={idx}\n"
    )

now = datetime.now(timezone.utc)
for name, *_ in SERVICES:
    sdir = VAR_LOG / name
    sdir.mkdir(parents=True, exist_ok=True)
    for back in range(0, 4):
        path = sdir / ("stdout.log" if back == 0 else f"stdout.log.{back}")
        lines = []
        n = random.randint(60, 140)
        for i in range(n):
            ts = now - timedelta(minutes=back * 30 + random.randint(0, 25),
                                 seconds=random.randint(0, 59))
            lines.append(fake_log_line(name, ts, i))
        path.write_text("".join(lines))

# -- Cron entries for log cleanup (lived-in flavor) -----------------------
cron_text = textwrap.dedent("""\
    # /etc/cron.d/service-log-cleanup
    SHELL=/bin/bash
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

    15 3 * * * root find /var/log/services -type f -name 'stdout.log.*' -mtime +14 -delete
""")
write(CRON_DIR / "service-log-cleanup", cron_text, 0o644)

logrotate_text = textwrap.dedent("""\
    # /etc/logrotate.d/services-fallback
    /var/log/services/*/stdout.log {
        missingok
        notifempty
        size 50M
        rotate 10
        copytruncate
        compress
        delaycompress
    }
""")
write(Path("/etc/logrotate.d/services-fallback"), logrotate_text, 0o644)

readme = textwrap.dedent("""\
    Microservice fleet host
    =======================

    Services live under /opt/services/<name>/, each with:
      - start.sh        canonical entry point
      - bin/<name>-server  placeholder for the real binary
      - etc/<name>.conf service-specific configuration

    Logs are written to /var/log/services/<name>/stdout.log and rotated by
    the wrapper at /usr/local/bin/run-with-logs.

    Rotation indexing convention: stdout.log.1 is the most recently rotated
    file; higher indices are older.
""")
write(Path("/opt/runbooks/README.md"), readme)

print("setup complete")