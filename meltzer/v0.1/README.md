# meltzer

A terminal coding agent. meltzer sends your request to a model served over an
OpenAI-compatible Chat Completions API, runs the commands the model asks for in a
bubblewrap sandbox, and edits files with patches.

Linux only. Requires Python 3.12+ and [bubblewrap](https://github.com/containers/bubblewrap)
(`bwrap`) on `PATH`.

## Install

```sh
pip install .            # or: pipx install .
export MELTZER_API_KEY=sk-...
```

For development:

```sh
pip install -e '.[dev]'
pytest
```

## Usage

```sh
meltzer                          # interactive session in the current directory
meltzer "explain src/session.py" # interactive, with a first prompt
meltzer exec "fix the failing test" -a never
echo "summarize the README" | meltzer exec -
```

Interactive mode reads one prompt per line. Commands:

| Command | Effect |
|---|---|
| `/plan` | show the task plan; `/plan add`, `edit`, `status`, `remove`, `clear` change it |
| `/exit`, `/quit` | end the session (Ctrl-D works too) |

Ctrl-C cancels the running turn; at the prompt it ends the session.

`meltzer exec` runs one prompt without a terminal and prints the final answer on
stdout; progress goes to stderr. Exit codes: `0` success, `1` model error,
`2` configuration error, `130` interrupted.

Options (both modes):

| Option | Meaning |
|---|---|
| `-C, --cd DIR` | run the session in `DIR` |
| `-m, --model MODEL` | model name |
| `-s, --sandbox MODE` | `read-only`, `workspace-write`, `full-access` |
| `-a, --approval-policy POLICY` | `on-request`, `never` |
| `-c KEY=VALUE` | set any config key; the value is parsed as TOML (repeatable) |
| `--dangerously-bypass-approvals-and-sandbox` | `full-access` and `never` |

## Tools

The model can call:

- `shell`: run a command with `bash -c`. Optional `workdir`, `background` (start
  and return at once; output goes to a log file under `~/.meltzer/logs/`),
  `escalate` with a `justification` (ask to run outside the sandbox).
- `apply_patch`: add, update, move or delete files with a patch. A patch is
  applied completely or not at all.
- `update_plan`: keep a checklist of steps for the current task.
- `compact`: replace the conversation with a summary when the context grows long.
- `spawn_agent`: hand a self-contained task to a subagent and get its final message.

A turn ends when the model answers without calling a tool, or after
`max_turn_steps` model requests.

## Sandbox

Commands run under `bwrap` according to `sandbox_mode`:

| Mode | Read | Write | Network |
|---|---|---|---|
| `read-only` | everywhere | nowhere | no |
| `workspace-write` (default) | everywhere | session directory and `/tmp` (private, see below) | no (escalate to run a command outside the sandbox to reach it) |
| `full-access` | everywhere | everywhere | yes (no sandbox) |

In `workspace-write`, `.git`, `.meltzer` and `~/.meltzer` stay read-only.
meltzer creates `<session dir>/.meltzer` at startup if it is missing.

In the sandbox, `/tmp` is a directory of the session's own, created as
`meltzer-<session id>` in the system temporary directory (mode 0700) and
removed when the session ends. The sandboxed commands of a session share it;
the host `/tmp` is not visible to them. Commands that run outside the sandbox
use the real `/tmp`.

Sandboxed commands also see an empty `/run` (and `/var/run`), so sockets of
host services there cannot be reached, and they get their own IPC namespace.
If the sandbox cannot be set up, the command is not run.

## Approvals

When the model sets `escalate` on a command, or a patch writes outside the
writable paths (the session directory and the session's temporary directory;
a patch to the host `/tmp` counts as outside), the request goes through
`approval_policy`:

- `on-request` (default): a reviewer decides. An approved command runs
  outside the sandbox.
- `never`: the request is rejected.

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

Settings are read when a session starts, from these layers (later wins):

1. built-in defaults
2. user config: `~/.meltzer/config.toml`
3. project config: `.meltzer/config.toml` in the project.
   Used only in a [trusted project](#project-trust).
4. command line (`-c` and flags; flags win over `-c`)

| Key | Default | Description |
|---|---|---|
| `model` | `gpt-5.1` | model name |
| `model_provider.base_url` | `https://api.openai.com/v1` | Chat Completions endpoint |
| `model_provider.api_key_env` | `MELTZER_API_KEY` | environment variable holding the API key |
| `sandbox_mode` | `workspace-write` | see [Sandbox](#sandbox) |
| `approval_policy` | `on-request` | see [Approvals](#approvals) |
| `approvals_reviewer` | `user` | `user` or `monitor` |
| `monitor.model` | session model | model for monitor reviews |
| `monitor.transcript_items` | `40` | transcript length of monitor reviews |
| `max_turn_steps` | `100` | model requests per turn |
| `notify` | none | program to run when a turn completes |
| `hooks.<Event>` | none | see [Hooks](#hooks) |

Example `~/.meltzer/config.toml`:

```toml
model = "gpt-5.1"
approvals_reviewer = "monitor"
notify = ["python3", "/home/me/.meltzer/notify.py"]

[model_provider]
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"
```

### Project trust

The project config and project rules are loaded only when the session
directory is trusted. Trust decisions are stored in the user config:

```toml
[projects."/home/me/src/app"]
trust_level = "trusted"          # or "untrusted"
```

The first interactive session in a directory with no decision asks:

```
Do you trust the files in /home/me/src/app?
Trust this directory? [y]es / [n]o:
```

The answer is saved in `~/.meltzer/config.toml`. `meltzer exec` never asks: in a
directory that is not trusted it runs without the project config and project
rules, and prints a one-line notice on stderr.

## Rules

Prefix rules let matching commands run outside the sandbox, or reject them.
They are read from `~/.meltzer/rules/*.rules` and, in a trusted project, from
the project's `.meltzer/rules/` directory:

```python
prefix_rule(pattern=["cargo", "check"], decision="allow")
prefix_rule(pattern=["git", ["status", "diff", "log"]])
prefix_rule(pattern=["rm", "-rf"], decision="forbidden", justification="Use trash instead.")
```

`pattern` lists the first tokens of a command; a nested list gives alternatives
for one position. `decision` is `allow` (default) or `forbidden`. Allow rules
that are too broad are rejected with a warning. "Always allow" answers are
appended to `~/.meltzer/rules/default.rules`.

## Hooks

Hooks run programs on session events: `SessionStart`, `PreToolUse`,
`PostToolUse`, `Stop`. `SessionStart` and `Stop` fire for the main session
only, not for subagents.

```toml
[[hooks.PreToolUse]]
matcher = "shell"                      # tool name, "a|b", or "*" (default)
command = ["python3", ".meltzer/hooks/check.py"]
cwd = "."                              # relative to the session directory
```

Hooks can be set in any config layer.

Each hook gets a JSON description of the event on stdin. A `PreToolUse` hook
that exits with status 2 blocks the tool call; its stderr is returned to the
model as the reason (`blocked by <command>` if stderr is empty).

A hook runs only after you trust it. Interactive sessions ask about new hooks at
startup; trusted hooks are stored in `~/.meltzer/trusted_hooks.toml`.
`meltzer exec` skips untrusted hooks.

## Notify

`notify` runs a program after each turn of the main session, with one JSON
argument:

```json
{"type": "agent-turn-complete", "thread-id": "...", "turn-id": "...", "cwd": "...",
 "client": "meltzer", "input-messages": ["..."], "last-assistant-message": "..."}
```

## AGENTS.md

If the session directory contains `AGENTS.md`, its content is added at the start
of the conversation as project instructions (up to 32 KiB).

## Files

| Path | Content |
|---|---|
| `~/.meltzer/config.toml` | user config, project trust decisions |
| `~/.meltzer/rules/` | user prefix rules |
| `~/.meltzer/trusted_hooks.toml` | trusted hooks |
| `~/.meltzer/sessions/YYYY/MM/DD/*.jsonl` | session logs |
| `~/.meltzer/logs/<session>/` | output of background commands |
| `<system temp dir>/meltzer-<session>/` | `/tmp` of sandboxed commands, removed at session end |
| `<project>/.meltzer/` | project config and rules (used in trusted projects) |

## License

Apache-2.0
