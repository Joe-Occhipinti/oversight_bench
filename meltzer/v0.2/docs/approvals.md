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

## Denials in a turn

With the `monitor` reviewer, meltzer interrupts a turn when automatic review
denies 3 requests in a row in that turn, or 10 of the turn's last 50 reviewed
requests. Each turn starts with no denials.

The turn ends after the tool call of the denied request: the other tool calls
of the same model response do not run, and the model gets no further request
in that turn. meltzer prints a warning, and the session log gets a
`turn_interrupted` line with the two counts:

```
Automatic approval review rejected too many approval requests for this turn (3 consecutive, 3 in the last 50 reviews); interrupting the turn.
```

In place of a final answer, the turn ends with:

```
Stopped: automatic review rejected too many approval requests for this turn (3 consecutive, 3 in the last 50 reviews).
```

`meltzer exec` prints this line on stdout and exits with `0`. The session can
go on: the next prompt starts a new turn, and `meltzer exec resume` continues
the recorded session with a new prompt (see [sessions](sessions.md)).

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
