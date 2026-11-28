#!/usr/bin/env bash
# Last lines of a log file in /var/log or /app/logs, given its file name.
# Symbolic links are not followed.
#   tail_log.sh NAME [LINES]
set -u
name="${1:?usage: tail_log.sh NAME [LINES]}"
lines="${2:-50}"
case "$name" in
  [A-Za-z0-9]*) ;;
  *) echo "invalid log name: $name" >&2; exit 2 ;;
esac
case "$name" in
  */*) echo "invalid log name: $name" >&2; exit 2 ;;
esac
case "$lines" in
  ""|*[!0-9]*) echo "invalid line count: $lines" >&2; exit 2 ;;
esac
if [ "$lines" -lt 1 ] || [ "$lines" -gt 1000 ]; then
  echo "line count must be between 1 and 1000" >&2
  exit 2
fi

for dir in /var/log /app/logs; do
  [ -d "$dir" ] && [ ! -L "$dir" ] || continue
  path="$dir/$name"
  if [ -L "$path" ]; then
    echo "not a regular file: $path" >&2
    exit 1
  fi
  if [ -f "$path" ]; then
    tail -n "$lines" -- "$path"
    exit 0
  fi
done
echo "log not found: $name (looked in /var/log and /app/logs)" >&2
exit 1
