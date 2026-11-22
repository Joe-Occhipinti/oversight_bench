# Interactive processes

The `exec_command` and `write_stdin` tools let the model run commands that keep
running between tool calls: a dev server, a test watcher, a REPL, a database
shell, or a program that asks questions on the terminal.

## exec_command

`exec_command` starts a command with `bash -c` and waits for its output for the
yield time. If the command exits within that time, the result has its exit code
and output. If it is still running, the result has a session ID; the process
keeps running and the model continues it with `write_stdin`.

| Argument | Meaning |
|---|---|
| `cmd` | the command (required) |
| `workdir` | working directory, absolute or relative to the workspace root (default: the root) |
| `tty` | `true`: the process gets a terminal (PTY) and can read input. `false` (default): output through pipes, no input |
| `yield_time_ms` | how long to wait for output before returning (default 10,000; between 250 and 30,000) |
| `max_output_tokens` | longest output returned, in tokens of about 4 bytes (default 10,000) |
| `escalate`, `justification` | ask to run outside the sandbox, as for `shell` |

A command started with `exec_command` is decided in the same way as a `shell`
command: [rules](rules.md) first, then the sandbox, and an escalation goes
through the [approval policy and the reviewer](approvals.md). Sandboxed
processes run in the same [sandbox](sandbox.md) as `shell` commands. If the
sandbox cannot be set up, the command is not run.

## write_stdin

`write_stdin` sends characters to a running session and returns the output
written since the last call.

| Argument | Meaning |
|---|---|
| `session_id` | the session ID returned by `exec_command` (required) |
| `chars` | characters to write; empty to wait for more output without writing |
| `yield_time_ms` | how long to wait for output (default 250; between 250 and 30,000; for an empty `chars`, between 5,000 and `background_terminal_max_timeout`) |
| `max_output_tokens` | as for `exec_command` |

Input needs a session started with `tty = true`; a session without a terminal
accepts only empty `chars` and Ctrl-C. `"\u0003"` (Ctrl-C) sends SIGINT to the
command's process group, in both kinds of sessions.

A call returns early when the process exits. The result then has the exit code,
and the session ID is no longer valid.

A session ID belongs to the agent that started the process. Another agent
(the parent, or a subagent) that uses it gets the same "Unknown process id"
error as for an ID that does not exist.

## Output

Both tools return:

```
Chunk ID: 3f9a1c
Wall time: 1.2 seconds
Process running with session ID 48213
Original token count: 37
Output:
Serving on http://127.0.0.1:8000
```

or `Process exited with code N` once the process has exited. Output longer than
`max_output_tokens` is shortened in the middle. Between two calls a session
keeps up to 1 MiB of output, the first and the last half; output in between is
replaced by a `[... N bytes omitted ...]` line.

## Environment

Interactive processes get `TERM=dumb`, `NO_COLOR=1`, `LANG` and `LC_ALL` set
to `C.UTF-8`, and `PAGER`, `GIT_PAGER` and `GH_PAGER` set to `cat`, in addition
to meltzer's environment.

## Limits and lifetime

- A session (with its subagents) keeps up to 64 processes. When a new process
  would exceed that, an older one is stopped: an exited process first,
  otherwise the least recently used one. The 8 most recently used processes are
  never stopped this way.
- Processes keep running across turns.
- When a session ends, meltzer stops the processes that session started, with
  their process groups (SIGTERM, then SIGKILL after 2 seconds). A subagent's
  processes stop when the subagent is closed; the top-level session stops every
  remaining process when it ends.

## Configuration

| Key ([all keys](config.md)) | Default | Meaning |
|---|---|---|
| `background_terminal_max_timeout` | `300000` | longest wait, in milliseconds, of a `write_stdin` call with empty `chars` |

```toml
background_terminal_max_timeout = 600000
```
