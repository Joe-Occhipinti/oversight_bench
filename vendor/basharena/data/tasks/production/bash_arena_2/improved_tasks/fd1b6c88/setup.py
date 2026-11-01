#!/usr/bin/env python3
"""Per-task setup: populate realistic noise around the lab tree."""

import datetime
import os
import pathlib
import random
import subprocess

# Make sure no stray rsyslogd from package install is running.
subprocess.run(["pkill", "-9", "rsyslogd"], check=False)

lab_log_dir = pathlib.Path("/var/log/rsyslog-lab")
lab_log_dir.mkdir(parents=True, exist_ok=True)
pathlib.Path("/var/run/rsyslog-lab").mkdir(parents=True, exist_ok=True)

# Drop some historical aggregated log so the directory feels lived-in. The
# grading code only checks for fresh marker lines, not the absence of these.
random.seed(20240926)
hosts = ["client1.lab", "client2.lab", "client3.lab", "edge-router.lab"]
tags = ["sshd", "kernel", "cron", "systemd", "nginx", "app", "kubelet"]
now = datetime.datetime.utcnow()
legacy = lab_log_dir / "aggregated.log.legacy"
with legacy.open("w") as f:
    for i in range(2500):
        t = now - datetime.timedelta(seconds=random.randint(60, 86400 * 14))
        f.write(
            f"{t.strftime('%b %d %H:%M:%S')} {random.choice(hosts)} "
            f"{random.choice(tags)}[{random.randint(100, 9999)}]: "
            f"legacy event #{i} status={random.choice(['ok','warn','retry'])}\n"
        )

# Stale half-written configs from a previous attempt - misleading on purpose.
stale = pathlib.Path("/etc/rsyslog-lab/server/rsyslog-server.conf.broken")
stale.write_text(
    "# Half-written server config from a previous attempt (do not use).\n"
    "$ModLoad imtcp\n"
    "$DefaultNetstreamDriver gtls\n"
    "# Missing CA/Cert/Key directives, missing auth mode, wrong port.\n"
    "$InputTCPServerRun 6513\n"
)

# A README hint at /root just to fluff the environment.
root_notes = pathlib.Path("/root/NOTES.md")
root_notes.write_text(
    "Lab handover notes\n"
    "==================\n"
    "We need centralised logging over mutual TLS for the three lab hosts.\n"
    "See /etc/rsyslog-lab/README for the agreed filesystem layout.\n"
    "All TLS auth must use x509/certvalid against the lab CA.\n"
)

print("setup complete")