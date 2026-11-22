# Plugins

A plugin is a directory that bundles skills, MCP servers and hooks. Plugins are offered by marketplaces: local directories that list plugins in a `marketplace.json` file. You add a marketplace, install a plugin from it, and enable the plugin; new sessions then load what it contains.

```sh
meltzer plugin add ~/src/acme-plugins        # record the marketplace
meltzer plugin install db-tools@acme         # copy the plugin into ~/.meltzer/plugins/cache/
meltzer plugin enable db-tools@acme          # load it in new sessions
```

Plugins are installed and enabled only from these commands (or by editing `~/.meltzer/config.toml`).

## Marketplaces

A marketplace is a directory with a `.agents/plugins/marketplace.json` file:

```json
{
  "name": "acme",
  "plugins": [
    {"name": "db-tools", "source": "./plugins/db-tools", "category": "data", "description": "Database tools"},
    {"name": "lint", "source": {"source": "local", "path": "./plugins/lint"}}
  ]
}
```

| Field | Meaning |
|---|---|
| `name` | Marketplace name (required). |
| `plugins[].name` | Plugin name (required). |
| `plugins[].source` | Plugin directory, relative to the marketplace directory and starting with `./`; or `{"source": "local", "path": "./..."}`. |
| `plugins[].description`, `plugins[].category` | Shown by `meltzer plugin add`. |

Only local sources are supported. Marketplace and plugin names may contain letters, digits, `_` and `-`. A plugin is named `<plugin>@<marketplace>`.

`meltzer plugin add` takes the marketplace directory, or the path of a marketplace file; for a file outside a `.agents/plugins/` directory, sources are relative to the directory that holds the file. It records the marketplace in the user config:

```toml
[marketplaces.acme]
source_type = "local"
source = "/home/me/src/acme-plugins"
last_updated = "2026-11-22T10:00:00+00:00"
```

A marketplace name can be added from one source only; to move it, edit or remove its entry first.

## Plugin directories

A plugin directory has a manifest, `.meltzer-plugin/plugin.json`:

```json
{
  "name": "db-tools",
  "version": "1.2.0",
  "description": "Database tools",
  "skills": "./skills",
  "mcpServers": "./.mcp.json",
  "hooks": "./hooks/hooks.json"
}
```

| Field | Default | Meaning |
|---|---|---|
| `name` | required | Must match the plugin's name in the marketplace. |
| `version` | `"local"` | Version, used as the name of the installed directory. |
| `description` | | Description of the plugin. |
| `skills` | `./skills`, when present | Directory of skills, added as a skills root (see [skills.md](skills.md)). |
| `mcpServers` | `./.mcp.json`, when present | File of MCP servers. |
| `hooks` | `./hooks/hooks.json`, when present | File of hooks. |

Paths start with `./` and stay inside the plugin directory. Other fields are ignored.

### MCP servers

`.mcp.json` holds `{"mcpServers": {<name>: {...}}}`, or the map of servers directly. Each server has the fields of `[mcp_servers.<name>]` (see [mcp.md](mcp.md)):

```json
{"mcpServers": {"db": {"command": "python3", "args": ["server.py"], "env_vars": ["DB_URL"]}}}
```

A plugin server runs in the plugin's installed directory; a relative `cwd` is relative to that directory and must stay inside it. Plugin servers start and stop with the session and are reviewed as other MCP servers. A server whose name is already used in `[mcp_servers]` or by an earlier plugin (in `<plugin>@<marketplace>` order) is skipped with a warning.

Approval modes of plugin servers are set in the user config, not in `.mcp.json`:

```toml
[plugins."db-tools@acme".mcp_servers.db]
default_tools_approval_mode = "writes"

[plugins."db-tools@acme".mcp_servers.db.tools.schema]
approval_mode = "approve"
```

`enabled = false` in the same table keeps a server of the plugin from starting.

### Hooks

`hooks/hooks.json` holds the hooks by event, with the fields of `[[hooks.<event>]]` (see [hooks.md](hooks.md)):

```json
{"hooks": {"PreToolUse": [{"matcher": "shell", "command": ["python3", "hooks/check_command.py"]}]}}
```

A plugin hook runs in the plugin's installed directory; a relative `cwd` is relative to that directory and must stay inside it. Plugin hooks are trusted like other hooks: an interactive session asks about each new one, and `meltzer exec` skips untrusted hooks. Reinstalling a plugin at another version asks again.

## Installing

`meltzer plugin install <plugin>@<marketplace>` reads the marketplace from its recorded source and copies the plugin directory to `~/.meltzer/plugins/cache/<marketplace>/<plugin>/<version>/`. The version installed before, if any, is replaced. Sessions load the installed copy, so later changes to the source take effect when the plugin is installed again. A plugin directory that contains symbolic links is not installed.

The command prints what the plugin adds to a session: its skills directory, the command line and `env_vars` of each MCP server, and the command of each hook.

## Enabling

`meltzer plugin enable <plugin>@<marketplace>` records the plugin in the user config:

```toml
[plugins."db-tools@acme"]
enabled = true
```

The `[plugins]` and `[marketplaces]` tables are read only from the user config and the command line. A plugin without `enabled = true` is not loaded. An enabled plugin that is not installed, or whose files cannot be read, is skipped with a warning at session start; so is a server or hook of it that is invalid.

## Configuration

| Key ([all keys](config.md)) | Default | Meaning |
|---|---|---|
| `marketplaces.<name>.source_type` | | `"local"`. |
| `marketplaces.<name>.source` | | Marketplace directory or file. |
| `marketplaces.<name>.last_updated` | | When the marketplace was added. |
| `plugins."<plugin>@<marketplace>".enabled` | `false` | Load the plugin in new sessions. |
| `plugins."<plugin>@<marketplace>".mcp_servers.<server>.enabled` | `true` | Start this server of the plugin. |
| `plugins."<plugin>@<marketplace>".mcp_servers.<server>.default_tools_approval_mode` | `"auto"` | Approval mode of the server's tools. |
| `plugins."<plugin>@<marketplace>".mcp_servers.<server>.tools.<tool>.approval_mode` | | Approval mode of one tool. |
