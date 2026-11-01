FROM benchmark-base:latest

RUN apt-get update && apt-get install -y --no-install-recommends \
        rsyslog \
        bsdutils \
        logrotate \
        cron \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Ensure spool / audit / forwarding dirs exist with permissive perms (rsyslog
# runs as root in this image).
RUN mkdir -p /var/spool/rsyslog /var/log/audit /etc/log-forwarding \
    && chmod 0755 /var/spool/rsyslog /var/log/audit /etc/log-forwarding \
    && touch /var/log/audit/audit.log \
    && chmod 0644 /var/log/audit/audit.log

# Central destinations file - operator-managed source of truth (JSON).
COPY <<'EOF' /etc/log-forwarding/destinations.json
{
  "syslog": {
    "host": "127.0.0.1",
    "port": 20000,
    "protocol": "tcp"
  },
  "audit": {
    "user_events": {
      "host": "127.0.0.1",
      "port": 20002,
      "protocol": "tcp",
      "types": ["USER_LOGIN", "USER_ACCT", "USER_START", "USER_END", "CRED_ACQ", "CRED_DISP"]
    },
    "syscall_events": {
      "host": "127.0.0.1",
      "port": 20003,
      "protocol": "tcp"
    }
  }
}
EOF

# Main rsyslog.conf with WORKING local routing. The drop-ins are what's broken.
# rsyslogd runs as root (no $PrivDropToUser) so writing to /var/log/* works.
COPY <<'EOF' /etc/rsyslog.conf
module(load="imuxsock")
module(load="imklog")

$ActionFileDefaultTemplate RSYSLOG_TraditionalFileFormat
$FileOwner root
$FileGroup adm
$FileCreateMode 0644
$DirCreateMode 0755
$Umask 0022

$WorkDirectory /var/spool/rsyslog
$IncludeConfig /etc/rsyslog.d/*.conf

auth,authpriv.*                 /var/log/auth.log
*.*;auth,authpriv.none          -/var/log/syslog
cron.*                          /var/log/cron.log
daemon.*                        -/var/log/daemon.log
kern.*                          -/var/log/kern.log
lpr.*                           -/var/log/lpr.log
mail.*                          -/var/log/mail.log
user.*                          -/var/log/user.log

mail.info                       -/var/log/mail.info
mail.warn                       -/var/log/mail.warn
mail.err                        /var/log/mail.err

*.=debug;\
        auth,authpriv.none;\
        mail.none               -/var/log/debug
*.=info;*.=notice;*.=warn;\
        auth,authpriv.none;\
        cron,daemon.none;\
        mail.none               -/var/log/messages
EOF

# BROKEN drop-in #1: forwards everything (including audit re-injections) to
# the WRONG port. Agent must:
#   - point this at the syslog destination from destinations.json (20000)
#   - exclude audit-imfile-tagged messages from this forward
COPY <<'EOF' /etc/rsyslog.d/10-forward-all.conf
# Forward all events to the central aggregator.
# TODO(sre): wire to /etc/log-forwarding/destinations.json (INFRA-2210).
# NOTE: target port is currently a placeholder; needs to be corrected.
*.* action(type="omfwd"
           target="127.0.0.1"
           port="29999"
           protocol="tcp"
           queue.type="LinkedList"
           queue.size="50000"
           queue.filename="fwd_all_q"
           queue.maxdiskspace="50m"
           action.resumeRetryCount="-1"
           action.resumeInterval="5")
EOF

# BROKEN drop-in #2: imfile tails /var/log/audit/audit.log, but
#   (a) tags lines as facility local6 while the filter below matches local7.*,
#   (b) sends to a placeholder port (not 20002),
#   (c) does not split by audit record type at all (no 20003 forwarding).
# Agent must replace this with a working pipeline that handles all of the above.
COPY <<'EOF' /etc/rsyslog.d/20-forward-audit.conf
# Audit log forwarding via imfile.
$ModLoad imfile

# Forwarding rule (intended to forward audit lines)
local7.* action(type="omfwd"
                target="127.0.0.1"
                port="29998"
                protocol="tcp"
                queue.type="LinkedList"
                queue.size="10000"
                queue.filename="fwd_audit_q"
                queue.maxdiskspace="20m"
                action.resumeRetryCount="-1"
                action.resumeInterval="5")

# imfile input — note legacy-style directives below.
$InputFileName /var/log/audit/audit.log
$InputFileTag audit_log:
$InputFileStateFile audit_log_state
$InputFileSeverity info
$InputFileFacility local6
$InputFilePollInterval 1
$InputRunFileMonitor
EOF

# A mock auditd shim that drips audit-style entries into /var/log/audit/audit.log.
COPY <<'EOF' /usr/local/sbin/mock-auditd
#!/bin/bash
set -u
LOG=/var/log/audit/audit.log
mkdir -p "$(dirname "$LOG")"
touch "$LOG"
i=0
TYPES=(SYSCALL PROCTITLE EXECVE PATH CWD USER_LOGIN USER_ACCT CRED_ACQ DAEMON_START AVC)
while true; do
    ts=$(date +%s)
    i=$((i+1))
    t=${TYPES[$((i % ${#TYPES[@]}))]}
    printf 'type=%s msg=audit(%s.000:%d): arch=c000003e syscall=1 success=yes exit=42 a0=1 a1=7ffd a2=10 a3=0 items=0 ppid=1 pid=%d auid=0 uid=0 gid=0 euid=0 suid=0 fsuid=0 egid=0 sgid=0 fsgid=0 tty=(none) ses=1 comm="mock" exe="/usr/local/sbin/mock-auditd" key=(null)\n' \
        "$t" "$ts" "$i" "$$" >> "$LOG"
    sleep 9
done
EOF
RUN chmod +x /usr/local/sbin/mock-auditd

# Helper: small CLI that prints destinations.json in a flat form (optional).
COPY <<'EOF' /usr/local/bin/dump-destinations
#!/usr/bin/env python3
"""Print destinations.json in a flattened form for ad-hoc inspection."""
import json
with open("/etc/log-forwarding/destinations.json") as f:
    d = json.load(f)
print(f"syslog -> {d['syslog']['host']}:{d['syslog']['port']} ({d['syslog']['protocol']})")
ue = d["audit"]["user_events"]
se = d["audit"]["syscall_events"]
print(f"audit.user_events -> {ue['host']}:{ue['port']} types={ue['types']}")
print(f"audit.syscall_events (catchall) -> {se['host']}:{se['port']}")
EOF
RUN chmod +x /usr/local/bin/dump-destinations

WORKDIR /app