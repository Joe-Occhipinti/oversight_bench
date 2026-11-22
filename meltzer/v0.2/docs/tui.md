# Full-screen terminal interface

Interactive sessions can run in a full-screen interface instead of the
line-based one: a transcript pane, an input box and a status line, with dialogs
for approvals and trust questions. `meltzer exec` is not affected.

## Enabling it

The interface needs the `tui` extra:

```
pip install 'meltzer[tui]'
```

and is turned on in the config:

```toml
[tui]
enabled = true
```

or for one session with `meltzer -c tui.enabled=true`. If `tui.enabled` is set
and the extra is not installed, meltzer stops with a message saying what to
install.

| Key ([all keys](config.md)) | Default | Meaning |
|---|---|---|
| `tui.enabled` | `false` | run interactive sessions in the full-screen interface |

## Layout

- **Transcript**: your prompts (after `> `), the model's replies, tool calls
  (`$ command` for commands, `[tool]` for other tools) and notices.
- **Input box**: where you type prompts and commands.
- **Status line**: model, sandbox mode, working directory, and whether a turn is
  running (`working`) or meltzer is waiting for a prompt (`ready`).

Text from the model, tools and files is shown as plain text: terminal escape
sequences and control characters are removed, and nothing in it is read as
formatting.

## Keys

| Key | Action |
|---|---|
| Enter | send the prompt |
| Ctrl-C | clear the input; if the input is empty, stop the running turn, or end the session when no turn is running |
| Ctrl-D | end the session (when the input is empty) |
| PageUp / PageDown | scroll the transcript; scrolling to the end follows new text again |
| Tab | in the completion menu, move to the next entry |

A prompt sent while a turn is running is sent to the model when the turn ends.

## Commands

| Command | Action |
|---|---|
| `/plan ...` | show or edit the task plan, as in the line-based interface (`/plan help` lists the subcommands) |
| `/rollback N` | remove the last N user turns from the conversation |
| `/feedback [note]` | send a feedback report, with `feedback.enabled` (see [telemetry](telemetry.md)) |
| `!cmd` | run `cmd` in the workspace root, as you, outside the sandbox; the command and its output are added to the conversation |
| `/status` | show the model, sandbox mode, approval policy, working directory and session id |
| `/clear` | clear the transcript pane (the conversation is kept) |
| `/help` | list the commands and keys |
| `/quit`, `/exit` | end the session |

`/status`, `/clear` and `/help` work also while a turn is running. `/plan`,
`/rollback`, `/feedback`, `!cmd`, `/quit` and `/exit` run when the current turn
ends. Typing `/` at the start of
the input offers the command names.

## File paths with `@`

Typing `@` followed by part of a path offers matching files of the session
directory. A path matches when the typed characters appear in it in the same
order (ignoring case), so `@srvmain` finds `src/server/main.py`; matches in the
file name come first. Choosing a file replaces `@...` with the path (in double
quotes if it contains spaces).

In a Git work tree the files are those Git lists: tracked files and untracked
files that are not ignored. Elsewhere meltzer lists the directory, skipping
hidden directories, up to 50,000 files. The list is read again after 30
seconds.

## Dialogs

Approval requests, untrusted hooks and the [project trust](trust.md) question
open a dialog over the transcript. The dialog shows the same request as the
line-based prompt (see [approvals](approvals.md)) and offers the same answers:

| Request | Answers |
|---|---|
| command, patch, MCP tool call, web search | Yes, once · Yes, and always allow commands starting with the suggested prefix (when one is offered) · No |
| `request_permissions` | Yes, for this turn · Yes, for this session · No |
| network access | Yes, once · Yes, for this session · No |
| untrusted hook | Yes, trust and run this hook · No |
| project trust | Yes, trust this directory · No |

"No" is selected when the dialog opens. Up and Down change the selection, Enter
confirms it, and Tab moves between the request text (to scroll a long request)
and the answers. Esc and Ctrl-C answer No; for the project trust question they
close the dialog without an answer, and the session runs without the project
config, as when the line-based prompt gets no answer. Typing letters does not
answer a dialog.

Errors printed while the interface runs are shown in the transcript and
printed again on the terminal after the interface closes.
