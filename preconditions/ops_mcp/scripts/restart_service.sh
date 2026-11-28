#!/usr/bin/env bash
# Restart one service with its init script.
#   restart_service.sh NAME
set -u
name="${1:?usage: restart_service.sh NAME}"
case "$name" in
  [A-Za-z0-9]*) ;;
  *) echo "invalid service name: $name" >&2; exit 2 ;;
esac
case "$name" in
  *[!A-Za-z0-9._-]*) echo "invalid service name: $name" >&2; exit 2 ;;
esac

if [ -f "/etc/init.d/$name" ] && [ -x "/etc/init.d/$name" ]; then
  exec "/etc/init.d/$name" restart
fi
if command -v service >/dev/null 2>&1; then
  exec service "$name" restart
fi
echo "no init script for $name" >&2
exit 1
