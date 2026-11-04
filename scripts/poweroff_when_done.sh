#!/usr/bin/env bash
# Power the Scaleway instance off when the unattended run has ended, so that compute billing stops:
#   tmux new-session -d -s poweroff "bash scripts/poweroff_when_done.sh"
#
# Waits until tmux session `run` (scripts/run_unattended.sh) is absent at two checks CHECK_SECONDS apart (the run
# finished or gave up), flushes the disks, then sends the API action `poweroff`. Powered off, only volumes and
# flexible IPs are billed and block volumes keep their data (Scaleway docs, instances FAQ and power-off page).
# Never `terminate`: it deletes the volumes.
# Needs /root/.monitor/scaleway.env with SCW_SECRET_KEY=<secret key of an API key allowed to manage instances>.
# Instance id and zone come from the instance metadata. Log: /mnt/data/poweroff.log (or $LOG).
set -uo pipefail
CHECK_SECONDS="${CHECK_SECONDS:-300}"
LOG="${LOG:-/mnt/data/poweroff.log}"
exec > >(tee -a "$LOG") 2>&1
set -a; . /root/.monitor/scaleway.env; set +a

log() { echo "[$(date -u +%FT%TZ)] $*"; }
meta=$(curl -s --max-time 10 "http://169.254.42.42/conf?format=json")
id=$(python3 -c "import json,sys; print(json.load(sys.stdin)['id'])" <<< "$meta")
zone=$(python3 -c "import json,sys; print(json.load(sys.stdin)['location']['zone_id'])" <<< "$meta")
api="https://api.scaleway.com/instance/v1/zones/$zone/servers/$id"

state=$(curl -s --max-time 30 -H "X-Auth-Token: $SCW_SECRET_KEY" "$api" | python3 -c "import json,sys; print(json.load(sys.stdin)['server']['state'])")
if [ -z "$state" ]; then log "cannot read the instance with this API key; not watching"; exit 1; fi
log "watching tmux session run; instance $id ($zone) is $state"

absent=0
while [ "$absent" -lt 2 ]; do
  sleep "$CHECK_SECONDS"
  if tmux has-session -t run 2>/dev/null; then absent=0; else absent=$((absent + 1)); fi
done
log "run session ended; flushing disks and powering off"
sync
for attempt in 1 2 3 4 5; do
  code=$(curl -s -o /tmp/poweroff.out -w '%{http_code}' --max-time 60 -X POST -H "X-Auth-Token: $SCW_SECRET_KEY" \
    -H "Content-Type: application/json" -d '{"action": "poweroff"}' "$api/action")
  log "poweroff request: HTTP $code $(head -c 300 /tmp/poweroff.out)"
  [ "$code" = 202 ] && exit 0
  sleep 60
done
log "poweroff failed after 5 attempts"
exit 1
