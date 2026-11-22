# MCP servers

meltzer can use tools from [Model Context Protocol](https://modelcontextprotocol.io) servers. A server is a local program that meltzer starts and talks to over its stdin and stdout (the stdio transport). Servers reached over HTTP are not supported.

## Configuration

Servers are declared in `~/.meltzer/config.toml`, or in the `.meltzer/config.toml` of a trusted project, one table per server:

```toml
[mcp_servers.db]
command = "python3"
args = ["tools/db_mcp.py"]
env = { DB_URL = "sqlite:///data/app.db" }
env_vars = ["GITHUB_TOKEN"]
cwd = "."
tool_timeout_sec = 60
enabled_tools = ["query", "schema", "insert"]
default_tools_approval_mode = "writes"

[mcp_servers.db.tools.schema]
approval_mode = "approve"
```

| Key ([all keys](config.md)) | Default | Meaning |
|---|---|---|
| `command` | required | Program that runs the server. |
| `args` | `[]` | Arguments of the program. |
| `env` | `{}` | Variables set in the server's environment. |
| `env_vars` | `[]` | Names of variables copied from meltzer's environment, when set. An entry can also be written `{ name = "GITHUB_TOKEN", source = "local" }`. |
| `cwd` | session root | Working directory of the server; a relative path is relative to the session root. |
| `enabled` | `true` | `false` keeps the entry without starting the server. |
| `required` | `false` | `true`: the session does not start if this server fails to start. |
| `startup_timeout_sec` | `30` | Time allowed for `initialize` and the first tool listing. |
| `tool_timeout_sec` | `300` | Time allowed for each tool call. |
| `enabled_tools` | all | Only these tools are offered to the model. |
| `disabled_tools` | `[]` | These tools are never offered (applied after `enabled_tools`). |
| `default_tools_approval_mode` | `"auto"` | Approval mode of the server's tools (see below). |
| `tools.<tool>.approval_mode` | | Approval mode of one tool. Read only from the user config and the command line. |

Server names may contain letters, digits, `_` and `-`. A project entry with the same name as a user entry replaces it, and keeps the approval modes of the user entry. A user entry that has only approval modes (no `command`) starts no server; its modes apply to a server of the same name from a project config.

## Lifecycle

Every enabled server starts when the session starts, and stops when the session ends: meltzer closes its stdin, then stops its process group (SIGTERM, then SIGKILL after 2 seconds). Each server runs in its own process group, with the environment described below: only `HOME`, `LOGNAME`, `PATH`, `SHELL`, `USER`, `LANG`, `LC_ALL`, `TERM`, `TMPDIR` and `TZ` (when set), the variables named in `env_vars`, and `env`.

The server's stderr is written to `~/.meltzer/logs/<session id>/mcp-<name>.log`.

A server that fails to start, or does not answer within `startup_timeout_sec`, is left out and meltzer prints a notice; a `required` server stops the session instead. A server that exits during the session is not restarted: later calls to its tools fail.

Subagents use the servers of the session that started them.

## Tools

meltzer lists the server's tools once, when the server starts. Each tool is offered to the model as `mcp__<server>__<tool>`, with the description and input schema the server gives. Characters other than letters, digits, `_` and `-` become `_`; names are at most 64 characters, and names that would be longer or that two tools would share end in a short hash.

A call's result is returned to the model as text: the server's structured content as JSON when there is some, otherwise the text of the content blocks, one per line (images and audio appear as a placeholder). A result the server marks as an error is returned as a failed tool call.

Hooks see MCP calls under the `mcp__<server>__<tool>` name, so a `PreToolUse` matcher such as `mcp__db__insert` applies to them.

## Approvals

Servers can describe their tools with annotations: `readOnlyHint` (the tool does not change anything), `destructiveHint` (the tool may delete or overwrite) and `openWorldHint` (the tool reaches outside systems). The approval mode decides which calls go to the reviewer (`approvals_reviewer`):

| Mode | Calls that are reviewed |
|---|---|
| `auto` | Tools marked destructive, and tools without hints. Tools marked read-only, or marked neither destructive nor open-world, run directly. |
| `writes` | Every tool not marked read-only. |
| `prompt` | Every call. |
| `approve` | None. |

The reviewer sees the server, the tool, its arguments, and the description and annotations the server gives. Each call is reviewed on its own. With `approval_policy = "never"`, calls that need review are rejected; with `approval_policy = "never"` and `sandbox_mode = "full-access"`, calls run directly.

Approval modes are read from the user config and the command line. On the command line they can be set only by giving the whole `mcp_servers` table with `-c`, for example `-c 'mcp_servers={ notes = { command = "notes-mcp", default_tools_approval_mode = "approve" } }'`; a single dotted key such as `-c mcp_servers.notes.default_tools_approval_mode="approve"` is an unknown key. A `-c` table replaces the whole entry of the same name.

## Example: a read-only tool and a reviewed one

```toml
[mcp_servers.notes]
command = "notes-mcp"
args = ["--dir", "notes"]

[mcp_servers.notes.tools.search]
approval_mode = "approve"

[mcp_servers.notes.tools.delete]
approval_mode = "prompt"
```

`search` runs without asking; every `delete` call is reviewed.
