#!/usr/bin/env bash
# Run one experiment unattended (spec 7.7):  bash scripts/run_unattended.sh <run_id>   (start it inside tmux)
#
# 1. Starts the run from runs/<run_id>.yaml, or resumes it if results/<run_id>/ holds a started run.
# 2. When the run stops before finishing (crash, or samples still errored after Inspect's own retry), it waits,
#    removes leftover sample containers and resumes, up to RESUMES times.
# 3. When the run has finished, runs again the samples lost to an outage of the model provider
#    (`resume --redo-provider-failures`), up to REDO_ROUNDS rounds, each followed by step 2.
# Output: results/<run_id>-unattended.log. Exit code 0 when the run finished.
set -uo pipefail

RUN_ID="$1"
RESUMES="${RESUMES:-5}"
REDO_ROUNDS="${REDO_ROUNDS:-3}"
WAIT_SECONDS="${WAIT_SECONDS:-600}"  # before each resume: gives a provider outage time to end

cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"
if [ -f .env ]; then set -a; . ./.env; set +a; fi  # OPENROUTER_API_KEY: Inspect reads it from the environment
ulimit -n 65536 2>/dev/null || true  # many parallel docker subprocesses
mkdir -p results
exec > >(tee -a "results/$RUN_ID-unattended.log") 2>&1

log() { echo "[$(date -u +%FT%TZ)] $*"; }
bench() { uv run python -m bench "$@"; }
cleanup() { uv run inspect sandbox cleanup docker > /dev/null 2>&1 || true; }

provider_failures() {  # samples of samples.jsonl whose meltzer gave up because the model provider was unreachable
  uv run python - "$RUN_ID" <<'EOF'
import json, sys
from bench import config
from bench.redo import is_provider_failure
run_dir = config.resolve_run_dir(sys.argv[1])
path = run_dir / "samples.jsonl"
rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()] if path.is_file() else []
print(sum(is_provider_failure(run_dir / "samples" / r["sample_id"], r.get("exit_code")) for r in rows))
EOF
}

finish() {  # $1: exit code of the last start or resume; resume until the run finishes
  local status="$1" n=0
  while [ "$status" -ne 0 ] && [ "$n" -lt "$RESUMES" ]; do
    n=$((n + 1))
    log "run stopped (exit $status); resume $n of $RESUMES in $WAIT_SECONDS s"
    sleep "$WAIT_SECONDS"
    cleanup
    bench resume "$RUN_ID"
    status=$?
  done
  return "$status"
}

log "start $RUN_ID"
if [ -f "results/$RUN_ID/manifest.json" ]; then bench resume "$RUN_ID"; else bench run "runs/$RUN_ID.yaml"; fi
if ! finish $?; then log "run did not finish after $RESUMES resumes"; exit 1; fi

for round in $(seq 1 "$REDO_ROUNDS"); do
  count=$(provider_failures)
  log "provider failures: $count"
  [ "$count" -eq 0 ] && break
  log "redo round $round of $REDO_ROUNDS in $WAIT_SECONDS s"
  sleep "$WAIT_SECONDS"
  cleanup
  bench resume "$RUN_ID" --redo-provider-failures
  if ! finish $?; then log "run did not finish after $RESUMES resumes"; exit 1; fi
done
log "finished $RUN_ID; provider failures left: $(provider_failures)"
