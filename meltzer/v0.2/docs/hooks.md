# Hooks

Hooks run programs from your configuration on session and tool events. A hook
can add context to the conversation, block a tool call or a prompt, deny an
approval request, replace a tool result, or continue a turn.

## Events

| Event | When it runs |
|---|---|
| `SessionStart` | once, when the main session has started |
| `SessionEnd` | once, when the main session ends |
| `UserPromptSubmit` | before a prompt is added to the conversation |
| `PreToolUse` | before each tool call |
| `PermissionRequest` | before a request goes to the reviewer (see [approvals](approvals.md)) |
| `PostToolUse` | after each tool call, with the tool's output |
| `PreCompact` | before the conversation is compacted |
| `PostCompact` | after the conversation is compacted |
| `SubagentStart` | once, when a subagent session has started (see [agents](agents.md)) |
| `SubagentStop` | when a subagent has finished a turn |
| `Stop` | when the model has finished a turn of the main session |

`SessionStart`, `SessionEnd` and `Stop` run for the main session only; a
subagent runs `SubagentStart` and `SubagentStop` instead. The other events run
in every session.

## Configuration

```toml
[[hooks.PreToolUse]]
matcher = "shell"                      # tool name, "a|b", or "*" (default)
command = ["python3", ".meltzer/hooks/check.py"]
cwd = "."                              # relative to the session directory

[[hooks.SessionStart]]
command = ["git", "status", "--short", "--branch"]
```

| Key ([all keys](config.md)) | Meaning |
|---|---|
| `command` | the program and its arguments (required) |
| `cwd` | working directory, relative to the session directory (default `.`) |
| `matcher` | which calls the hook runs for (default `*`, all) |

`matcher` applies to `PreToolUse` and `PostToolUse` (the tool name),
`PermissionRequest` (the request type: `shell`, `exec_command`, `apply_patch`,
`mcp_tool_call`, `request_permissions`, `network_access` or `web_search`), and
`SubagentStart` and `SubagentStop` (the agent type, such as `explorer`). Other
events ignore it.

Hooks can be set in any config layer. The hooks of an event run one after
another, in configuration order, with a timeout of 60 seconds; a hook that
times out is stopped with the processes it started. Each hook gets the environment of meltzer plus `MELTZER_PROJECT_DIR`
(the session directory) and `MELTZER_SESSION_ID`.

## Trust

A hook runs only after you trust it. Interactive sessions ask about new hooks at
startup; trusted hooks are stored in `~/.meltzer/trusted_hooks.toml`.
`meltzer exec` skips untrusted hooks.

## Input

Each hook gets a JSON object on stdin:

```json
{"session_id": "...", "transcript_path": "~/.meltzer/sessions/.../session-....jsonl",
 "cwd": "/path/to/project", "hook_event_name": "PreToolUse",
 "tool_name": "shell", "tool_input": {"command": "pytest -q"}}
```

In a subagent session the object also has `agent_id` and `agent_type`.

