#!/usr/bin/env bash
# Stop and start again the unattended run in tmux session `run` (it resumes, keeping finished samples):
#   bash scripts/monitor/restart_run.sh <run_id>
set -uo pipefail
RUN_ID="$1"
cd "$(dirname "$0")/../.."
export PATH="$HOME/.local/bin:$PATH"
tmux kill-session -t run 2>/dev/null && echo "stopped tmux session run"
sleep 30  # let the stopped process exit
uv run inspect sandbox cleanup docker > /dev/null 2>&1 || true
tmux new-session -d -s run "bash scripts/run_unattended.sh $RUN_ID"
echo "started tmux session run: bash scripts/run_unattended.sh $RUN_ID"
