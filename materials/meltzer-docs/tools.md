# Tools

The model can call:

- `exec_command` and `write_stdin`: run a command with `bash -c`, and send input
  to one that keeps running between calls. See [interactive processes](exec.md).
  Offered when `features.unified_exec` is true (the default).
- `shell`: run a command with `bash -c`. Optional `workdir`, `background` (start
  and return at once; output goes to a log file under `~/.meltzer/logs/`),
  `escalate` with a `justification` (ask to run outside the sandbox). Offered
  instead of `exec_command` and `write_stdin` when `features.unified_exec` is
  false.
- `apply_patch`: add, update, move or delete files with a patch. A patch is
  applied completely or not at all.
- `update_plan`: keep a checklist of steps for the current task.
- `compact`: replace the conversation with a summary when the context grows long.
- `spawn_agent`: hand a self-contained task to a subagent and get its final
  message, or start it with `background` and use `send_input`, `wait` and
  `close_agent`. See [subagents](agents.md).
- `request_permissions`: ask for network access to one host or write access to
  more paths for the commands that follow. See [permissions](permissions.md).
- `request_user_input`: ask you one to three short questions, each with a few
  options. You pick an option or type your own answer, and the answers go back
  to the model. Only the main session of an interactive run has it.
- `web_search`: search the web through a search service you configure. Only
  registered when set up. See [web search](web-search.md).
- `image_gen`: generate or edit an image. Only registered when an image model
  is set. See [image generation](images.md).
- `get_context_remaining`: report how many tokens are left in the context window.
  See [Context remaining](#context-remaining).

Tools of [MCP servers](mcp.md) are offered as `mcp__<server>__<tool>`.

A turn ends when the model answers without calling a tool, or after
`max_turn_steps` model requests.

## Environment at session start

When a session starts, meltzer adds an environment description to the
conversation: the session directory, approval policy and reviewer, sandbox
mode, network access in the sandbox and the writable directories. It is added
again when one of these changes, for example when a grant from
`request_permissions` starts or ends.

## Commands you run with `!cmd`

In an interactive session, a prompt that starts with `!` runs the rest as a
shell command: `bash -c` in the session directory, as you, outside the sandbox.
meltzer shows the output and the exit code and adds the command and its output
to the conversation, so the model sees them in its next turn. No model turn
starts. `!` alone shows the usage. `meltzer exec` has no `!` commands.

## Context remaining

The `get_context_remaining` tool reports how many tokens are left in the
model's context window: `model_context_window` minus the prompt tokens of the
last model response, as reported by the provider. It is always registered.
When `model_context_window` is not set, or no response has reported token
usage yet, the tool says that the count is unknown.

| Key | Default | Meaning |
|---|---|---|
| `model_context_window` | unset | context window of the model, in tokens |
