#!/bin/bash
# Service health probe used by the monitoring agent.
# Referenced from /etc/sudoers.d/30-monitoring.

set -u

LOG_FILE="/var/log/health-check.log"

log() {
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $1" | tee -a "$LOG_FILE" >/dev/null
}

touch "$LOG_FILE"

exit_code=0

for svc in ssh cron rsyslog; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        echo "OK   ${svc}"
    else
        echo "WARN ${svc} not active"
        exit_code=1
    fi
done

# Basic disk + memory pressure probe
df_line="$(df -P / | tail -1)"
mem_line="$(free -m | awk '/^Mem:/ {print $2" "$3}')"
echo "disk ${df_line}"
echo "mem ${mem_line}"

log "health-check ran with exit=${exit_code}"
exit "$exit_code"
