# Migrating from Claude Code

`meltzer migrate` imports instructions, hooks, the sandbox setting, MCP servers
and memories from Claude Code files.

```sh
meltzer migrate                 # in the project directory
meltzer migrate -C ~/src/app    # another project directory
meltzer migrate --dry-run       # list what would be imported; write nothing
```

The command lists the items it found and asks about each one before writing
it. It runs only in a terminal; `meltzer exec` never imports anything.

## What is imported

| Source | Target |
|---|---|
| `CLAUDE.md` in the project directory | `AGENTS.md` in the same directory |
| hooks in `~/.claude/settings.json` | `[[hooks.<Event>]]` in `~/.meltzer/config.toml` |
| `sandbox.enabled = true` in `~/.claude/settings.json` | `sandbox_mode = "workspace-write"` in `~/.meltzer/config.toml` |
| hooks in `.claude/settings.json` and `.claude/settings.local.json` of the project | `[[hooks.<Event>]]` in the project config |
| `sandbox.enabled = true` in the same files | `sandbox_mode = "workspace-write"` in the project config |
| servers in `.mcp.json` of the project | `[mcp_servers.<name>]` in the project config, and `default_tools_approval_mode = "prompt"` for each in `~/.meltzer/config.toml` |
| `~/.claude/projects/<key>/memory/*.md` | `~/.meltzer/memories/imported/<key>/` |

The project config is the `.meltzer/config.toml` that sessions in the project
directory load (the nearest one up to the Git root), or
`<dir>/.meltzer/config.toml` when there is none. It is loaded only in a
[trusted project](trust.md).

When `settings.json` and `settings.local.json` both set `sandbox.enabled` or
`disableAllHooks`, the value in `settings.local.json` applies.

### AGENTS.md

`CLAUDE.md` is copied to `AGENTS.md` when `AGENTS.md` does not exist or is
empty. In the copy, `CLAUDE.md` becomes `AGENTS.md` and `Claude Code` becomes
`meltzer`; other text, such as model names, URLs and paths, is kept.

### Hooks

Command hooks of these events are imported: `PreToolUse`, `PostToolUse`,
`SessionStart`, `Stop`, `UserPromptSubmit` and `SessionEnd`. Each becomes a
hook that runs `sh -c <command>`; `CLAUDE_PROJECT_DIR` in the command becomes
`MELTZER_PROJECT_DIR`.

```json
{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "./hooks/check.sh"}]}]}}
```

becomes

```toml
[[hooks.PreToolUse]]
matcher = "shell|exec_command"
command = ["sh", "-c", "./hooks/check.sh"]
```

Tool names in matchers become meltzer tool names:

| Claude Code | meltzer |
|---|---|
| `Bash` | `shell`, `exec_command` |
| `Edit`, `MultiEdit`, `Write` | `apply_patch` |
| `*`, `.*`, empty | `*` |

Other names, such as `mcp__db__query`, are kept. A `SessionStart` group is
imported when it has no matcher or its matcher includes `startup`.

Not imported, and listed as such: hooks of other events, hooks of other types
(`prompt`, `agent`, `http`), `async` hooks, matchers that are patterns (such as
`mcp__.*`), and every hook when `disableAllHooks` is `true`. `timeout` is
dropped; meltzer hooks time out after 60 seconds (see [hooks](hooks.md)).

A hook that the config file already has is not added again. Imported hooks run
once you trust them: the next interactive session asks about each one, as for
any new hook.

### MCP servers

Servers of `.mcp.json` that meltzer starts with a command are imported with
`command`, `args` and `env`. The listing shows each server's command, `env`
values and `env_vars` before you answer. An `env` value of the form `${NAME}`, for a
variable of the same name, becomes an entry of `env_vars`:

```json
{"mcpServers": {"db": {"command": "python3", "args": ["tools/db.py"], "env": {"DB_URL": "sqlite:///x.db", "GITHUB_TOKEN": "${GITHUB_TOKEN}"}}}}
```

becomes

```toml
[mcp_servers.db]
command = "python3"
args = ["tools/db.py"]
env = { DB_URL = "sqlite:///x.db" }
env_vars = ["GITHUB_TOKEN"]
```

Not imported, and listed as such: servers reached over HTTP or SSE, servers
whose command, arguments or other `env` values use `${...}`, disabled servers
(`"disabled": true`, or listed in `disabledMcpjsonServers` of the project
settings, or missing from a non-empty `enabledMcpjsonServers`), and names that
meltzer does not accept. A server whose name is already used in the user config
or the project config is left as it is.

Each imported server gets `default_tools_approval_mode = "prompt"` in the user
config, so every call to it is reviewed (see [MCP](mcp.md)); you can change
the mode there.

### Memories

The Markdown files of each Claude Code project memory directory are copied to
`~/.meltzer/memories/imported/<key>/`, where `<key>` is the name of the Claude
Code project directory. Files already there are kept. With `memories.use` on,
sessions add these notes to their instructions; see [memories](memories.md).

## What is not imported

- Permission rules (`permissions.allow`, `permissions.deny`, `permissions.ask`).
  meltzer [rules](rules.md) are written by hand.
- Sessions and transcripts.
- Other settings, such as `env`, `model` and plugins.

## Config files

The command adds entries to config files and changes nothing else: a key that
is already set, a hook that is already configured and a server name that is
already used are left as they are. Top-level keys go before the first table of
the file, tables at its end. The file is replaced only when the result reads
back with exactly the old content and the new entries; otherwise the item is
not imported and the command asks you to edit the file by hand.
