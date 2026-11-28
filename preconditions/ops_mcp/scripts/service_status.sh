#!/usr/bin/env bash
# Status of one service: its processes (matched by process name), the ports they listen on
# and its pid file.
#   service_status.sh NAME
set -u
name="${1:?usage: service_status.sh NAME}"
case "$name" in
  [A-Za-z0-9]*) ;;
  *) echo "invalid service name: $name" >&2; exit 2 ;;
esac
case "$name" in
  *[!A-Za-z0-9._-]*) echo "invalid service name: $name" >&2; exit 2 ;;
esac

echo "== processes"
pids=$(pgrep -- "$name" | paste -sd'|' -)
if [ -n "$pids" ]; then
  pgrep -a -- "$name"
else
  echo "(none)"
fi

echo "== listening ports"
if [ -n "$pids" ]; then
  ss -ltnupH 2>/dev/null | grep -E "pid=($pids)," || echo "(none)"
else
  echo "(none)"
fi

echo "== pid file"
found=0
for f in "/var/run/$name.pid" "/run/$name.pid"; do
  if [ -f "$f" ]; then
    echo "$f: $(cat "$f")"
    found=1
  fi
done
[ "$found" -eq 1 ] || echo "(none)"
