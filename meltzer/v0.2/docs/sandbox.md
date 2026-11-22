# Sandbox

Commands of the `shell` and `exec_command` tools run under `bwrap` according to
`sandbox_mode`:

| Mode | Read | Write | Network |
|---|---|---|---|
| `read-only` | everywhere | nowhere | no |
| `workspace-write` (default) | everywhere | session directory and `/tmp` (private, see below) | no by default; through the [network proxy](network.md) when `network.enabled` is set; escalate to run a command outside the sandbox to reach it |
| `full-access` | everywhere | everywhere | yes (no sandbox) |

A [permission profile](permissions.md) can add writable directories, read-only
and denied paths, and network access to a sandboxed mode. A grant from
`request_permissions` widens the sandbox for the commands that follow.

## Writable paths

In `workspace-write` the writable paths are the session directory and the
session's temporary directory (`/tmp` in the sandbox), plus any directories a
permission profile or a grant adds.

## Protected paths

In `workspace-write`, `.git`, `.meltzer` and `~/.meltzer` stay read-only, also
inside directories added by a profile or a grant. meltzer creates
`<session dir>/.meltzer` at startup if it is missing.

## Private /tmp

In the sandbox, `/tmp` is a directory of the session's own, created as
`meltzer-<session id>-<random>` in the system temporary directory (mode 0700) and
removed when the session ends. The sandboxed commands of a session share it;
the host `/tmp` is not visible to them. Commands that run outside the sandbox
use the real `/tmp`.

Sandboxed commands also see an empty `/run` (and `/var/run`), so sockets of
host services there cannot be reached, and they get their own IPC namespace.
If the sandbox cannot be set up, the command is not run.

## Other platforms

On macOS, sandboxed commands run under Seatbelt (`/usr/bin/sandbox-exec`) with
the same filesystem rules: reads everywhere except the denied paths, writes
only in the writable roots minus their protected and read-only paths, and
network only in mode `full`. Network mode `proxy` needs the Linux sandbox; on
macOS such commands are not run. On other platforms, Windows included,
sandboxed commands are not run; `full-access` runs commands without a sandbox.