| Event | Extra fields |
|---|---|
| `SessionStart` | `source`: `startup`, or `resume` in a resumed or forked session |
| `SessionEnd` | `reason`: `exit` |
| `UserPromptSubmit` | `prompt` |
| `PreToolUse` | `tool_name`, `tool_input` (the call's arguments) |
| `PermissionRequest` | `tool_name` (the request type), `tool_input` (the request: command, cwd, justification, paths, patch, server and arguments, ...) |
| `PostToolUse` | `tool_name`, `tool_input`, `tool_response`: `{"output": ..., "success": ...}` |
| `PreCompact`, `PostCompact` | `trigger`: `manual` |
| `Stop` | `last_assistant_message`, `stop_hook_active` |
| `SubagentStop` | `last_assistant_message`, `stop_hook_active`, `agent_transcript_path` |

## Output

A hook answers with its exit status and output.

- **Exit status 0.** Stdout may hold one JSON object (below). Empty stdout has
  no effect. For `SessionStart`, `SubagentStart` and `UserPromptSubmit`, plain
  text on stdout is added to the conversation as context.
- **Exit status 2.** For `PreToolUse`, `PermissionRequest`, `PostToolUse`,
  `UserPromptSubmit`, `Stop` and `SubagentStop`, the hook blocks and stderr is
  the reason. A `PreToolUse` hook blocks even with empty stderr (the reason is
  then `blocked by <command>`); the other events need a reason on stderr.
- **Anything else** (another exit status, a timeout, JSON that is not an
  object, a field of the wrong type, a field the event does not support): the
  hook has no effect and meltzer shows a notice.

### JSON fields

Every event accepts:

| Field | Meaning |
|---|---|
| `continue` | `false` stops (see the table below) |
| `stopReason` | text shown with a stop |
| `systemMessage` | a notice shown to you, not to the model |
| `suppressOutput` | accepted and ignored |

Event fields go in `hookSpecificOutput` (which may name the event in
`hookEventName`); `decision` and `reason` go at the top level.

| Event | Fields |
|---|---|
| `PreToolUse` | `hookSpecificOutput.permissionDecision`: `"deny"` with `permissionDecisionReason` blocks the call; `"allow"` with `updatedInput` (an object) replaces the call's arguments. `decision: "block"` with `reason` also blocks. `hookSpecificOutput.additionalContext`. `continue`, `stopReason`, `suppressOutput` and `"ask"` are not supported. |
| `PermissionRequest` | `hookSpecificOutput.decision`: `{"behavior": "deny", "message": "..."}` denies the request; `{"behavior": "allow"}` leaves the decision to the reviewer. `updatedInput`, `updatedPermissions`, `interrupt`, `continue`, `stopReason` and `suppressOutput` are not supported. |
| `PostToolUse` | `decision: "block"` with `reason`: the model gets `reason` as an error instead of the result. `continue: false`: the model gets `reason` (or `stopReason`) instead of the result. `hookSpecificOutput.additionalContext`. |
| `UserPromptSubmit` | `decision: "block"` with `reason`, or `continue: false`: the prompt is not added and the turn does not start. `hookSpecificOutput.additionalContext`. |
| `Stop`, `SubagentStop` | `decision: "block"` with `reason`: the turn continues, with `reason` as the next prompt. `continue: false` ends the turn. |
| `PreCompact` | `continue: false`: the conversation is not compacted and the turn ends. |
| `PostCompact` | `continue: false`: the turn ends. |
| `SessionStart`, `SubagentStart` | `hookSpecificOutput.additionalContext` |

Example: a `PreToolUse` hook that blocks `rm -rf`:

```python
import json, sys

event = json.load(sys.stdin)
if event["tool_name"] == "shell" and "rm -rf" in event["tool_input"].get("command", ""):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "use trash instead of rm -rf",
    }}))
```

### Several hooks

- `PreToolUse`: the first hook that blocks stops the hooks after it. When
  several hooks replace the arguments, the last one wins.
- `PermissionRequest`: the first hook that denies stops the hooks after it; a
  deny wins over an allow.
- Other events: all matching hooks run. Context from each hook is added in
  order; the reasons of all hooks that block are joined.

## What hooks add to the conversation

- Context (`additionalContext`, or plain text from `SessionStart`,
  `SubagentStart` and `UserPromptSubmit`) is added as a hook context item,
  after the prompt for `UserPromptSubmit` and after the results of the current
  tool calls for `PreToolUse` and `PostToolUse`.
- A `Stop` or `SubagentStop` block adds the reason as a hook prompt and the
  model runs again in the same turn. The hooks then run once more with
  `stop_hook_active: true`; a second block does not continue the turn again.

Text a hook adds to the conversation (context or a reason) longer than 10,000 characters is cut to its start and end, with
a note; the full text is written to the session log as a `hook_output` event.

## Permission requests

A `PermissionRequest` hook runs when a request needs review, before the
reviewer set by `approvals_reviewer` is asked. A hook that denies rejects the
request; the model gets the hook's reason. Any other answer, including
`allow`, leaves the decision to the reviewer. With
`approval_policy = "never"`, requests are rejected before any hook runs.
