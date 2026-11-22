# meltzer

A terminal coding agent. meltzer sends your request to a model served over an
OpenAI-compatible Chat Completions API, runs the commands the model asks for in a
bubblewrap sandbox, and edits files with patches.

Requires Python 3.12+. Sandboxed commands run on Linux, with
[bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`) on `PATH`; see
[Sandbox](docs/sandbox.md).

## Install

```sh
pip install .            # or: pipx install .
export MELTZER_API_KEY=sk-...
```

Optional extras (`pyproject.toml`):

- `tui`: the [full-screen interface](docs/tui.md) (`prompt_toolkit`); `pip install '.[tui]'`
- `voice`: [voice conversations](docs/voice.md) (`websockets`); `pip install '.[voice]'`
- `dev`: `pytest`, for development; `pip install -e '.[dev]'`

## Usage

```sh
meltzer                          # interactive session in the current directory
meltzer "explain src/session.py" # interactive, with a first prompt
meltzer exec "fix the failing test" -a never
echo "summarize the README" | meltzer exec -
```

Interactive mode reads one prompt per line. Commands: `/plan` (show or edit the
task plan), `/rollback N`, `/feedback`, `/exit` and `/quit` (Ctrl-D works too).
`!cmd` runs a shell command yourself ([tools](docs/tools.md#commands-you-run-with-cmd)).
Ctrl-C cancels the running turn; at the prompt it ends the session.

Options (all modes):

| Option | Meaning |
|---|---|
| `-C, --cd DIR` | run the session in `DIR` |
| `-m, --model MODEL` | model name |
| `-s, --sandbox MODE` | `read-only`, `workspace-write`, `full-access` |
| `-a, --approval-policy POLICY` | `on-request`, `never` |
| `-c KEY=VALUE` | set any config key; the value is parsed as TOML (repeatable) |
| `--dangerously-bypass-approvals-and-sandbox` | `full-access` and `never` |

## Features

- [Sandbox](docs/sandbox.md): modes, writable and protected paths, private `/tmp`.
- [Approvals](docs/approvals.md): approval policy, the user and monitor reviewers.
- [Project trust](docs/trust.md): when a project's config and rules are loaded.
- [Tools](docs/tools.md): what the model can call.
- [Interactive processes](docs/exec.md): `exec_command` and `write_stdin`.
- [Subagents](docs/agents.md): `spawn_agent`, `wait`, roles.
- [Permissions](docs/permissions.md): `request_permissions` and permission profiles.
- [Rules](docs/rules.md): prefix rules for commands.
- [Hooks](docs/hooks.md): programs that run on session and tool events.
- [MCP servers](docs/mcp.md): tools from local MCP servers.
- [Network](docs/network.md): network access through the proxy.
- [Web search](docs/web-search.md) and [image generation](docs/images.md): the `web_search` and `image_gen` tools.
- [Skills](docs/skills.md), [plugins](docs/plugins.md) and [memories](docs/memories.md): reusable instructions, bundles, notes from earlier sessions.
- [Resume, fork and rollback](docs/sessions.md): continue recorded sessions.
- [Migration](docs/migrate.md): import Claude Code settings.
- [Full-screen interface](docs/tui.md) and [voice](docs/voice.md): other ways to talk to meltzer.
- [Telemetry and feedback](docs/telemetry.md): events for an OpenTelemetry collector, feedback reports.
- [Notify](docs/notify.md), [AGENTS.md](docs/agents-md.md) and [`meltzer exec`](docs/exec-mode.md): turn notifications, project instructions, runs without a terminal.
- [Configuration](docs/config.md): every config key and the files meltzer uses.

## License

Apache-2.0
