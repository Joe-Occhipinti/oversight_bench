"""Plugins: marketplaces (marketplace.py), installed plugins (store.py) and loading them (loader.py).

    meltzer plugin add <dir or marketplace.json>   record a local marketplace in ~/.meltzer/config.toml
    meltzer plugin install <plugin>@<marketplace>  copy a plugin into ~/.meltzer/plugins/cache/
    meltzer plugin enable <plugin>@<marketplace>   enable an installed plugin for new sessions
"""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

from meltzer.config.loader import meltzer_home, user_config_path
from meltzer.mcp.config import parse_mcp_servers
from meltzer.plugins.loader import load_plugins
from meltzer.plugins.marketplace import PluginError, PluginId, load_marketplace
from meltzer.plugins.store import PluginStore, configured_marketplaces, record_marketplace, set_plugin_enabled

EXIT_OK = 0
EXIT_ERROR = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meltzer plugin", description="Add marketplaces and install plugins.")
    commands = parser.add_subparsers(dest="action", required=True)
    add = commands.add_parser("add", help="add a local marketplace")
    add.add_argument("path", type=Path, help="marketplace directory or marketplace.json file")
    install = commands.add_parser("install", help="install a plugin from an added marketplace")
    install.add_argument("plugin", metavar="PLUGIN@MARKETPLACE")
    enable = commands.add_parser("enable", help="enable an installed plugin")
    enable.add_argument("plugin", metavar="PLUGIN@MARKETPLACE")
    return parser


def add_marketplace(path: Path, home: Path | None = None) -> list[str]:
    marketplace = load_marketplace(path.expanduser())
    source = path.expanduser().resolve()
    record_marketplace(user_config_path(home), marketplace, source)
    lines = [f"added marketplace {marketplace.name} from {source}"]
    for plugin in marketplace.plugins:
        about = f": {plugin.description}" if plugin.description else ""
        lines.append(f"  {plugin.name}@{marketplace.name}{about}")
    return lines


def install_plugin(text: str, home: Path | None = None) -> list[str]:
    plugin_id = PluginId.parse(text)
    config_path = user_config_path(home)
    entry = configured_marketplaces(config_path).get(plugin_id.marketplace)
    if not isinstance(entry, dict) or not isinstance(entry.get("source"), str):
        raise PluginError(
            f"marketplace {plugin_id.marketplace} is not added; run `meltzer plugin add <dir>` first"
        )
    marketplace = load_marketplace(Path(entry["source"]))
    if marketplace.name != plugin_id.marketplace:
        raise PluginError(
            f"{marketplace.path} now names the marketplace '{marketplace.name}'; add it again"
        )
    listed = marketplace.get(plugin_id.plugin)
    if listed is None:
        raise PluginError(f"marketplace {plugin_id.marketplace} has no plugin {plugin_id.plugin}")
    installed = PluginStore(meltzer_home(home)).install(plugin_id, listed.source)
    lines = [f"installed {plugin_id} {installed.version} from {listed.source} to {installed.path}"]
    # What the plugin adds to a session once enabled.
    loaded = load_plugins({str(plugin_id): {"enabled": True}}, meltzer_home(home))
    for plugin in loaded.plugins:
        if plugin.skill_roots:
            lines.append(f"  skills: {', '.join(str(root) for root in plugin.skill_roots)}")
        for server in parse_mcp_servers(plugin.mcp_servers):
            env_vars = f"; env_vars: {', '.join(server.env_vars)}" if server.env_vars else ""
            lines.append(f"  MCP server {server.name}: {shlex.join([server.command, *server.args])}{env_vars}")
        for hook in plugin.hooks:
            lines.append(f"  {hook.event} hook: {shlex.join(hook.command)}")
    lines.extend(f"warning: {warning}" for warning in loaded.warnings)
    lines.append(f"enable it with `meltzer plugin enable {plugin_id}`")
    return lines


def enable_plugin(text: str, home: Path | None = None) -> list[str]:
    plugin_id = PluginId.parse(text)
    if PluginStore(meltzer_home(home)).installed(plugin_id) is None:
        raise PluginError(f"{plugin_id} is not installed; run `meltzer plugin install {plugin_id}` first")
    config_path = user_config_path(home)
    set_plugin_enabled(config_path, plugin_id, True)
    return [f"enabled {plugin_id} in {config_path}; new sessions load it"]


def run_plugin(argv: list[str], *, home: Path | None = None) -> int:
    """Entry point for `meltzer plugin ...`; argv excludes `plugin`."""
    args = build_parser().parse_args(argv)
    try:
        if args.action == "add":
            lines = add_marketplace(args.path, home)
        elif args.action == "install":
            lines = install_plugin(args.plugin, home)
        else:
            lines = enable_plugin(args.plugin, home)
    except PluginError as exc:
        print(f"meltzer plugin {args.action}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    for line in lines:
        print(line)
    return EXIT_OK
