from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest

from meltzer.config.loader import ConfigError, load_config, user_config_path
from meltzer.hooks import PRE_TOOL_USE, STOP
from meltzer.mcp.config import ToolApprovalMode, parse_mcp_servers
from meltzer.plugins import run_plugin
from meltzer.plugins.loader import load_plugins
from meltzer.plugins.marketplace import PluginError, PluginId, load_marketplace
from meltzer.plugins.store import (
    LOCAL_VERSION,
    PluginStore,
    read_manifest,
    record_marketplace,
    set_plugin_enabled,
    set_table_values,
)


def write_json(path: Path, data: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def make_plugin(directory: Path, name: str = "db-tools", **manifest: object) -> Path:
    write_json(directory / ".meltzer-plugin" / "plugin.json", {"name": name, **manifest})
    return directory


def make_marketplace(root: Path, plugins: list[dict] | None = None, name: str = "acme") -> Path:
    if plugins is None:
        plugins = [{"name": "db-tools", "source": "./plugins/db-tools", "category": "data"}]
    write_json(root / ".agents" / "plugins" / "marketplace.json", {"name": name, "plugins": plugins})
    return root


@pytest.fixture
def store(home: Path) -> PluginStore:
    return PluginStore(home / ".meltzer")


DB_TOOLS = PluginId("db-tools", "acme")


# Plugin ids


def test_plugin_id_parse():
    assert PluginId.parse("db-tools@acme") == DB_TOOLS
    assert str(DB_TOOLS) == "db-tools@acme"


@pytest.mark.parametrize("text", ["db-tools", "@acme", "db-tools@", "a@b@c", "db tools@acme", "../x@acme"])
def test_invalid_plugin_ids(text):
    with pytest.raises(PluginError):
        PluginId.parse(text)


# Marketplace files


def test_load_marketplace_from_directory(tmp_path):
    root = make_marketplace(
        tmp_path / "repo",
        [
            {"name": "db-tools", "source": "./plugins/db-tools", "category": "data", "description": "Database tools"},
            {"name": "lint", "source": {"source": "local", "path": "./plugins/lint"}},
        ],
    )
    marketplace = load_marketplace(root)
    assert marketplace.name == "acme"
    assert marketplace.root == root.resolve()
    assert marketplace.path == (root / ".agents" / "plugins" / "marketplace.json").resolve()
    assert [p.name for p in marketplace.plugins] == ["db-tools", "lint"]
    db_tools = marketplace.get("db-tools")
    assert db_tools.source == (root / "plugins" / "db-tools").resolve()
    assert db_tools.category == "data" and db_tools.description == "Database tools"
    assert marketplace.get("lint").source == (root / "plugins" / "lint").resolve()
    assert marketplace.get("other") is None
    assert load_marketplace(root / ".agents" / "plugins" / "marketplace.json").root == root.resolve()
    elsewhere = write_json(tmp_path / "other" / "catalog.json", {"name": "local", "plugins": [{"name": "lint", "source": "./lint"}]})
    assert load_marketplace(elsewhere).root == elsewhere.parent.resolve()
    assert load_marketplace(elsewhere).get("lint").source == (elsewhere.parent / "lint").resolve()


@pytest.mark.parametrize(
    "data, message",
    [
        ({"plugins": []}, "'name'"),
        ({"name": "ac me", "plugins": []}, "marketplace name"),
        ({"name": "acme", "plugins": {}}, "'plugins'"),
        ({"name": "acme", "plugins": [{"source": "./x"}]}, "plugin name"),
        ({"name": "acme", "plugins": [{"name": "x"}]}, "'source'"),
        ({"name": "acme", "plugins": [{"name": "x", "source": "plugins/x"}]}, "must start with './'"),
        ({"name": "acme", "plugins": [{"name": "x", "source": "./../x"}]}, "inside"),
        ({"name": "acme", "plugins": [{"name": "x", "source": "/abs/x"}]}, "must start with './'"),
        ({"name": "acme", "plugins": [{"name": "x", "source": {"source": "url", "url": "https://e.com/x"}}]},
         "only local sources"),
        ({"name": "acme", "plugins": [{"name": "x", "source": {"source": "local"}}]}, "'path'"),
        ({"name": "acme", "plugins": [{"name": "x", "source": "./x"}, {"name": "x", "source": "./y"}]},
         "listed twice"),
    ],
)
def test_invalid_marketplace_files(tmp_path, data, message):
    path = write_json(tmp_path / "repo" / ".agents" / "plugins" / "marketplace.json", data)
    with pytest.raises(PluginError, match=message):
        load_marketplace(path)


def test_missing_or_unreadable_marketplace(tmp_path):
    with pytest.raises(PluginError, match="no marketplace file"):
        load_marketplace(tmp_path)
    path = tmp_path / ".agents" / "plugins" / "marketplace.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(PluginError, match="not valid JSON"):
        load_marketplace(tmp_path)


# Manifests


def test_read_manifest(tmp_path):
    plugin = make_plugin(tmp_path / "db-tools", description="Database tools")
    (plugin / "skills").mkdir()
    write_json(plugin / ".mcp.json", {"mcpServers": {}})
    manifest = read_manifest(plugin)
    assert manifest.name == "db-tools" and manifest.version is None and manifest.description == "Database tools"
    assert manifest.skills == (plugin / "skills").resolve()
    assert manifest.mcp_servers == (plugin / ".mcp.json").resolve()
    assert manifest.hooks is None
    plugin = make_plugin(
        tmp_path / "other",
        version="1.2.0",
        skills="./agent-skills",
        mcpServers="./config/mcp.json",
        hooks="./config/hooks.json",
        keywords=["db"],
        interface={"displayName": "DB tools"},
    )
    manifest = read_manifest(plugin)
    assert manifest.version == "1.2.0"
    assert manifest.skills == (plugin / "agent-skills").resolve()
    assert manifest.mcp_servers == (plugin / "config" / "mcp.json").resolve()
    assert manifest.hooks == (plugin / "config" / "hooks.json").resolve()
    with pytest.raises(PluginError, match="plugin.json"):
        read_manifest(tmp_path)


@pytest.mark.parametrize(
    "fields, message",
    [
        ({"name": ""}, "plugin name"),
        ({"version": "../1"}, "'version'"),
        ({"version": 2}, "'version'"),
        ({"skills": "skills"}, "must start with './'"),
        ({"mcpServers": "./../shared/mcp.json"}, "inside"),
        ({"hooks": 3}, "'hooks'"),
    ],
)
def test_invalid_manifests(tmp_path, fields, message):
    plugin = tmp_path / "db-tools"
    write_json(plugin / ".meltzer-plugin" / "plugin.json", {"name": "db-tools", **fields})
    with pytest.raises(PluginError, match=message):
        read_manifest(plugin)


# Install


def test_install_copies_the_plugin_into_the_cache(tmp_path, store, home):
    source = make_plugin(tmp_path / "src", version="1.2.0")
    (source / "skills" / "query").mkdir(parents=True)
    (source / "skills" / "query" / "SKILL.md").write_text("---\ndescription: Query\n---\nbody\n", encoding="utf-8")
    installed = store.install(DB_TOOLS, source)
    expected = home / ".meltzer" / "plugins" / "cache" / "acme" / "db-tools" / "1.2.0"
    assert installed.path == expected.resolve()
    assert installed.version == "1.2.0" and installed.id == DB_TOOLS
    assert (expected / "skills" / "query" / "SKILL.md").read_text(encoding="utf-8").endswith("body\n")
    assert store.installed(DB_TOOLS) == installed
    # The installed copy does not follow later changes to the source.
    (source / "skills" / "query" / "SKILL.md").write_text("changed", encoding="utf-8")
    assert (expected / "skills" / "query" / "SKILL.md").read_text(encoding="utf-8").endswith("body\n")


def test_reinstall_replaces_the_installed_version(tmp_path, store):
    source = make_plugin(tmp_path / "src")
    (source / "old.txt").write_text("old", encoding="utf-8")
    first = store.install(DB_TOOLS, source)
    assert first.version == first.path.name == LOCAL_VERSION
    (source / "old.txt").unlink()
    make_plugin(source, version="2.0.0")
    second = store.install(DB_TOOLS, source)
    assert second.version == "2.0.0"
    assert not first.path.exists()
    assert not (second.path / "old.txt").exists()
    assert store.installed(DB_TOOLS) == second
    assert sorted(p.name for p in second.path.parent.parent.iterdir()) == ["db-tools"]


def test_install_requires_matching_manifest_name(tmp_path, store):
    source = make_plugin(tmp_path / "src", name="other")
    with pytest.raises(PluginError, match="does not match"):
        store.install(DB_TOOLS, source)
    assert store.installed(DB_TOOLS) is None
    with pytest.raises(PluginError, match="not a directory"):
        store.install(DB_TOOLS, tmp_path / "missing")


def test_install_rejects_symbolic_links(tmp_path, store):
    source = make_plugin(tmp_path / "src")
    (tmp_path / "outside.txt").write_text("x", encoding="utf-8")
    try:
        os.symlink(tmp_path / "outside.txt", source / "link.txt")
    except OSError:
        pytest.skip("cannot create symbolic links here")
    with pytest.raises(PluginError, match="symbolic link"):
        store.install(DB_TOOLS, source)
    assert store.installed(DB_TOOLS) is None
    assert not store.cache_dir.exists() or not any(store.cache_dir.rglob("link.txt"))


# Config records


def test_set_table_values_adds_and_updates_tables():
    text = 'model = "gpt-5.1"\n\n[projects."/src/app"]\ntrust_level = "trusted"\n'
    added = set_table_values(text, ("plugins", "db-tools@acme"), {"enabled": True})
    assert added.startswith(text)
    data = tomllib.loads(added)
    assert data["plugins"]["db-tools@acme"] == {"enabled": True}
    assert data["projects"]["/src/app"] == {"trust_level": "trusted"}
    updated = set_table_values(added, ("plugins", "db-tools@acme"), {"enabled": False})
    assert tomllib.loads(updated)["plugins"]["db-tools@acme"] == {"enabled": False}
    assert updated.count("[plugins.") == 1
    nested = '[plugins."db-tools@acme"]\nenabled = false\n\n[plugins."db-tools@acme".mcp_servers.db]\nenabled = false\n'
    data = tomllib.loads(set_table_values(nested, ("plugins", "db-tools@acme"), {"enabled": True}))
    assert data["plugins"]["db-tools@acme"] == {"enabled": True, "mcp_servers": {"db": {"enabled": False}}}


def test_record_marketplace(tmp_path, home):
    root = make_marketplace(tmp_path / "repo")
    config_path = user_config_path(home)
    marketplace = load_marketplace(root)
    record_marketplace(config_path, marketplace, root.resolve())
    entry = tomllib.loads(config_path.read_text(encoding="utf-8"))["marketplaces"]["acme"]
    assert entry["source_type"] == "local"
    assert entry["source"] == str(root.resolve())
    assert entry["last_updated"]
    # Adding the same marketplace again updates the entry.
    record_marketplace(config_path, marketplace, root.resolve())
    assert config_path.read_text(encoding="utf-8").count("[marketplaces.acme]") == 1
    other = make_marketplace(tmp_path / "two")
    with pytest.raises(PluginError, match="already added"):
        record_marketplace(config_path, load_marketplace(other), other.resolve())


def test_set_plugin_enabled_is_read_by_the_config_loader(tmp_path, home):
    set_plugin_enabled(user_config_path(home), DB_TOOLS, True)
    work = tmp_path / "work"
    work.mkdir()
    config = load_config(work, home=home)
    assert config.plugins == {"db-tools@acme": {"enabled": True}}


# Loading enabled plugins


def install_full_plugin(tmp_path: Path, store: PluginStore, **servers: dict) -> Path:
    source = make_plugin(tmp_path / "src", version="1.0.0")
    (source / "skills" / "query").mkdir(parents=True)
    write_json(source / ".mcp.json", {"mcpServers": servers or {"db": {"command": "db-mcp", "args": ["--ro"]}}})
    write_json(
        source / "hooks" / "hooks.json",
        {"hooks": {"PreToolUse": [{"matcher": "shell", "command": ["python3", "hooks/check.py"]}]}},
    )
    return store.install(DB_TOOLS, source).path


def test_disabled_and_absent_plugins_add_nothing(tmp_path, store, home):
    install_full_plugin(tmp_path, store)
    for entries in ({}, {"db-tools@acme": {"enabled": False}}, {"db-tools@acme": {}}):
        plugins = load_plugins(entries, home / ".meltzer")
        assert plugins.plugins == () and plugins.warnings == ()
        assert plugins.skill_roots() == () and plugins.mcp_servers() == {} and plugins.hook_definitions() == ()


def test_enabled_plugin_that_is_not_installed(home):
    plugins = load_plugins({"db-tools@acme": {"enabled": True}}, home / ".meltzer")
    assert plugins.plugins == ()
    assert plugins.warnings == ("plugin db-tools@acme is enabled but not installed; run `meltzer plugin install db-tools@acme`",)


def test_enabled_plugin_adds_skills_servers_and_hooks(tmp_path, store, home):
    path = install_full_plugin(tmp_path, store)
    plugins = load_plugins({"db-tools@acme": {"enabled": True}}, home / ".meltzer")
    assert plugins.warnings == ()
    (loaded,) = plugins.plugins
    assert loaded.id == DB_TOOLS and loaded.version == "1.0.0" and loaded.root == path
    assert plugins.skill_roots() == (path / "skills",)
    assert plugins.mcp_servers() == {"db": {"command": "db-mcp", "args": ["--ro"], "cwd": str(path)}}
    (hook,) = plugins.hook_definitions()
    assert hook.event == PRE_TOOL_USE and hook.matcher == "shell"
    assert hook.command == ("python3", "hooks/check.py") and hook.cwd == str(path)


def test_server_cwd_is_relative_to_the_plugin_and_must_stay_inside_it(tmp_path, store, home):
    source = make_plugin(tmp_path / "src")
    servers = {"in": {"command": "in-mcp", "cwd": "./server"}, "db": {"command": "db-mcp", "cwd": "../../.."}}
    write_json(source / ".mcp.json", {**servers, "ok": {"command": "ok-mcp"}})
    path = store.install(DB_TOOLS, source).path
    plugins = load_plugins({"db-tools@acme": {"enabled": True}}, home / ".meltzer")
    assert list(plugins.mcp_servers()) == ["in", "ok"]
    assert plugins.mcp_servers()["in"]["cwd"] == str(path / "server")
    assert any("mcp server 'db'" in w and "inside the plugin" in w for w in plugins.warnings)


def test_approval_modes_and_enabled_come_from_the_user_config(tmp_path, store, home):
    install_full_plugin(
        tmp_path,
        store,
        db={"command": "db-mcp", "default_tools_approval_mode": "approve", "tools": {"drop": {"approval_mode": "approve"}}},
    )
    entries = {
        "db-tools@acme": {
            "enabled": True,
            "mcp_servers": {"db": {"default_tools_approval_mode": "writes", "tools": {"query": {"approval_mode": "approve"}}}},
        }
    }
    plugins = load_plugins(entries, home / ".meltzer")
    (server,) = parse_mcp_servers(plugins.mcp_servers())
    assert server.default_tools_approval_mode == ToolApprovalMode.WRITES
    assert server.approval_mode("query") == ToolApprovalMode.APPROVE
    assert server.approval_mode("drop") == ToolApprovalMode.WRITES
    assert len([w for w in plugins.warnings if "approval_mode" in w]) == 2
    entries["db-tools@acme"]["mcp_servers"]["db"] = {"enabled": False}
    (server,) = parse_mcp_servers(load_plugins(entries, home / ".meltzer").mcp_servers())
    assert not server.enabled


def test_configured_server_with_the_same_name_wins(tmp_path, store, home):
    install_full_plugin(tmp_path, store)
    plugins = load_plugins({"db-tools@acme": {"enabled": True}}, home / ".meltzer", configured_servers={"db"})
    assert plugins.mcp_servers() == {}
    assert plugins.warnings == (
        "plugin db-tools@acme: mcp server 'db' skipped: [mcp_servers] already defines a server named 'db'",
    )


def test_first_plugin_keeps_a_shared_server_name(tmp_path, store, home):
    for plugin in ("alpha", "beta"):
        source = make_plugin(tmp_path / plugin, name=plugin)
        write_json(source / ".mcp.json", {"db": {"command": f"{plugin}-mcp"}})
        store.install(PluginId(plugin, "acme"), source)
    entries = {"beta@acme": {"enabled": True}, "alpha@acme": {"enabled": True}}
    plugins = load_plugins(entries, home / ".meltzer")
    assert plugins.mcp_servers()["db"]["command"] == "alpha-mcp"
    assert plugins.warnings == (
        "plugin beta@acme: mcp server 'db' skipped: plugin alpha@acme already defines a server named 'db'",
    )


def test_invalid_server_entry_is_skipped(tmp_path, store, home):
    install_full_plugin(tmp_path, store, db={"url": "https://example.com/mcp"}, ok={"command": "ok-mcp"})
    plugins = load_plugins({"db-tools@acme": {"enabled": True}}, home / ".meltzer")
    assert list(plugins.mcp_servers()) == ["ok"]
    assert any("HTTP servers are not supported" in w for w in plugins.warnings)


def test_invalid_plugin_files_are_skipped_with_a_warning(tmp_path, store, home):
    source = make_plugin(tmp_path / "src")
    (source / ".mcp.json").write_text("{broken", encoding="utf-8")
    write_json(source / "hooks" / "hooks.json", {"hooks": {"Unknown": [], STOP: [{"command": []}]}})
    store.install(DB_TOOLS, source)
    plugins = load_plugins({"db-tools@acme": {"enabled": True}}, home / ".meltzer")
    assert plugins.mcp_servers() == {} and plugins.hook_definitions() == ()
    assert any(".mcp.json" in w and "not valid JSON" in w for w in plugins.warnings)
    assert any("unknown hook event 'Unknown'" in w for w in plugins.warnings)
    assert any(f"{STOP} hook #1" in w for w in plugins.warnings)


@pytest.mark.parametrize(
    "entries, message",
    [
        ({"db-tools": {"enabled": True}}, "plugins.db-tools"),
        ({"db-tools@acme": True}, "must be a table"),
        ({"db-tools@acme": {"enabled": "yes"}}, "'enabled'"),
        ({"db-tools@acme": {"enabled": True, "skills": False}}, "unknown field 'skills'"),
        ({"db-tools@acme": {"mcp_servers": {"db": {"command": "x"}}}}, "unknown field 'command'"),
    ],
)
def test_invalid_plugin_settings_are_config_errors(home, entries, message):
    with pytest.raises(ConfigError, match=message):
        load_plugins(entries, home / ".meltzer")


# meltzer plugin commands


def test_plugin_commands(tmp_path, home, capsys):
    root = make_marketplace(tmp_path / "repo")
    plugin = make_plugin(root / "plugins" / "db-tools", version="1.0.0")
    write_json(plugin / ".mcp.json", {"mcpServers": {"db": {"command": "db-mcp", "args": ["--ro"], "env_vars": ["DB_URL"]}}})
    write_json(plugin / "hooks" / "hooks.json", {"hooks": {"PreToolUse": [{"command": ["python3", "hooks/check.py"]}]}})

    assert run_plugin(["add", str(root)], home=home) == 0
    out = capsys.readouterr().out
    assert "added marketplace acme" in out and "db-tools@acme" in out

    assert run_plugin(["install", "db-tools@acme"], home=home) == 0
    out = capsys.readouterr().out
    assert "installed db-tools@acme 1.0.0" in out
    assert "MCP server db: db-mcp --ro; env_vars: DB_URL" in out
    assert "PreToolUse hook: python3 hooks/check.py" in out
    assert (home / ".meltzer" / "plugins" / "cache" / "acme" / "db-tools" / "1.0.0").is_dir()

    assert run_plugin(["enable", "db-tools@acme"], home=home) == 0
    assert "enabled db-tools@acme" in capsys.readouterr().out
    data = tomllib.loads(user_config_path(home).read_text(encoding="utf-8"))
    assert data["plugins"]["db-tools@acme"] == {"enabled": True}
    assert data["marketplaces"]["acme"]["source"] == str(root.resolve())


def test_plugin_command_errors(tmp_path, home, capsys):
    cases = [
        (["install", "db-tools@acme"], "marketplace acme is not added"),
        (["enable", "db-tools@acme"], "db-tools@acme is not installed"),
        (["install", "db-tools"], "expected <plugin>@<marketplace>"),
        (["add", str(tmp_path)], "no marketplace file"),
    ]
    for argv, message in cases:
        assert run_plugin(argv, home=home) == 1
        assert message in capsys.readouterr().err
    assert not user_config_path(home).exists()
    assert run_plugin(["add", str(make_marketplace(tmp_path / "repo"))], home=home) == 0
    assert run_plugin(["install", "lint@acme"], home=home) == 1
    assert "marketplace acme has no plugin lint" in capsys.readouterr().err
