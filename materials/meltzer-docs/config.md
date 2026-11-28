# Configuration

Settings are read when a session starts, from these layers (later wins):

1. built-in defaults
2. user config: `~/.meltzer/config.toml`
3. project config: `.meltzer/config.toml` in the project.
   Used only in a [trusted project](trust.md).
4. command line (`-c` and flags; flags win over `-c`)

`-c KEY=VALUE` takes a dotted key (`-c network.enabled=true`); the value is
parsed as TOML. A key that is not in the tables below is a configuration error,
and so is an invalid value (exit code 2).

- Keys marked **user only** are read only from the user config and the command
  line. In a project config they are ignored with a warning.
- The lists `hooks.<Event>`, `network.allowed_domains` and
  `network.denied_domains` from several layers are combined.
- The tables `mcp_servers`, `permissions`, `plugins` and `marketplaces` are
  merged by entry name; a later layer replaces an entry of the same name.

Example `~/.meltzer/config.toml`:

```toml
model = "gpt-5.1"
approvals_reviewer = "monitor"
notify = ["python3", "/home/me/.meltzer/notify.py"]

[model_provider]
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"
```

## Model and session

| Key | Type | Default | Meaning |
|---|---|---|---|
| `model` | string | `gpt-5.1` | model name |
| `model_provider.base_url` | string | `https://api.openai.com/v1` | Chat Completions endpoint |
| `model_provider.api_key_env` | string | `MELTZER_API_KEY` | environment variable that holds the API key |
| `max_turn_steps` | integer | `100` | model requests per turn |
| `model_context_window` | integer | unset | context window of the model, in tokens (see [tools](tools.md#context-remaining)) |
| `notify` | list of strings | unset | program to run when a turn completes ([notify](notify.md)) |
| `developer_instructions_file` | string | unset | file whose text is appended to the system prompt |
| `projects."<dir>".trust_level` | string | unset | `trusted` or `untrusted`; **user only** ([trust](trust.md)) |

## Sandbox and approvals

| Key | Type | Default | Meaning |
|---|---|---|---|
| `sandbox_mode` | string | `workspace-write` | `read-only`, `workspace-write` or `full-access` ([sandbox](sandbox.md)) |
| `default_permissions` | string | unset | permission profile to use instead of `sandbox_mode` ([permissions](permissions.md)) |
| `permissions.<name>` | table | none | a permission profile |
| `approval_policy` | string | `on-request` | `on-request` or `never` ([approvals](approvals.md)) |
| `approvals_reviewer` | string | `user` | `user` or `monitor` |
| `monitor.model` | string | session model | model for monitor reviews |
| `monitor.transcript_items` | integer | `40` | transcript length of monitor reviews |
| `monitor.environment` | string | unset | description of this machine for monitor reviews; **user only** |
| `monitor.trusted_hosts` | list | `[]` | hosts monitor reviews treat as trusted services of the user's organization; **user only** |

Keys of `permissions.<name>`: `description` (text), `extends` (profile name),
`workspace_roots` (table, path to `true` or `false`), `filesystem` (table, path
to `read`, `write` or `deny`), `network.enabled` (bool).

## Network and web search

| Key | Type | Default | Meaning |
|---|---|---|---|
| `network.enabled` | bool | `false` | network access for sandboxed commands, through the proxy ([network](network.md)) |
| `network.allowed_domains` | list of strings | `[]` | hosts commands may reach without review |
| `network.denied_domains` | list of strings | `[]` | hosts commands may never reach |
| `network.allow_local_binding` | bool | `false` | commands can reach loopback addresses through the proxy; **user only** |
| `web_search.enabled` | bool | `false` | register the `web_search` tool; **user only** ([web search](web-search.md)) |
| `web_search.endpoint` | string | unset | URL of the search service; **user only** |
| `web_search.api_key_env` | string | unset | environment variable that holds the search API key; **user only** |

## Processes and subagents

| Key | Type | Default | Meaning |
|---|---|---|---|
| `background_terminal_max_timeout` | integer | `300000` | longest wait, in milliseconds, of an empty `write_stdin` call ([exec](exec.md)) |
| `features.unified_exec` | boolean | `true` | command tools: `exec_command` and `write_stdin` when true, `shell` when false ([tools](tools.md)) |
| `agents.max_depth` | integer | `1` | how deep subagents can be nested ([agents](agents.md)) |
| `agents.max_threads` | integer | `6` | subagents open at once |
| `agents.default_model` | string | unset | model for subagents without a role model |

## Hooks

`hooks.<Event>` is a list of tables, one per hook, for the events
`SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PreToolUse`,
`PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`,
`SubagentStart`, `SubagentStop` and `Stop` ([hooks](hooks.md)). Each hook has
`command` (list of strings, required), `cwd` (string, default `.`, relative to
the session directory) and `matcher` (string, default `*`).

## MCP servers

`mcp_servers.<name>` is a table per server ([MCP](mcp.md)).

| Key | Type | Default | Meaning |
|---|---|---|---|
| `command` | string | required | program that runs the server |
| `args` | list of strings | `[]` | arguments of the program |
| `env` | table | `{}` | variables set in the server's environment |
| `env_vars` | list | `[]` | variables copied from meltzer's environment |
| `cwd` | string | session root | working directory of the server |
| `enabled` | bool | `true` | start the server |
| `required` | bool | `false` | the session does not start if the server fails to start |
| `startup_timeout_sec` | number | `30` | time allowed for startup |
| `tool_timeout_sec` | number | `300` | time allowed for each tool call |
| `enabled_tools` | list of strings | all | only these tools are offered |
| `disabled_tools` | list of strings | `[]` | these tools are never offered |
| `default_tools_approval_mode` | string | `auto` | `auto`, `prompt`, `writes` or `approve`; **user only** |
| `tools.<tool>.approval_mode` | string | `default_tools_approval_mode` | approval mode of one tool; **user only** |

## Skills, plugins and memories

| Key | Type | Default | Meaning |
|---|---|---|---|
| `skills.include_instructions` | bool | `true` | add the list of skills to the system message ([skills](skills.md)) |
| `skills.config` | list of tables | `[]` | `name` or `path`, and `enabled`; **user only** |
| `plugins."<plugin>@<marketplace>"` | table | none | `enabled`, and settings of its MCP servers; **user only** ([plugins](plugins.md)) |
| `marketplaces.<name>` | table | none | `source_type`, `source`, `last_updated`; **user only** |
| `memories.generate` | bool | `false` | write notes from finished sessions; **user only** ([memories](memories.md)) |
| `memories.use` | bool | `false` | add the notes to new sessions; **user only** |
| `memories.extract_model` | string | session model | model that writes the notes; **user only** |
| `memories.max_bytes` | integer | `16384` | size of the summary and of imported notes; **user only** |

## Interfaces, images and telemetry

| Key | Type | Default | Meaning |
|---|---|---|---|
| `tui.enabled` | bool | `false` | full-screen interface ([tui](tui.md)) |
| `realtime.base_url` | string | `wss://api.openai.com/v1/realtime` | realtime service; **user only** ([voice](voice.md)) |
| `realtime.model` | string | `gpt-realtime` | realtime model |
| `realtime.voice` | string | `marin` | voice of the realtime model |
| `image_generation.model` | string | unset | Images API model; registers `image_gen`; **user only** ([images](images.md)) |
| `analytics.enabled` | bool | `false` | collect events for the exporter; **user only** ([telemetry](telemetry.md)) |
| `otel.exporter` | string or table | `"none"` | `"none"` or `{ otlp-http = { endpoint, headers, protocol } }`; **user only** |
| `feedback.enabled` | bool | `false` | keep recent log lines for `/feedback`; **user only** |
| `feedback.endpoint` | string | unset | where `/feedback` sends a report; **user only** |

## Files

| Path | Content |
|---|---|
| `~/.meltzer/config.toml` | user config, project trust decisions |
| `~/.meltzer/rules/` | user prefix rules |
| `~/.meltzer/agents/`, `~/.meltzer/skills/` | user agent roles and skills |
| `~/.meltzer/trusted_hooks.toml` | trusted hooks |
| `~/.meltzer/sessions/YYYY/MM/DD/*.jsonl` | session logs |
| `~/.meltzer/logs/<session>/` | output of background commands, MCP server logs |
| `~/.meltzer/memories/`, `~/.meltzer/plugins/cache/` | memories, installed plugins |
| `~/.meltzer/generated_images/<session>/`, `~/.meltzer/feedback/` | generated images, feedback reports |
| `<system temp dir>/meltzer-<session>-<random>/` | `/tmp` of sandboxed commands, removed at session end |
| `<project>/.meltzer/` | project config, rules, skills and agent roles (used in trusted projects) |
