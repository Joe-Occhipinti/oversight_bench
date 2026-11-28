#!/usr/bin/env bash
# Print the state of an unattended run (read only):  bash scripts/monitor/status.sh <run_id>
set -uo pipefail
RUN_ID="$1"
cd "$(dirname "$0")/../.."
RUN_DIR="results/$RUN_ID"
LOG="results/$RUN_ID-unattended.log"
now=$(date +%s)
minutes_since() { echo $(( (now - $(stat -c %Y "$1")) / 60 )); }
# sample folders (docs/spec-exp3.md 10.2): tier<k>/search/wave<w>/exp<i>/, tier<k>/execution/<pair>/, ceiling<k>/execution/<pair>/
sample_files() { ls "$RUN_DIR"/*/search/wave*/exp*/"$1" "$RUN_DIR"/*/execution/*/"$1" 2>/dev/null; }

echo "time (UTC): $(date -u +%FT%TZ)"
echo "tmux session 'run': $(tmux has-session -t run 2>/dev/null && echo alive || echo absent)"

# expected samples: tiers x waves x per_wave (search) + tiers x (1 + ceilings) x test_n x side tasks (execution)
expected=$(python3 - "$RUN_DIR/config.yaml" <<'EOF' 2>/dev/null
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
def number(key):
    return int(re.search(rf"\b{key}:\s*(\d+)", text).group(1))
tiers = len(re.search(r"^tiers:\s*\[([^\]]*)\]", text, re.M).group(1).split(","))
sides = len(re.search(r"side_tasks:\s*\[([^\]]*)\]", text).group(1).split(","))
ceilings = re.search(r"ceilings:\s*(true|false)", text).group(1) == "true"
search, execution = tiers * number("waves") * number("per_wave"), tiers * (1 + ceilings) * number("test_n") * sides
print(f"{search + execution} ({search} search, {execution} execution)")
EOF
)
finished=$(sample_files exit_code | wc -l)
echo "samples finished (exit_code written): $finished of ${expected:-?}"
echo "  search: $(ls "$RUN_DIR"/*/search/wave*/exp*/exit_code 2>/dev/null | wc -l), execution: $(ls "$RUN_DIR"/*/execution/*/exit_code 2>/dev/null | wc -l)"
latest=$(sample_files exit_code | xargs -r ls -t 2>/dev/null | head -1)
[ -n "$latest" ] && echo "minutes since the last sample finished: $(minutes_since "$latest")"
echo "strategies written: $(ls "$RUN_DIR"/tier*/strategy.md 2>/dev/null | wc -l)"
echo "sample stacks running (default containers): $(docker ps --format '{{.Names}}' 2>/dev/null | grep -c -- '-default-')"
echo "samples that hit a model provider failure: $(sample_files stderr.txt | xargs -r grep -l 'request to model provider failed' 2>/dev/null | wc -l)"
if [ -f "$RUN_DIR/manifest.json" ]; then
  echo "manifest: $(python3 -c "import json,sys; m=json.load(open(sys.argv[1])); print('status', m.get('status'), '| start', m.get('start_time'), '| end', m.get('end_time'), '| resumes', len(m.get('resumes', [])))" "$RUN_DIR/manifest.json")"
fi
[ -f "$RUN_DIR/samples.jsonl" ] && echo "samples.jsonl rows: $(wc -l < "$RUN_DIR/samples.jsonl")"
eval_log=$(ls -t "$RUN_DIR"/inspect_logs/*/*.eval 2>/dev/null | head -1)
[ -n "$eval_log" ] && echo "minutes since the Inspect log changed: $(minutes_since "$eval_log") ($(basename "$(dirname "$eval_log")"))"
echo
echo "load: $(cut -d' ' -f1-3 /proc/loadavg) | memory used/total GB: $(free -g | awk '/Mem:/ {print $3 "/" $2}')"
echo "disk free for Docker: $(df -h --output=avail "$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /)" | tail -1 | tr -d ' ')"
echo
if [ -f "$LOG" ]; then
  echo "minutes since the unattended log changed: $(minutes_since "$LOG")"
  echo "---- last 30 lines of $LOG"
  tail -n 30 "$LOG"
else
  echo "no $LOG yet"
fi
