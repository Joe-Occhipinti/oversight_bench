# Subagents

The model can hand parts of a task to subagents. A subagent is a new session
in the same process: it works in the same directory, with the same
configuration, sandbox, approval settings and rules as the session that
started it, but with a conversation of its own. It does not see its parent's
conversation.

## Tools

- `spawn_agent(prompt, agent_type, background)`: start a subagent with the task
  in `prompt`. By default the call waits and returns the subagent's final
  message, and the subagent is closed. With `background = true` it returns the
  subagent's id at once, and the subagent works while the parent goes on.
  `agent_type` selects a [role](#roles) (default: `default`).
- `send_input(target, message)`: send another message to a subagent started in
  the background. A subagent that is working handles it after its current
  task; a finished one starts on it at once.
- `wait(targets, timeout_ms)`: wait until at least one of the subagents in
  `targets` is completed, failed or closed, and return their statuses with the
  final message of each completed one. `timeout_ms` defaults to 30 seconds. It
  must be positive, and is then kept between 10 seconds and 1 hour; when it
  passes, the status is empty and `timed_out` is true.
- `close_agent(target)`: close a subagent, and the subagents it started, and
  return its status before it was closed. A subagent that is working stops at
  its next model request.

A session can address only the subagents it started, by the id `spawn_agent`
returned. A finished background subagent stays open, so it can receive more
input, until it is closed; all open subagents are closed when the top-level
session ends.

Example `wait` result:

```json
{"status": {"5f0c...": {"status": "completed", "final_message": "All 12 tests pass."}}, "timed_out": false}
```

## Limits

- `agents.max_depth` (default 1): how deep subagents can be nested. The
  top-level session is depth 0, so by default subagents cannot start
  subagents of their own. Sessions at the limit do not have the subagent tools.
- `agents.max_threads` (default 6): how many subagents can be open at once in
  the whole session tree. `spawn_agent` fails at the limit; close a subagent to
  start another.

```toml
[agents]
max_depth = 2
max_threads = 4
default_model = "openai/gpt-5-mini"   # model for subagents without a role model
```

## Roles

A role is a preset for subagents, chosen with `agent_type`. Roles are read when
the session starts from:

- `~/.meltzer/agents/*.toml`
- in a [trusted project](trust.md), the project's `.meltzer/agents/*.toml`

```toml
# ~/.meltzer/agents/reviewer.toml
name = "reviewer"                         # default: the file name without .toml
description = "Reviews a diff and lists the bugs it finds."
developer_instructions = """
Read the diff you are given and the code around it.
List each bug with the file, the line and a short explanation.
"""
model = "openai/gpt-5-mini"               # optional
```

| Key | Meaning |
|---|---|
| `name` | the name used in `agent_type` |
| `description` | shown to the model in the `spawn_agent` tool, so it can choose a role |
| `developer_instructions` | added to the subagent's system message under `# Agent role: <name>` (required) |
| `model` | the subagent's model; default `agents.default_model`, else the parent's model |

Other keys are ignored with a warning. A file that is not valid TOML, or has
no `developer_instructions`, is skipped with a warning. A project role replaces
a user role of the same name, and both replace built-in roles.

Built-in roles:

- `default`: no extra instructions.
- `explorer`: for specific questions about the codebase.
- `worker`: for implementation work, such as part of a feature or a bug fix.

A role changes only the subagent's instructions and model. Sandbox, approval,
[permission](permissions.md) and monitor settings come from the parent session.

## Approvals and logs

Approval requests of a subagent go to the reviewer set by `approvals_reviewer`,
as in its parent (see [approvals](approvals.md)). The monitor uses the parent's
`monitor.model` (or the parent's model if unset), also for subagents whose role
sets another model. With the `user` reviewer, prompts from subagents running in
the background are shown one at a time.

Each subagent has its own session log under `~/.meltzer/sessions/`, with
`parent_session_id` and `agent_role` in its first line.
