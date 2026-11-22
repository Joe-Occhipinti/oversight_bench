"""Loads the layered configuration: defaults < user < project < command line.

The user config lives in ~/.meltzer/config.toml, the project config in a
.meltzer/config.toml (see find_project_config).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Iterable

from meltzer.config.schema import KEYS, KEYS_BY_NAME, Config, ConfigValueError
from meltzer.config.trust import (
    PROJECTS_KEY,
    ProjectsTableError,
    ProjectTrust,
    TrustLevel,
    parse_projects,
    project_trust,
)
from meltzer.sandbox.policy import resolve

CONFIG_DIR_NAME = ".meltzer"
# MCP server settings that only the user config and the command line set.
MCP_SERVERS_KEY = "mcp_servers"
MCP_APPROVAL_KEY = "approval_mode"
MCP_DEFAULT_APPROVAL_KEY = "default_tools_approval_mode"
CONFIG_FILE_NAME = "config.toml"
DEFAULT_SOURCE = "default"


class ConfigError(Exception):
    """The configuration is invalid; the session does not start."""


def meltzer_home(home: Path | None = None) -> Path:
    return (home if home is not None else Path.home()) / CONFIG_DIR_NAME


def user_config_path(home: Path | None = None) -> Path:
    return meltzer_home(home) / CONFIG_FILE_NAME


def project_search_dirs(cwd: Path, home: Path | None = None) -> list[Path]:
    """Directories searched for project files, nearest first."""
    home_dir = resolve(home if home is not None else Path.home())
    found: list[Path] = []
    for directory in (cwd, *cwd.parents):
        found.append(directory)
        if (directory / ".git").exists() or resolve(directory) == home_dir:
            break
    return found


def find_project_config(cwd: Path, home: Path | None = None) -> Path | None:
    """Return the nearest project config for cwd, or None."""
    user_config = resolve(user_config_path(home))
    for directory in project_search_dirs(cwd, home):
        candidate = directory / CONFIG_DIR_NAME / CONFIG_FILE_NAME
        # The user config is already its own layer; do not load it twice.
        if candidate.is_file() and resolve(candidate) != user_config:
            return candidate
    return None


def parse_override(text: str) -> tuple[str, Any]:
    """Parse a `-c key=value` override. The value is read as TOML, falling back to a plain string."""
    key, sep, raw = text.partition("=")
    key = key.strip()
    if not sep or not key:
        raise ConfigError(f"invalid override {text!r}: expected key=value")
    raw = raw.strip()
    try:
        value = tomllib.loads(f"value = {raw}")["value"]
    except tomllib.TOMLDecodeError:
        value = raw
    return key, value


def _flatten(table: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for name, value in table.items():
        dotted = f"{prefix}{name}"
        if isinstance(value, dict) and dotted not in KEYS_BY_NAME:
            flat.update(_flatten(value, f"{dotted}."))
        else:
            flat[dotted] = value
    return flat


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read file: {exc.strerror}") from exc


class _Builder:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {
            key.name: dict(key.default) if isinstance(key.default, dict) else key.default for key in KEYS
        }
        self.sources: dict[str, str] = {key.name: DEFAULT_SOURCE for key in KEYS}
        self.warnings: list[str] = []

    def apply(self, entries: Iterable[tuple[str, Any]], origin: str, *, user_layer: bool = True) -> None:
        """Apply one layer. Keys that only the user may set are ignored, with a warning, in other layers."""
        for name, raw in entries:
            key = KEYS_BY_NAME.get(name)
            if key is None:
                raise ConfigError(f"{origin}: unknown key '{name}'")
            if key.user_only and not user_layer:
                self._ignored(origin, name)
                continue
            try:
                value = key.parse(raw)
            except ConfigValueError as exc:
                raise ConfigError(f"{origin}: invalid value for '{name}': {exc}") from None
            if name == MCP_SERVERS_KEY and not user_layer:
                value = self._keep_user_mcp_settings(self._drop_user_only_mcp_settings(value, origin))
            if key.merge:
                self._merge(name, value, origin)
                continue
            if key.merge_entries:
                self._merge_entries(name, value, origin)
                continue
            self.values[name] = value
            self.sources[name] = origin

    def _drop_user_only_mcp_settings(self, servers: dict[str, Any], origin: str) -> dict[str, Any]:
        """Remove approval modes from MCP server entries; only the user config and the command line set them."""
        for server, entry in servers.items():
            if not isinstance(entry, dict):
                continue
            if MCP_DEFAULT_APPROVAL_KEY in entry:
                del entry[MCP_DEFAULT_APPROVAL_KEY]
                self._ignored(origin, f"{MCP_SERVERS_KEY}.{server}.{MCP_DEFAULT_APPROVAL_KEY}")
            tools = entry.get("tools")
            if not isinstance(tools, dict):
                continue
            for tool, settings in tools.items():
                if isinstance(settings, dict) and MCP_APPROVAL_KEY in settings:
                    del settings[MCP_APPROVAL_KEY]
                    self._ignored(origin, f"{MCP_SERVERS_KEY}.{server}.tools.{tool}.{MCP_APPROVAL_KEY}")
        return servers

    def _keep_user_mcp_settings(self, servers: dict[str, Any]) -> dict[str, Any]:
        """Copy the approval modes of earlier entries into entries of the same name that replace them."""
        for server, entry in servers.items():
            earlier = self.values[MCP_SERVERS_KEY].get(server)
            if not isinstance(entry, dict) or not isinstance(earlier, dict):
                continue
            if MCP_DEFAULT_APPROVAL_KEY in earlier:
                entry[MCP_DEFAULT_APPROVAL_KEY] = earlier[MCP_DEFAULT_APPROVAL_KEY]
            earlier_tools = earlier.get("tools")
            if not isinstance(earlier_tools, dict):
                continue
            for tool, settings in earlier_tools.items():
                if not isinstance(settings, dict) or MCP_APPROVAL_KEY not in settings:
                    continue
                tools = entry.setdefault("tools", {})
                if not isinstance(tools, dict):
                    continue
                tool_settings = tools.setdefault(tool, {})
                if isinstance(tool_settings, dict):
                    tool_settings[MCP_APPROVAL_KEY] = settings[MCP_APPROVAL_KEY]
        return servers

    def _ignored(self, origin: str, name: str) -> None:
        self.warnings.append(f"{origin}: '{name}' can only be set in the user config or on the command line; ignored")

    def _add_source(self, name: str, origin: str) -> None:
        previous = self.sources[name]
        self.sources[name] = origin if previous == DEFAULT_SOURCE else f"{previous}, {origin}"

    def _merge(self, name: str, value: tuple[Any, ...], origin: str) -> None:
        """Append the entries of a later layer; entries already present are not added twice."""
        added = tuple(entry for entry in dict.fromkeys(value) if entry not in self.values[name])
        if not added:
            return
        self.values[name] = (*self.values[name], *added)
        self._add_source(name, origin)

    def _merge_entries(self, name: str, value: dict[str, Any], origin: str) -> None:
        """Add the named entries of a later layer; an entry with the same name replaces the earlier one."""
        if not value:
            return
        self.values[name] = {**self.values[name], **value}
        self._add_source(name, origin)

    def build(self, trust: ProjectTrust) -> Config:
        kwargs = {key.attr: self.values[key.name] for key in KEYS}
        return Config(
            **kwargs,
            sources=dict(self.sources),
            warnings=tuple(self.warnings),
            project_trust=trust,
        )


def _project_levels(data: dict[str, Any], path: Path) -> tuple[dict[Path, TrustLevel], list[str]]:
    """Trust levels from the [projects] table of the user config, removed from data."""
    if PROJECTS_KEY not in data:
        return {}, []
    try:
        levels, warnings = parse_projects(data.pop(PROJECTS_KEY))
    except ProjectsTableError as exc:
        raise ConfigError(f"{path}: {exc}") from None
    return levels, [f"{path}: {warning}" for warning in warnings]


def read_project_trust(cwd: Path, home: Path | None = None) -> ProjectTrust:
    """The trust decision the user config records for a session directory."""
    user_path = user_config_path(home)
    levels: dict[Path, TrustLevel] = {}
    if user_path.is_file():
        levels, _ = _project_levels(_read_toml(user_path), user_path)
    return project_trust(cwd, levels)


def load_config(
    cwd: Path,
    *,
    home: Path | None = None,
    overrides: Iterable[tuple[str, Any]] = (),
    unrecorded_trust: TrustLevel | None = None,
) -> Config:
    """Build the effective config for a session rooted at cwd.

    `unrecorded_trust` applies when the user config records no trust decision
    for cwd, e.g. an answer from this session that could not be saved.
    """
    builder = _Builder()

    user_path = user_config_path(home)
    levels: dict[Path, TrustLevel] = {}
    if user_path.is_file():
        data = _read_toml(user_path)
        levels, warnings = _project_levels(data, user_path)
        builder.warnings.extend(warnings)
        builder.apply(_flatten(data).items(), str(user_path))

    trust = project_trust(cwd, levels)
    if not trust.recorded and unrecorded_trust is not None:
        trust = ProjectTrust(root=trust.root, level=unrecorded_trust)

    project_path = find_project_config(cwd, home)
    if project_path is not None and trust.trusted:
        data = _read_toml(project_path)
        if PROJECTS_KEY in data:
            del data[PROJECTS_KEY]
            builder.warnings.append(f"{project_path}: '{PROJECTS_KEY}' can only be set in user config; ignored")
        builder.apply(_flatten(data).items(), str(project_path), user_layer=False)

    builder.apply(overrides, "command line")
    return builder.build(trust)
