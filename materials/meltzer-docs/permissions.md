# Permissions

## request_permissions

The `request_permissions` tool lets the model ask for more sandbox
permissions for the commands and patches that follow, instead of running a
command outside the sandbox. It can ask for:

- network access to one host;
- write access to more files or directories.

Arguments:

| Argument | Meaning |
|---|---|
| `reason` (required) | why the permissions are needed; shown to the reviewer |
| `network` | `{"target": "<host or IP>[:port]"}`: network access to the requested target |
| `write` | list of paths to make writable, absolute or relative to the session directory |

A network target is a single host name (`pypi.org`), an IPv4 address
(`10.0.0.5`) or an IPv6 address (`2001:db8::1`, or `[2001:db8::1]:443` with a
port), with an optional port (`pypi.org:443`). Wildcards, lists of hosts,
URLs and paths are rejected.

Write paths must exist. Symbolic links are resolved when the request is
made: the grant covers the path the link pointed to at that time. Protected
paths (`.git` and `.meltzer` directories, `~/.meltzer`) cannot be requested,
and stay read-only inside a granted directory, as in the session directory.
A directory that contains a `.git` or `.meltzer` entry anywhere below it
cannot be requested either, nor can `/run`, `/dev`, `/proc`, `/sys`, `/tmp` or
a directory that contains one of them; the tool returns an error before the
request is reviewed. A directory that is too large to check, or that cannot
be read, is refused too. Request a narrower directory instead.

### Review

The request goes through `approval_policy` like an escalated command (see
[approvals](approvals.md)):

- `on-request`: the reviewer sees the reason, the network target and the
  resolved write paths. The user reviewer is asked on the terminal; the
  monitor reviews it like any other request.
- `never`: nothing is granted.

### Grants

An approved request becomes a grant of the session. Until the grant ends:

- commands that run in the sandbox, with `shell` or `exec_command`, can write
  to the granted paths and reach the granted network target;
- `apply_patch` writes to the granted paths without asking for approval.

A grant lasts until the end of the turn in which it was approved (with
`meltzer exec`, which runs one turn, until the run ends). A grant approved
for the session lasts until the session ends.

A grant applies to commands started while it holds.

Grants belong to the session that asked for them. A subagent's grants do not
apply to its parent or to other subagents, and a subagent does not receive
its parent's grants.

The tool result lists what was granted and the scope:

```json
{"permissions": {"network": {"target": "pypi.org:443"}, "write": ["/home/me/.cache/pip"]}, "scope": "turn"}
```

A request that is denied, or made with `approval_policy = "never"`, gives an
empty grant:

```json
{"permissions": {}, "scope": "turn"}
```

Each grant is written to the session log as a `permissions_granted` record
with its scope, permissions and reason.

Example: the model asks for `{"reason": "fetch the upstream branch",
"network": {"target": "github.com:443"}}`; once approved, `git fetch upstream`
runs in the sandbox for the rest of the turn.

## Permission profiles

A permission profile is a named set of sandbox settings. Define profiles under
`[permissions.<name>]` and select one with `default_permissions`:

```toml
default_permissions = "dev"

[permissions.dev]
description = "Workspace, the shared library and the pip cache"
extends = ":workspace"

[permissions.dev.workspace_roots]
"~/code/shared-lib" = true

[permissions.dev.filesystem]
"~/.cache/pip" = "write"
"docs" = "read"
"~/.aws" = "deny"

[permissions.dev.network]
enabled = true
```

Built-in profiles:

| Profile | Same as |
|---|---|
| `:read-only` | `sandbox_mode = "read-only"` |
| `:workspace` | `sandbox_mode = "workspace-write"` |
| `:full-access` | `sandbox_mode = "full-access"` |

Keys of a profile:

- `description`: free text.
- `extends`: the profile this one builds on (a built-in or another profile).
  The parent applies first; this profile's entries are added on top, replacing
  parent entries for the same path. Without `extends`, a profile starts from
  `:read-only`.
- `workspace_roots`: more directories that are writable like the session root,
  with `.git` and `.meltzer` inside them read-only. `true` adds a root; `false`
  removes one added by a parent. Applies to profiles based on `:workspace`.
- `filesystem`: paths and their access:
  - `read`: read-only, also inside a writable directory;
  - `write`: writable; `.git` and `.meltzer` inside it, `~/.meltzer` and the
    session root's `.git` and `.meltzer` stay read-only;
  - `deny`: sandboxed commands can neither read nor write it; `apply_patch`
    rejects a patch that names it, with a "cannot read" error.

  Paths are absolute, relative to the session root, or start with `~/`. The
  most specific entry applies to a path. An entry that contains a path with more
  access is an error: a `read` entry cannot contain the session root, a
  workspace root or a `write` entry, and a `deny` entry cannot contain any of
  these or a `read` entry.
- `network.enabled`: `true` lets sandboxed commands reach the network through
  meltzer's network proxy (see [network.md](network.md)); `false` blocks it.
  Without it, the network setting of the base profile applies.

A profile based on `:full-access` runs commands without a sandbox and takes no
`filesystem`, `workspace_roots` or `network` entries.

Selection: `default_permissions` selects the profile. Without it,
`sandbox_mode` applies. A sandbox mode given on the command line (`--sandbox`,
`-c sandbox_mode=...`, `--dangerously-bypass-approvals-and-sandbox`) takes
precedence over `default_permissions`. `[permissions]` tables from the user
config, a trusted project config and the command line are merged by profile
name; a later layer replaces a profile of the same name. Subagents run with
their parent's profile.

A writable or denied path that is a symlink when a command starts stops the
command with a sandbox error. Writable paths that do not exist yet are not
mounted.
