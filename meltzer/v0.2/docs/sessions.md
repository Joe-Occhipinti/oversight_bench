# Resume, fork and rollback

Every session is recorded in `~/.meltzer/sessions/YYYY/MM/DD/` as a JSON lines
file named `session-<start time>-<session id>.jsonl`. A recorded session can be
continued (`resume`), copied into a new session (`fork`), and an interactive
session can remove its last turns (`/rollback`).

## Commands

| Command | What it does |
|---|---|
| `meltzer resume ID [prompt]` | continue session `ID` interactively |
| `meltzer resume --last [prompt]` | continue the most recent session |
| `meltzer fork ID [prompt]`, `meltzer fork --last [prompt]` | start a new interactive session from a copy of the conversation |
| `meltzer exec resume ID <prompt>`, `meltzer exec resume --last <prompt>` | continue a session with one prompt, without a terminal, as [`meltzer exec`](exec-mode.md) |

`ID` is the session id (a UUID), as in the file name and in the `id` field of
the file's first line. `--last` takes the session recorded in the current
directory (`-C` sets it) that was written to most recently; sessions of subagents
are not counted. Sessions are looked up only in
`~/.meltzer/sessions`. A subagent's session cannot be resumed or forked on its
own; resume or fork its parent session.

The other options are the same as for `meltzer` and `meltzer exec`: `-C`,
`-m`, `-s`, `-a`, `-c` and `--dangerously-bypass-approvals-and-sandbox`.

```
meltzer resume --last
meltzer resume 3f2b8c1e-5d0a-4c7e-9a61-0b9e2d4f7a10 "now add tests for it"
meltzer fork --last -m gpt-5.1
meltzer exec resume --last "run the tests again and fix what fails"
```

## What a resumed session keeps

The conversation is loaded from the file: user prompts, the model's messages
and reasoning, tool calls and their results, hook prompts, subagent and agent
messages, `!` commands, and the summary of a compaction (the conversation
before a compaction is not loaded, as in the running session). Turns removed
with `/rollback` stay removed. A tool call that has no result in the file (the
session ended while it ran) gets a result saying so.

Everything else comes from the current invocation, as for a new session:

- Settings: model, sandbox mode, approval policy and reviewer, permission
  profile, rules, hooks, MCP servers, skills and plugins, from the current
  config files and command line. The settings recorded in the session file are
  not used.
- Context: the session adds `AGENTS.md` and the environment description again
  from the current files and settings. The `AGENTS.md`, environment, skill,
  hook context, memory and plan items of the recorded session are not loaded.
  The task plan starts empty.
- Workspace: the directory of the current invocation (`-C` or the current
  directory). If the session was recorded in another directory, meltzer prints
  a note and continues in the current one.
- Grants from [`request_permissions`](permissions.md) start empty.

`SessionStart` [hooks](hooks.md) run as for a new session.

A resumed session keeps its id and appends to the same file. It first writes a
`resumed` line with its working directory and settings (`config`,
`config_sources`), in the same form as the `session_meta` line.

A forked session gets a new id and a new file. Its `session_meta` line has
`forked_from_id` with the id of the session it was copied from, followed by
the copied conversation. The original file is not changed.

## /rollback

In an interactive session, `/rollback N` removes the last `N` user turns from
the conversation: the `N`th-from-last prompt you typed and everything after it.
The model does not see those turns in later requests, and they are not loaded
when the session is resumed.

```
> /rollback 1
rolled back 1 turn (6 items); files changed in those turns are not restored
```

- `/rollback` is a command you type at the prompt, between turns. The model has
  no tool for it, and `meltzer exec` has no rollback.
- Files changed in the removed turns stay as they are. Use your version control
  to undo them.
- Rules saved with "always allow" and session grants stay in place.
- The task plan keeps its steps.
- The session file keeps the removed turns, followed by a `rolled_back` line
  with `num_turns`.

## Session file lines

| `type` | Written when |
|---|---|
| `session_meta` | the session starts (first line) |
| `item` | an item is added to the conversation |
| `item_updated`, `item_removed` | the plan item changes |
| `compacted` | the conversation is compacted; holds the items that replace it |
| `rolled_back` | `/rollback N` |
| `resumed` | the session is resumed |
| `review`, `hook_output`, ... | approvals, hooks and other events; not needed to load the conversation |
