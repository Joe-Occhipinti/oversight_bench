#!/usr/bin/env bash
# One hourly check of an unattended run, called by cron:  bash scripts/monitor/monitor_tick.sh <run_id>
#
# Collects the run's state (status.sh) and runs headless Claude Code (Sonnet 5.5), which writes a short report and
# emails it to MAIL_TO through the claude.ai Gmail connector. It may restart a stuck or stopped run with
# restart_run.sh, nothing else. When the run has finished it sends a final report and removes its own cron line.
# Needs Claude Code logged in to the claude.ai account (`claude` then `/login`): the connectors come with that
# login, not with a `claude setup-token` token.
set -uo pipefail
RUN_ID="$1"
MAIL_TO="${MAIL_TO:-occhidipinti00@gmail.com}"
cd "$(dirname "$0")/../.."
export PATH="$HOME/.local/bin:$PATH"
LOG="results/$RUN_ID-unattended.log"

run_finished() { ! tmux has-session -t run 2>/dev/null && tail -n 1 "$LOG" 2>/dev/null | grep -q "finished $RUN_ID"; }

status=$(bash scripts/monitor/status.sh "$RUN_ID" 2>&1)
if run_finished; then kind="FINAL"; else kind="hourly"; fi

prompt="You monitor the oversight_bench experiment run $RUN_ID on this server. It was started by
scripts/run_unattended.sh in tmux session 'run'; that script resumes the run after a stop and at the end runs
again the samples lost to model provider failures. Its log is $LOG; earlier reports are in
results/$RUN_ID-monitor.log. This is the $kind report. The current state is below.

1. You may read files of the repository and of results/ to check details.
2. You may act only when the run is clearly stuck or stopped without finishing: the tmux session is alive but no
   sample has finished and neither log has changed for over 2 hours, or the session is absent and the log does
   not end with 'finished $RUN_ID'. Then run 'bash scripts/monitor/restart_run.sh $RUN_ID' once and say so in the
   report. Do not restart a run that is building images or that finished. Never edit or delete files.
3. Send the report with mcp__claude_ai_Gmail__send_message to $MAIL_TO. Subject: '[oversight_bench] $RUN_ID'
   followed by ' FINAL' for the final report, then ': ' and a headline under 80 characters (state and progress,
   for example 'running, 41/98 samples done, no errors'). Body: at most 15 lines of plain, concise English
   (progress and rate, estimated time left, errors such as provider failures, infra errors and resumes, resource
   use, anything that looks wrong, any action you took), then a line '---- raw state' and the state below as is.
4. Finally print the subject you sent, or 'EMAIL FAILED: <reason>'.

State:
$status"

echo "==== $(date -u +%FT%TZ) $kind"
timeout 1200 claude -p "$prompt" --model claude-sonnet-5-5 --no-session-persistence --max-budget-usd 2 \
  --allowedTools "Read" "Grep" "Glob" "mcp__claude_ai_Gmail__send_message" \
  "Bash(bash scripts/monitor/status.sh:*)" "Bash(bash scripts/monitor/restart_run.sh:*)" \
  "Bash(tail:*)" "Bash(ls:*)" "Bash(docker ps:*)" 2>&1
echo "claude exit code: $?"
if [ "$kind" = FINAL ]; then
  crontab -l 2>/dev/null | grep -v "monitor_tick.sh $RUN_ID" | crontab -
  echo "monitor: run finished; cron line removed"
fi
