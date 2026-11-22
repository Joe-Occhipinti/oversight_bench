# Approvals

When the model sets `escalate` on a command, or a patch writes outside the
writable paths (the session directory and the session's temporary directory,
and directories added by a [permission profile](permissions.md) or a grant; a
patch to the host `/tmp` counts as outside), the request goes through
`approval_policy`:

- `on-request` (default): a reviewer decides. An approved command runs
  outside the sandbox.
- `never`: the request is rejected.

[Rules](rules.md) apply to commands before this: a matching rule allows,
reviews or rejects the command. MCP tool calls, `request_permissions`, network
access and web searches go through the same policy and reviewers; see
[MCP](mcp.md), [permissions](permissions.md), [network](network.md) and
[web search](web-search.md). `PermissionRequest` [hooks](hooks.md) run before the
reviewer.

## Reviewers

`approvals_reviewer` selects the reviewer:

- `user` (default): meltzer asks on the terminal: approve once, always allow
  commands starting with the same prefix (saved as a rule), or deny. For a
  patch it shows the paths outside the writable paths and the patch text.
  Needs an interactive session.
- `monitor`: a separate model conversation reviews each request with read-only
  tools (`read_file`, `list_dir`, `grep`) and answers allow or deny. The
  model is `monitor.model`, or the session model if unset. Reviews are written to
  the session log.

`meltzer exec` needs `approvals_reviewer = "monitor"` or `approval_policy = "never"`.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `approval_policy` | `on-request` | `on-request` or `never` |
| `approvals_reviewer` | `user` | `user` or `monitor` |
| `monitor.model` | session model | model for monitor reviews |
| `monitor.transcript_items` | `40` | transcript length of monitor reviews |
| `monitor.environment` | unset | description of this machine for monitor reviews (user config only) |
| `monitor.trusted_hosts` | `[]` | hosts monitor reviews treat as trusted services of the user's organization (user config only) |

All keys: [config.md](config.md).
