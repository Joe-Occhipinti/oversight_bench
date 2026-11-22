"""Loads the enabled plugins of the user config for a session.

    [plugins."db-tools@acme"]
    enabled = true

    [plugins."db-tools@acme".mcp_servers.db]     # optional, per server of the plugin
    enabled = true
    default_tools_approval_mode = "writes"

    [plugins."db-tools@acme".mcp_servers.db.tools.query]
    approval_mode = "approve"

An enabled plugin is read from its installed copy (see meltzer.plugins.store)
and gives the session:
  - its skills directory, as a skills root;
  - the MCP servers of its .mcp.json, {"mcpServers": {<name>: {...}}} or a
    plain map of servers, with the fields of [mcp_servers.<name>]; servers run
    in the plugin directory, and a relative `cwd` is relative to it;
  - the hooks of its hooks/hooks.json, {"hooks": {<event>: [{...}]}}, with the
    fields of [[hooks.<event>]]; hooks run in the plugin directory, and a
    relative `cwd` is relative to it. Plugin hooks run once trusted, as any
    other hook.

Approval modes of plugin servers are set only in the user config, as above.
A plugin that cannot be loaded, or a server or hook of it, is skipped with a
warning; invalid [plugins] settings are a config error.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path, PurePath
from typing import Any, Iterable

from meltzer.config.loader import MCP_APPROVAL_KEY, MCP_DEFAULT_APPROVAL_KEY, ConfigError
from meltzer.hooks import HOOK_EVENTS, HookDefinition, parse_hook_entries
from meltzer.mcp.config import parse_mcp_servers
from meltzer.plugins.marketplace import PluginError, PluginId
from meltzer.plugins.store import ENABLED_KEY, PLUGINS_KEY, InstalledPlugin, PluginStore, read_manifest
from meltzer.sandbox.policy import is_within, resolve
from meltzer.tomlwrite import toml_string

MCP_SERVERS_KEY = "mcp_servers"
PLUGIN_FIELDS = (ENABLED_KEY, MCP_SERVERS_KEY)
# Settings of a plugin's server that the user config may set.
PLUGIN_SERVER_FIELDS = (ENABLED_KEY, MCP_DEFAULT_APPROVAL_KEY, "tools")
MCP_SERVERS_FILE_KEY = "mcpServers"
HOOKS_FILE_KEY = "hooks"


@dataclass(frozen=True)
class LoadedPlugin:
    id: PluginId
    version: str
    root: Path
    skill_roots: tuple[Path, ...]
    # Server entries in the form of [mcp_servers.<name>], by server name.
    mcp_servers: dict[str, dict[str, Any]]
    hooks: tuple[HookDefinition, ...]


@dataclass(frozen=True)
class PluginSet:
    """The plugins a session loads."""

    plugins: tuple[LoadedPlugin, ...] = ()
    warnings: tuple[str, ...] = ()

    def skill_roots(self) -> tuple[Path, ...]:
        return tuple(root for plugin in self.plugins for root in plugin.skill_roots)

    def mcp_servers(self) -> dict[str, dict[str, Any]]:
        """Server entries of every plugin, to add to the `mcp_servers` table."""
        return {name: entry for plugin in self.plugins for name, entry in plugin.mcp_servers.items()}

    def hook_definitions(self) -> tuple[HookDefinition, ...]:
        return tuple(hook for plugin in self.plugins for hook in plugin.hooks)


@dataclass(frozen=True)
class _PluginSettings:
    id: PluginId
    enabled: bool
    servers: dict[str, dict[str, Any]]


def _settings(key: str, entry: Any) -> _PluginSettings:
    """The [plugins."<id>"] entry of the user config. Raises ConfigError."""
    where = f"{PLUGINS_KEY}.{toml_string(key) if '@' in key else key}"
    try:
        plugin_id = PluginId.parse(key)
    except PluginError as exc:
        raise ConfigError(f"{where}: {exc}") from None
    if not isinstance(entry, dict):
        raise ConfigError(f"{where}: must be a table, got {entry!r}")
    unknown = sorted(set(entry) - set(PLUGIN_FIELDS))
    if unknown:
        raise ConfigError(f"{where}: unknown field '{unknown[0]}'")
    enabled = entry.get(ENABLED_KEY, False)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{where}: '{ENABLED_KEY}' must be true or false, got {enabled!r}")
    servers = entry.get(MCP_SERVERS_KEY, {})
    if not isinstance(servers, dict):
        raise ConfigError(f"{where}: '{MCP_SERVERS_KEY}' must be a table of server names")
    for name, settings in servers.items():
        server_where = f"{where}.{MCP_SERVERS_KEY}.{name}"
        if not isinstance(settings, dict):
            raise ConfigError(f"{server_where}: must be a table, got {settings!r}")
        unknown = sorted(set(settings) - set(PLUGIN_SERVER_FIELDS))
        if unknown:
            raise ConfigError(f"{server_where}: unknown field '{unknown[0]}'")
        # The values are checked as they would be in [mcp_servers.<name>].
        try:
            parse_mcp_servers({name: {"command": name, **settings}})
        except ConfigError as exc:
            message = str(exc).removeprefix(f"{MCP_SERVERS_KEY}.{name}: ")
            raise ConfigError(f"{server_where}: {message}") from None
    return _PluginSettings(id=plugin_id, enabled=enabled, servers=servers)


def _plugin_dir(root: Path, value: Any) -> Path:
    """A working directory inside the plugin: root itself, or a relative path inside it."""
    if value is None:
        return root
    if not isinstance(value, str) or not value or PurePath(value).is_absolute():
        raise PluginError(f"cwd must be a relative path inside the plugin directory, got {value!r}")
    path = resolve(root / value)
    if not is_within(path, root):
        raise PluginError(f"cwd must stay inside the plugin directory, got {value!r}")
    return path


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PluginError(f"{path}: cannot read file: {exc.strerror}") from None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginError(f"{path}: not valid JSON: {exc}") from None


class _Loader:
    def __init__(self, meltzer_home: Path, configured_servers: Iterable[str]):
        self.store = PluginStore(meltzer_home)
        self.warnings: list[str] = []
        # Server name -> who defines it.
        self.server_owners: dict[str, str] = {name: "" for name in configured_servers}

    def warn(self, plugin_id: PluginId, message: str) -> None:
        self.warnings.append(f"plugin {plugin_id}: {message}")

    def load(self, settings: _PluginSettings) -> LoadedPlugin | None:
        installed = self.store.installed(settings.id)
        if installed is None:
            self.warnings.append(
                f"plugin {settings.id} is enabled but not installed; run `meltzer plugin install {settings.id}`"
            )
            return None
        try:
            manifest = read_manifest(installed.path)
        except PluginError as exc:
            self.warn(settings.id, f"skipped: {exc}")
            return None
        skill_roots: tuple[Path, ...] = ()
        if manifest.skills is not None:
            if manifest.skills.is_dir():
                skill_roots = (manifest.skills,)
            else:
                self.warn(settings.id, f"skills directory {manifest.skills} does not exist")
        servers = self._servers(installed, manifest.mcp_servers, settings) if manifest.mcp_servers else {}
        hooks = self._hooks(installed, manifest.hooks) if manifest.hooks else ()
        return LoadedPlugin(
            id=settings.id,
            version=installed.version,
            root=installed.path,
            skill_roots=skill_roots,
            mcp_servers=servers,
            hooks=hooks,
        )

    def _servers(
        self, installed: InstalledPlugin, path: Path, settings: _PluginSettings
    ) -> dict[str, dict[str, Any]]:
        try:
            data = _read_json(path)
            if isinstance(data, dict) and MCP_SERVERS_FILE_KEY in data:
                data = data[MCP_SERVERS_FILE_KEY]
            if not isinstance(data, dict):
                raise PluginError(f"{path}: expected an object of servers")
        except PluginError as exc:
            self.warn(installed.id, str(exc))
            return {}
        servers: dict[str, dict[str, Any]] = {}
        for name, entry in data.items():
            if name in self.server_owners:
                owner = self.server_owners[name]
                where = f"plugin {owner}" if owner else f"[{MCP_SERVERS_KEY}]"
                self.warn(installed.id, f"mcp server '{name}' skipped: {where} already defines a server named '{name}'")
                continue
            if isinstance(entry, dict):
                entry = self._with_user_settings(installed.id, name, dict(entry), settings.servers.get(name, {}))
                try:
                    entry["cwd"] = str(_plugin_dir(installed.path, entry.get("cwd")))
                except PluginError as exc:
                    self.warn(installed.id, f"mcp server '{name}' skipped: {exc}")
                    continue
            try:
                parse_mcp_servers({name: entry})
            except ConfigError as exc:
                self.warn(installed.id, f"{exc}; skipped")
                continue
            servers[name] = entry
            self.server_owners[name] = str(installed.id)
        return servers

    def _with_user_settings(
        self, plugin_id: PluginId, name: str, entry: dict[str, Any], user: dict[str, Any]
    ) -> dict[str, Any]:
        """The server entry without the plugin's approval modes, with the user's settings applied."""
        user_table = f"[{PLUGINS_KEY}.{toml_string(str(plugin_id))}.{MCP_SERVERS_KEY}.{name}]"
        if MCP_DEFAULT_APPROVAL_KEY in entry:
            del entry[MCP_DEFAULT_APPROVAL_KEY]
            self.warn(plugin_id, f"'{name}.{MCP_DEFAULT_APPROVAL_KEY}' ignored; set it under {user_table} in the user config")
        tools = entry.get("tools")
        if isinstance(tools, dict):
            tools = {tool: dict(value) if isinstance(value, dict) else value for tool, value in tools.items()}
            for tool, value in tools.items():
                if isinstance(value, dict) and MCP_APPROVAL_KEY in value:
                    del value[MCP_APPROVAL_KEY]
                    self.warn(
                        plugin_id,
                        f"'{name}.tools.{tool}.{MCP_APPROVAL_KEY}' ignored; set it under {user_table} in the user config",
                    )
            entry["tools"] = tools
        for key, value in user.items():
            if key == "tools":
                merged = dict(entry.get("tools") or {})
                for tool, tool_settings in value.items():
                    merged[tool] = {**merged.get(tool, {}), **tool_settings}
                entry["tools"] = merged
            else:
                entry[key] = value
        return entry

    def _hooks(self, installed: InstalledPlugin, path: Path) -> tuple[HookDefinition, ...]:
        try:
            data = _read_json(path)
            events = data.get(HOOKS_FILE_KEY) if isinstance(data, dict) else None
            if not isinstance(events, dict):
                raise PluginError(f"{path}: expected an object with a '{HOOKS_FILE_KEY}' object of events")
        except PluginError as exc:
            self.warn(installed.id, str(exc))
            return ()
        hooks: list[HookDefinition] = []
        for event, entries in events.items():
            if event not in HOOK_EVENTS:
                self.warn(installed.id, f"{path}: unknown hook event '{event}'; skipped")
                continue
            try:
                definitions = parse_hook_entries(event, entries)
            except ValueError as exc:
                self.warn(installed.id, f"{path}: {event} {exc}; skipped")
                continue
            for definition in definitions:
                try:
                    cwd = _plugin_dir(installed.path, definition.cwd)
                except PluginError as exc:
                    self.warn(installed.id, f"{path}: {event} hook skipped: {exc}")
                    continue
                hooks.append(replace(definition, cwd=str(cwd)))
        return tuple(hooks)


def load_plugins(
    entries: dict[str, Any], meltzer_home: Path, *, configured_servers: Iterable[str] = ()
) -> PluginSet:
    """The enabled plugins of the [plugins] table, in id order.

    `configured_servers` are the names in [mcp_servers]; a plugin server with
    one of these names is skipped. Raises ConfigError for invalid settings.
    """
    settings = [_settings(key, entry) for key, entry in entries.items()]
    loader = _Loader(meltzer_home, configured_servers)
    plugins = []
    for item in sorted(settings, key=lambda s: str(s.id)):
        if not item.enabled:
            continue
        plugin = loader.load(item)
        if plugin is not None:
            plugins.append(plugin)
    return PluginSet(plugins=tuple(plugins), warnings=tuple(loader.warnings))
