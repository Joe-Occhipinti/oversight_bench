"""Plugin manifests, the installed plugins and their entries in the user config.

A plugin is a directory with a manifest, .meltzer-plugin/plugin.json:

    {
      "name": "db-tools",
      "version": "1.2.0",               # optional; "local" when absent
      "description": "Database tools",
      "skills": "./skills",             # default: skills/, when present
      "mcpServers": "./.mcp.json",      # default: .mcp.json, when present
      "hooks": "./hooks/hooks.json"     # default: hooks/hooks.json, when present
    }

Installing copies the plugin directory to
~/.meltzer/plugins/cache/<marketplace>/<plugin>/<version>/, replacing the
version installed before. Sessions load plugins from there.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tomllib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from meltzer.config.trust import table_path
from meltzer.plugins.marketplace import LOCAL_SOURCE, Marketplace, PluginError, PluginId, check_name, relative_path
from meltzer.sandbox.policy import resolve
from meltzer.tomlwrite import toml_string

MANIFEST_FILE = Path(".meltzer-plugin") / "plugin.json"
DEFAULT_SKILLS_DIR = "skills"
DEFAULT_MCP_FILE = ".mcp.json"
DEFAULT_HOOKS_FILE = "hooks/hooks.json"
# Version directory of plugins whose manifest gives no version.
LOCAL_VERSION = "local"
PLUGINS_DIR = "plugins"
CACHE_DIR = "cache"
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9_+-][A-Za-z0-9._+-]*$")
BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")

MARKETPLACES_KEY = "marketplaces"
PLUGINS_KEY = "plugins"
ENABLED_KEY = "enabled"


@dataclass(frozen=True)
class PluginManifest:
    name: str
    version: str | None
    description: str | None
    # Absolute paths inside the plugin directory; None when the plugin has none.
    skills: Path | None
    mcp_servers: Path | None
    hooks: Path | None


def _manifest_path(root: Path, data: dict[str, Any], key: str, default: str) -> Path | None:
    """The path a manifest key names, or the default path when the key is absent and the default exists."""
    if key in data:
        if not isinstance(data[key], str):
            raise PluginError(f"'{key}' must be a path starting with './', got {data[key]!r}")
        return relative_path(root, data[key], f"'{key}'")
    path = root / default
    return resolve(path) if path.exists() else None


def read_manifest(root: Path) -> PluginManifest:
    """Read the manifest of the plugin directory root. Raises PluginError."""
    file = root / MANIFEST_FILE
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PluginError(f"{root} has no plugin manifest ({MANIFEST_FILE.as_posix()})") from None
    except OSError as exc:
        raise PluginError(f"{file}: cannot read file: {exc.strerror}") from None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginError(f"{file}: not valid JSON: {exc}") from None
    try:
        if not isinstance(data, dict):
            raise PluginError("expected a JSON object")
        name = check_name("plugin", data.get("name"))
        version = data.get("version")
        if version is not None and (not isinstance(version, str) or not VERSION_PATTERN.match(version)):
            raise PluginError(f"'version' must be a version string such as \"1.2.0\", got {version!r}")
        description = data.get("description")
        if description is not None and not isinstance(description, str):
            raise PluginError(f"'description' must be a string, got {description!r}")
        return PluginManifest(
            name=name,
            version=version,
            description=description,
            skills=_manifest_path(root, data, "skills", DEFAULT_SKILLS_DIR),
            mcp_servers=_manifest_path(root, data, "mcpServers", DEFAULT_MCP_FILE),
            hooks=_manifest_path(root, data, "hooks", DEFAULT_HOOKS_FILE),
        )
    except PluginError as exc:
        raise PluginError(f"{file}: {exc}") from None


@dataclass(frozen=True)
class InstalledPlugin:
    id: PluginId
    version: str
    # The installed copy.
    path: Path


def _check_no_links(source: Path) -> None:
    for directory, subdirectories, files in os.walk(source):
        for name in (*subdirectories, *files):
            path = Path(directory) / name
            if path.is_symlink():
                raise PluginError(f"{path} is a symbolic link; plugins are installed from regular files only")


class PluginStore:
    """Installed plugins, under ~/.meltzer/plugins/cache/<marketplace>/<plugin>/<version>/."""

    def __init__(self, meltzer_home: Path):
        self.cache_dir = meltzer_home / PLUGINS_DIR / CACHE_DIR

    def plugin_dir(self, plugin_id: PluginId) -> Path:
        return self.cache_dir / plugin_id.marketplace / plugin_id.plugin

    def installed(self, plugin_id: PluginId) -> InstalledPlugin | None:
        """The installed version of a plugin, or None."""
        directory = self.plugin_dir(plugin_id)
        if not directory.is_dir():
            return None
        versions = [p for p in directory.iterdir() if p.is_dir() and not p.name.startswith(".")]
        if not versions:
            return None
        latest = max(versions, key=lambda p: p.stat().st_mtime)
        return InstalledPlugin(id=plugin_id, version=latest.name, path=resolve(latest))

    def install(self, plugin_id: PluginId, source: Path) -> InstalledPlugin:
        """Copy the plugin directory source into the cache. Raises PluginError."""
        if not source.is_dir():
            raise PluginError(f"plugin source {source} is not a directory")
        manifest = read_manifest(source)
        if manifest.name != plugin_id.plugin:
            raise PluginError(
                f"plugin manifest name '{manifest.name}' does not match marketplace plugin name '{plugin_id.plugin}'"
            )
        _check_no_links(source)
        version = manifest.version or LOCAL_VERSION
        target = self.plugin_dir(plugin_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.parent / f".staging-{uuid.uuid4().hex}"
        previous = target.parent / f".previous-{uuid.uuid4().hex}"
        try:
            shutil.copytree(source, staging / version)
            if target.exists():
                os.replace(target, previous)
            os.replace(staging, target)
        except OSError as exc:
            # Put the version installed before back in place.
            if previous.exists() and not target.exists():
                os.replace(previous, target)
            raise PluginError(f"cannot install {plugin_id} into {target}: {exc}") from None
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(previous, ignore_errors=True)
        return InstalledPlugin(id=plugin_id, version=version, path=resolve(target / version))


# Entries in the user config


def _key(name: str) -> str:
    return name if BARE_KEY.match(name) else toml_string(name)


def _value(value: str | bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return toml_string(value)


def set_table_values(text: str, table: tuple[str, ...], values: dict[str, str | bool]) -> str:
    """Return `text` with `values` set in the table `table`; other lines are kept."""
    lines = text.splitlines(keepends=True)
    header = next((n for n, line in enumerate(lines) if table_path(line) == table), None)
    if header is None:
        prefix = text
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        if prefix:
            prefix += "\n"
        body = "".join(f"{_key(key)} = {_value(value)}\n" for key, value in values.items())
        return prefix + f"[{'.'.join(_key(part) for part in table)}]\n" + body
    end = next((n for n in range(header + 1, len(lines)) if table_path(lines[n]) is not None), len(lines))
    if not lines[header].endswith("\n"):
        lines[header] += "\n"
    for key, value in values.items():
        assignment = f"{_key(key)} = {_value(value)}\n"
        pattern = re.compile(rf"^\s*{re.escape(_key(key))}\s*=")
        line = next((n for n in range(header + 1, end) if pattern.match(lines[n])), None)
        if line is not None:
            lines[line] = assignment
            continue
        lines.insert(header + 1, assignment)
        end += 1
    return "".join(lines)


def _read_text(config_path: Path) -> str:
    try:
        return config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    except (OSError, UnicodeDecodeError) as exc:
        raise PluginError(f"cannot read {config_path}: {exc}") from None


def _lookup(data: dict[str, Any], table: tuple[str, ...]) -> Any:
    for part in table:
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def _update_config(config_path: Path, table: tuple[str, ...], values: dict[str, str | bool]) -> None:
    """Set values in a table of the user config; the file is replaced only if the result reads back."""
    updated = set_table_values(_read_text(config_path), table, values)
    try:
        saved = _lookup(tomllib.loads(updated), table)
    except tomllib.TOMLDecodeError as exc:
        raise PluginError(f"cannot update {config_path}; edit it by hand: {exc}") from None
    if not isinstance(saved, dict) or any(saved.get(key) != value for key, value in values.items()):
        raise PluginError(f"cannot update {config_path}; edit it by hand")
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = config_path.with_name(config_path.name + ".tmp")
        temporary.write_text(updated, encoding="utf-8")
        os.replace(temporary, config_path)
    except OSError as exc:
        raise PluginError(f"cannot write {config_path}: {exc.strerror}") from None


def configured_marketplaces(config_path: Path) -> dict[str, Any]:
    """The [marketplaces] table of the user config."""
    text = _read_text(config_path)
    try:
        table = tomllib.loads(text).get(MARKETPLACES_KEY, {})
    except tomllib.TOMLDecodeError as exc:
        raise PluginError(f"{config_path}: {exc}") from None
    if not isinstance(table, dict):
        raise PluginError(f"{config_path}: '{MARKETPLACES_KEY}' must be a table")
    return table


def record_marketplace(config_path: Path, marketplace: Marketplace, source: Path, now: datetime | None = None) -> None:
    """Record a marketplace as [marketplaces.<name>] in the user config."""
    existing = configured_marketplaces(config_path).get(marketplace.name)
    if isinstance(existing, dict) and existing.get("source") not in (None, str(source)):
        raise PluginError(f"marketplace {marketplace.name} is already added from {existing['source']}")
    timestamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    _update_config(
        config_path,
        (MARKETPLACES_KEY, marketplace.name),
        {"source_type": LOCAL_SOURCE, "source": str(source), "last_updated": timestamp},
    )


def set_plugin_enabled(config_path: Path, plugin_id: PluginId, enabled: bool) -> None:
    """Set [plugins."<plugin>@<marketplace>"] enabled in the user config."""
    _update_config(config_path, (PLUGINS_KEY, str(plugin_id)), {ENABLED_KEY: enabled})
