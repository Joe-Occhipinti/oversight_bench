# Memories

With memories on, meltzer keeps short notes from your earlier sessions (how you
like to work, a project's build and test commands, problems that were solved)
and adds them to new sessions, so you do not have to repeat them.

Memories are off by default. Turn them on in `~/.meltzer/config.toml`; the
`[memories]` keys are read only from the user config and the command line:

```toml
[memories]
generate = true   # write notes from finished sessions
use = true        # add the notes to new sessions
```

The two keys are independent: with only `generate`, notes are written but not
used; with only `use`, existing notes are used and no new ones are written.

## Writing notes

When a session starts with `generate` on, meltzer reads the earlier sessions
recorded under `~/.meltzer/sessions/` that it has not read before:

- at most 5 per start, oldest first; the rest are read at later starts;
- only sessions with no activity in the last hour;
- only top-level sessions: subagent sessions are part of the session that
  started them and are not read on their own.

For each session, meltzer sends the conversation (your prompts, the model's
answers, tool calls and their results) to the model in one request, with no
tools, and asks for a short list of facts worth keeping. Long tool output is
shortened. The model may answer that nothing is worth keeping; the session then
has no note.

The request uses `memories.extract_model`, or the session's model when it is
not set. Likely secrets (API keys, access tokens, private keys, and values of
names such as `password=` or `API_KEY:`) are replaced with `[REDACTED]` in the
text sent to the model and in the notes it writes.

If the model request fails, meltzer shows a warning and starts the session as
usual; the session that failed is read again at the next start.

Notes are written at session start, before the first prompt; subagents do not
write notes.

## Using notes

When a session starts with `use` on, meltzer adds the summary of the notes, and
the notes imported by [`meltzer migrate`](migrate.md), to the system
instructions. The model sees them as notes from earlier sessions. Subagents
have the same instructions as the session that started them.

The summary holds the newest notes first, up to `memories.max_bytes` (16 KiB by
default). The imported notes are the Markdown files under
`~/.meltzer/memories/imported/<key>/`, in name order, up to `memories.max_bytes`
in total.

## Files

```
~/.meltzer/memories/
  <session id>.md      the note for one session
  memory_summary.md    the summary added to new sessions
  imported/<key>/      notes imported by `meltzer migrate`
  processed.json       the sessions already read
```

All files are under `~/.meltzer`, which sandboxed commands can read but not
write. You can edit or delete notes; `memory_summary.md` is rebuilt from the
notes at the next start with `generate` on. Delete `processed.json` to have all
sessions read again.

## Configuration

| Key ([all keys](config.md)) | Default | Meaning |
|---|---|---|
| `memories.generate` | `false` | write notes from finished sessions at session start |
| `memories.use` | `false` | add the summary of the notes to new sessions |
| `memories.extract_model` | the session's model | model that writes the notes |
| `memories.max_bytes` | `16384` | size of the summary, and of the imported notes, in bytes |
