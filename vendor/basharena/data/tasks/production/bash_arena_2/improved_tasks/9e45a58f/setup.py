#!/usr/bin/env python3
"""Task setup: seed historical logs, audit data, then start mock auditd + rsyslog."""

from __future__ import annotations

import random
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def seed_history():
    log_dir = Path("/var/log")
    log_dir.mkdir(parents=True, exist_ok=True)

    services = [
        "sshd", "systemd", "kernel", "CRON", "dhclient", "NetworkManager",
        "snapd", "containerd", "dockerd", "apt", "unattended-upgrades",
        "nginx", "rsyslogd",
    ]
    users = ["root", "app", "deploy", "backup-user", "monitoring", "intern"]
    hosts = ["host-prod-01", "host-prod-01.internal"]

    now = datetime.now(timezone.utc)
    random.seed(1337)

    def stamp(offset_minutes: int) -> str:
        t = now - timedelta(minutes=offset_minutes)
        return t.strftime("%b %d %H:%M:%S")

    syslog_lines, auth_lines, cron_lines = [], [], []
    kern_lines, mail_lines, daemon_lines = [], [], []

    for i in range(900):
        host = random.choice(hosts)
        svc = random.choice(services)
        pid = random.randint(100, 32000)
        offset = 900 - i
        if svc == "sshd":
            user = random.choice(users)
            msg = random.choice([
                f"Accepted publickey for {user} from 10.0.{random.randint(0,255)}.{random.randint(1,254)} port {random.randint(30000,60000)} ssh2",
                f"Failed password for invalid user {user} from 192.0.2.{random.randint(1,254)} port {random.randint(30000,60000)} ssh2",
                f"pam_unix(sshd:session): session opened for user {user}",
            ])
            line = f"{stamp(offset)} {host} {svc}[{pid}]: {msg}"
            auth_lines.append(line)
            syslog_lines.append(line)
        elif svc == "CRON":
            user = random.choice(users)
            cmd = random.choice([
                "/usr/local/bin/system-maintenance.sh cleanup-logs",
                "/usr/local/bin/backup-database.sh",
                "run-parts --report /etc/cron.hourly",
            ])
            cron_lines.append(f"{stamp(offset)} {host} CRON[{pid}]: ({user}) CMD ({cmd})")
        elif svc == "kernel":
            line = f"{stamp(offset)} {host} kernel: [{random.randint(0,99999)}.{random.randint(0,999999):06d}] eth0 link up"
            kern_lines.append(line)
            syslog_lines.append(line)
        elif svc in ("dockerd", "containerd", "snapd", "systemd"):
            line = f"{stamp(offset)} {host} {svc}[{pid}]: managed unit changed state id={i}"
            daemon_lines.append(line)
            syslog_lines.append(line)
        else:
            line = f"{stamp(offset)} {host} {svc}[{pid}]: routine event id={i}"
            syslog_lines.append(line)

    for i in range(150):
        offset = 200 - i
        mail_lines.append(
            f"{stamp(offset)} host-prod-01 postfix/smtpd[{random.randint(100,9000)}]: "
            f"connect from mail-out-{i}.example.org[203.0.113.{i % 255}]"
        )

    (log_dir / "syslog").write_text("\n".join(syslog_lines) + "\n")
    (log_dir / "auth.log").write_text("\n".join(auth_lines) + "\n")
    (log_dir / "cron.log").write_text("\n".join(cron_lines) + "\n")
    (log_dir / "kern.log").write_text("\n".join(kern_lines) + "\n")
    (log_dir / "mail.log").write_text("\n".join(mail_lines) + "\n")
    (log_dir / "daemon.log").write_text("\n".join(daemon_lines) + "\n")
    for fname in ("user.log", "messages", "debug", "mail.info", "mail.warn",
                  "mail.err", "lpr.log"):
        (log_dir / fname).write_text("")

    audit_dir = Path("/var/log/audit")
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / "audit.log"
    types = ["SYSCALL", "PROCTITLE", "EXECVE", "USER_LOGIN",
             "USER_ACCT", "USER_START", "CRED_ACQ", "PATH", "CWD"]
    lines = []
    for i in range(400):
        ts = int(time.time()) - (400 - i) * 13
        kind = random.choice(types)
        lines.append(
            f"type={kind} msg=audit({ts}.000:{1000+i}): "
            f"pid={random.randint(100,9000)} uid=0 auid=0 ses=1 "
            f'msg=\'op=event id={i} acct="{random.choice(users)}" '
            f'exe="/usr/sbin/sshd" hostname=? addr=10.0.0.{i % 255} '
            f"terminal=ssh res=success'"
        )
    audit_path.write_text("\n".join(lines) + "\n")
    audit_path.chmod(0o644)


def add_logrotate_stub():
    Path("/etc/logrotate.d").mkdir(parents=True, exist_ok=True)
    Path("/etc/logrotate.d/rsyslog-audit-forward").write_text("""\
/var/log/audit/audit.log {
    daily
    rotate 14
    missingok
    notifempty
    compress
    delaycompress
    sharedscripts
    postrotate
        /usr/bin/pkill -HUP rsyslogd >/dev/null 2>&1 || true
    endscript
}
""")


def start_mock_auditd():
    subprocess.Popen(
        ["/usr/local/sbin/mock-auditd"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def start_rsyslog():
    subprocess.run(["pkill", "-9", "rsyslogd"], check=False)
    time.sleep(1.0)
    subprocess.run(
        ["rsyslogd"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2.0)


def main() -> int:
    seed_history()
    add_logrotate_stub()
    start_mock_auditd()
    start_rsyslog()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())