# meltzer exec

`meltzer exec` runs one prompt without a terminal and prints the final answer on
stdout; progress goes to stderr. Exit codes: `0` success, `1` model error,
`2` configuration error, `130` interrupted.

```sh
meltzer exec "fix the failing test" -a never
echo "summarize the README" | meltzer exec -
```

The prompt is the argument, or `-` to read it from stdin. It takes the same
options as interactive mode (see the [README](../README.md)).

`meltzer exec` needs `approvals_reviewer = "monitor"` or
`approval_policy = "never"` (see [approvals](approvals.md)). It never asks about
[project trust](trust.md). `meltzer exec resume` continues a recorded session
with one prompt (see [sessions](sessions.md)).
