"""Marketplace files: the plugins a local directory offers.

A marketplace is a directory with a .agents/plugins/marketplace.json file:

    {
      "name": "acme",
      "plugins": [
        {"name": "db-tools", "source": "./plugins/db-tools", "category": "data",
         "description": "Database tools"},
        {"name": "lint", "source": {"source": "local", "path": "./plugins/lint"}}
      ]
    }

`source` is a path relative to the marketplace directory, starting with `./`.
A marketplace file can also be given by its own path; sources are then
relative to the directory that holds it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from meltzer.sandbox.policy import is_within, resolve

MARKETPLACE_FILE = Path(".agents") / "plugins" / "marketplace.json"
LOCAL_SOURCE = "local"
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class PluginError(Exception):
    """A marketplace, plugin or plugin setting is invalid, or a plugin cannot be installed."""


def check_name(kind: str, name: Any) -> str:
    """A marketplace or plugin name: letters, digits, '_' and '-'."""
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise PluginError(f"invalid {kind} name {name!r}; use letters, digits, '_' and '-'")
    return name


@dataclass(frozen=True)
class PluginId:
    """A plugin of a marketplace, written `<plugin>@<marketplace>`."""

    plugin: str
    marketplace: str

    @classmethod
    def parse(cls, text: str) -> PluginId:
        plugin, sep, marketplace = text.partition("@")
        if not sep:
            raise PluginError(f"invalid plugin id {text!r}: expected <plugin>@<marketplace>")
        try:
            return cls(check_name("plugin", plugin), check_name("marketplace", marketplace))
        except PluginError as exc:
            raise PluginError(f"invalid plugin id {text!r}: {exc}") from None

    def __str__(self) -> str:
        return f"{self.plugin}@{self.marketplace}"


def relative_path(base: Path, value: Any, where: str) -> Path:
    """A `./`-relative path inside base, resolved. Raises PluginError."""
    if not isinstance(value, str) or not value.startswith("./"):
        raise PluginError(f"{where} must start with './', got {value!r}")
    if ".." in PurePosixPath(value).parts:
        raise PluginError(f"{where} must stay inside {base}, got {value!r}")
    path = resolve(base / value)
    if not is_within(path, resolve(base)):
        raise PluginError(f"{where} must stay inside {base}, got {value!r}")
    return path


@dataclass(frozen=True)
class MarketplacePlugin:
    name: str
    # Directory of the plugin.
    source: Path
    description: str | None = None
    category: str | None = None


@dataclass(frozen=True)
class Marketplace:
    name: str
    # The marketplace file and the directory its sources are relative to.
    path: Path
    root: Path
    plugins: tuple[MarketplacePlugin, ...]

    def get(self, name: str) -> MarketplacePlugin | None:
        return next((plugin for plugin in self.plugins if plugin.name == name), None)


def marketplace_file(path: Path) -> tuple[Path, Path]:
    """The marketplace file for a directory or file path, and the directory its sources are relative to."""
    path = resolve(path)
    if path.is_dir():
        return path / MARKETPLACE_FILE, path
    if path.parent.name == MARKETPLACE_FILE.parent.name and path.parent.parent.name == MARKETPLACE_FILE.parts[0]:
        return path, path.parent.parent.parent
    return path, path.parent


def _optional_string(entry: dict[str, Any], key: str, where: str) -> str | None:
    value = entry.get(key)
    if value is not None and not isinstance(value, str):
        raise PluginError(f"{where}: '{key}' must be a string, got {value!r}")
    return value


def _source(root: Path, value: Any, where: str) -> Path:
    if isinstance(value, dict):
        kind = value.get("source")
        if kind != LOCAL_SOURCE:
            raise PluginError(f"{where}: unsupported source {kind!r}; only local sources are supported")
        if "path" not in value:
            raise PluginError(f"{where}: a local source needs a 'path'")
        value = value["path"]
    elif value is None:
        raise PluginError(f"{where}: 'source' is required")
    return relative_path(root, value, f"{where}: 'source'")


def _plugin(root: Path, entry: Any, index: int) -> MarketplacePlugin:
    where = f"plugin #{index + 1}"
    if not isinstance(entry, dict):
        raise PluginError(f"{where}: expected an object, got {entry!r}")
    name = check_name("plugin", entry.get("name"))
    where = f"plugin {name}"
    return MarketplacePlugin(
        name=name,
        source=_source(root, entry.get("source"), where),
        description=_optional_string(entry, "description", where),
        category=_optional_string(entry, "category", where),
    )


def load_marketplace(path: Path) -> Marketplace:
    """Read the marketplace at a directory or file path. Raises PluginError."""
    file, root = marketplace_file(path)
    if not file.is_file():
        raise PluginError(f"no marketplace file at {file}")
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PluginError(f"{file}: cannot read file: {exc.strerror}") from None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginError(f"{file}: not valid JSON: {exc}") from None
    try:
        if not isinstance(data, dict):
            raise PluginError("expected a JSON object")
        if "name" not in data:
            raise PluginError("'name' is required")
        name = check_name("marketplace", data["name"])
        entries = data.get("plugins", [])
        if not isinstance(entries, list):
            raise PluginError(f"'plugins' must be a list, got {entries!r}")
        plugins = [_plugin(root, entry, index) for index, entry in enumerate(entries)]
        names = [plugin.name for plugin in plugins]
        repeated = next((n for n in names if names.count(n) > 1), None)
        if repeated is not None:
            raise PluginError(f"plugin {repeated} is listed twice")
    except PluginError as exc:
        raise PluginError(f"{file}: {exc}") from None
    return Marketplace(name=name, path=file, root=root, plugins=tuple(plugins))
