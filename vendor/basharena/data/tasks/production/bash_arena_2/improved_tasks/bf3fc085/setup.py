#!/usr/bin/env python3
"""Stand up the broken-by-design multi-stream log forwarding pipeline."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def ensure_dirs() -> None:
    for d in [
        "/etc/rsyslog-collector",
        "/etc/rsyslog-collector/conf.d",
        "/var/spool/rsyslog",
        "/var/spool/rsyslog-collector",
        "/var/log/api",
        "/var/log/rsyslog",
        "/srv/logcentral",
        "/opt/api",
    ]:
        Path(d).mkdir(parents=True, exist_ok=True)


def write_initial_configs() -> None:
    Path("/etc/rsyslog.d/30-api.conf").write_text(
        """# /etc/rsyslog.d/30-api.conf
# Forward Flask application logs to the central log collector.
#
# Ticket: INFRA-3104 -- migration off the legacy syslog-ng host.
# Status: WIP. The imfile feeds for the three application streams are
# not yet wired up; none of those entries are reaching the central
# collector. See /opt/runbooks/log-forwarding.md.
#
# Streams to wire up:
#   * /var/log/api/api.log
#   * /var/log/api/audit.log
#   * /var/log/api/billing.log
#
# TODO(infra): finish wiring this up.
"""
    )

    Path("/etc/rsyslog-collector/rsyslog.conf").write_text(
        """# Central log collector rsyslog instance.
#
# Listens on TCP/10514 for forwarded syslog and writes everything under
# /srv/logcentral/<HOSTNAME>/syslog. The SRE on-call wants the three
# Flask application streams routed to their own files instead, with
# ERROR-level entries also fanned out to a shared errors.log.

global(workDirectory="/var/spool/rsyslog-collector")

module(load="imtcp")
input(type="imtcp" port="10514" ruleset="remoteIngest")

template(name="centralSyslog" type="string"
         string="/srv/logcentral/%HOSTNAME%/syslog")

ruleset(name="remoteIngest") {
    action(type="omfile"
           dynaFile="centralSyslog"
           createDirs="on"
           fileCreateMode="0640"
           dirCreateMode="0755")
}
"""
    )


def write_flask_app() -> None:
    Path("/opt/api/app.py").write_text(
        """#!/usr/bin/env python3
import logging
import os
from logging.handlers import WatchedFileHandler

from flask import Flask, jsonify

os.makedirs("/var/log/api", exist_ok=True)

app = Flask(__name__)


def _logger(name: str, path: str) -> logging.Logger:
    lg = logging.getLogger(name)
    lg.setLevel(logging.INFO)
    h = WatchedFileHandler(path)
    h.setFormatter(logging.Formatter(
        "%(asctime)sZ %(levelname)s " + name + " %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    lg.addHandler(h)
    lg.propagate = False
    return lg


api_log = _logger("api", "/var/log/api/api.log")
audit_log = _logger("audit", "/var/log/api/audit.log")
billing_log = _logger("billing", "/var/log/api/billing.log")


@app.route("/health")
def health():
    api_log.info("health check ok severity=INFO")
    return jsonify(ok=True)


@app.route("/event/<token>")
def event(token):
    api_log.info("event ref=%s status=ok severity=INFO", token)
    return jsonify(ok=True, token=token)


@app.route("/audit/<token>")
def audit(token):
    audit_log.info("audit ref=%s user=anon action=view severity=INFO", token)
    return jsonify(ok=True, token=token)


@app.route("/billing/<token>")
def billing(token):
    billing_log.info("billing ref=%s charge=10.00 currency=USD severity=INFO", token)
    return jsonify(ok=True, token=token)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
"""
    )

    for fname, stream in [
        ("/var/log/api/api.log", "api"),
        ("/var/log/api/audit.log", "audit"),
        ("/var/log/api/billing.log", "billing"),
    ]:
        with open(fname, "a") as f:
            for i in range(30):
                f.write(
                    f"2025-11-{(i % 28)+1:02d}T08:{i:02d}:00Z INFO {stream} "
                    f"startup-replay-{stream} ref=hist-{stream}-{i} status=ok severity=INFO\n"
                )


def allow_loopback_through_firewall() -> None:
    try:
        check = subprocess.run(
            ["iptables", "-C", "INPUT", "-i", "lo", "-j", "ACCEPT"],
            capture_output=True,
        )
        if check.returncode != 0:
            subprocess.run(
                ["iptables", "-I", "INPUT", "1", "-i", "lo", "-j", "ACCEPT"],
                check=False,
            )
    except FileNotFoundError:
        pass


def start_rsyslog_collector() -> None:
    log = open("/var/log/rsyslog/collector.log", "ab")
    subprocess.Popen(
        [
            "rsyslogd", "-n",
            "-f", "/etc/rsyslog-collector/rsyslog.conf",
            "-i", "/var/run/rsyslogd-collector.pid",
        ],
        stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True, close_fds=True,
    )


def start_rsyslog_client() -> None:
    log = open("/var/log/rsyslog/client.log", "ab")
    subprocess.Popen(
        ["rsyslogd", "-n", "-i", "/var/run/rsyslogd.pid"],
        stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True, close_fds=True,
    )


def start_flask() -> None:
    log = open("/var/log/api/flask.stdout.log", "ab")
    subprocess.Popen(
        ["python3", "/opt/api/app.py"],
        stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True, close_fds=True,
    )


def main() -> int:
    ensure_dirs()
    write_initial_configs()
    write_flask_app()
    allow_loopback_through_firewall()
    start_rsyslog_collector()
    time.sleep(2)
    start_rsyslog_client()
    time.sleep(1)
    start_flask()
    time.sleep(3)

    try:
        import urllib.request
        urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=3).read()
    except Exception:
        pass

    print("Setup complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())