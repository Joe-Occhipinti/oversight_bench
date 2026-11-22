from __future__ import annotations

import io
import json
import sys
import tomllib
from pathlib import Path

import pytest

from meltzer.config.loader import load_config, user_config_path
from meltzer.hooks import POST_TOOL_USE, PRE_TOOL_USE, SESSION_START, STOP, HookDefinition, TrustStore
from meltzer.mcp.config import parse_mcp_servers
from meltzer.migrate import (
    ItemType,
    MigrationError,
    detect,
    import_item,
    rewrite_instructions,
    run_migrate,
)


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def write_json(path: Path, data: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def trusted(root: Path) -> str:
    return f'\n[projects.{json.dumps(str(root))}]\ntrust_level = "trusted"\n'


def command_hook(command: str, **fields: object) -> dict:
    return {"type": "command", "command": command, **fields}


def read_toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    (path / ".git").mkdir(parents=True)
    return path


def items_of(home: Path, root: Path, kind: ItemType):
    return [item for item in detect(home, root).items if item.type == kind]


def only_item(home: Path, root: Path, kind: ItemType):
    items = items_of(home, root, kind)
    assert len(items) == 1, items
    return items[0]


# Instructions


def test_rewrite_instructions_renames_only_the_file_and_the_product():
    text = "Read CLAUDE.md first. Claude Code runs tests; claudette and claude-sonnet-4 stay. See ./claude.md and ~/.claude/x"
    assert rewrite_instructions(text) == (
        "Read AGENTS.md first. meltzer runs tests; claudette and claude-sonnet-4 stay. See ./AGENTS.md and ~/.claude/x"
    )


def test_claude_md_fills_an_empty_agents_md_and_never_replaces_a_written_one(home, root):
    write_text(root / "CLAUDE.md", "rules")
    write_text(root / "AGENTS.md", "  \n")
    item = only_item(home, root, ItemType.AGENTS_MD)
    assert item.sources == (root / "CLAUDE.md",) and item.target == root / "AGENTS.md"
    import_item(item)
    assert (root / "AGENTS.md").read_text(encoding="utf-8") == "rules\n"
    write_text(root / "AGENTS.md", "mine")
    assert items_of(home, root, ItemType.AGENTS_MD) == []
    assert "not changed" in " ".join(import_item(item))
    assert (root / "AGENTS.md").read_text(encoding="utf-8") == "mine"


# Hooks


def test_user_hooks_go_to_the_user_config(home, root):
    write_json(home / ".claude" / "settings.json", {"hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [command_hook('"$CLAUDE_PROJECT_DIR"/.claude/hooks/check.sh', timeout=30)]}],
        "PostToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [command_hook("ruff format")]}],
        "Stop": [{"hooks": [command_hook("notify-send done")]}],
        "SessionStart": [{"matcher": "startup", "hooks": [command_hook("git status")]}],
    }})
    item = only_item(home, root, ItemType.HOOKS)
    assert item.sources == (home / ".claude" / "settings.json",)
    assert item.target == user_config_path(home)
    assert item.hooks == (
        HookDefinition(PRE_TOOL_USE, ("sh", "-c", '"$MELTZER_PROJECT_DIR"/.claude/hooks/check.sh'), matcher="shell|exec_command"),
        HookDefinition(POST_TOOL_USE, ("sh", "-c", "ruff format"), matcher="apply_patch"),
        HookDefinition(SESSION_START, ("sh", "-c", "git status")),
        HookDefinition(STOP, ("sh", "-c", "notify-send done")),
    )
    import_item(item)
    config = load_config(root, home=home)
    assert config.hooks_pre_tool_use == item.hooks[:1]
    assert config.hooks_post_tool_use == item.hooks[1:2]
    assert config.hooks_session_start == item.hooks[2:3]
    assert config.hooks_stop == item.hooks[3:]


def test_hooks_already_configured_are_not_added_again(home, root):
    write_text(user_config_path(home), 'model = "gpt-5"\n\n[[hooks.Stop]]\ncommand = ["sh", "-c", "make lint"]\n')
    write_json(home / ".claude" / "settings.json", {"hooks": {
        "Stop": [{"hooks": [command_hook("make lint"), command_hook("make test")]}],
    }})
    item = only_item(home, root, ItemType.HOOKS)
    assert [hook.command for hook in item.hooks] == [("sh", "-c", "make test")]
    import_item(item)
    data = read_toml(user_config_path(home))
    assert data["model"] == "gpt-5"
    assert [entry["command"] for entry in data["hooks"]["Stop"]] == [["sh", "-c", "make lint"], ["sh", "-c", "make test"]]
    assert items_of(home, root, ItemType.HOOKS) == []


def test_hooks_that_cannot_be_converted_are_listed_as_skipped(home, root):
    write_json(home / ".claude" / "settings.json", {"hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "prompt", "prompt": "is this safe?"}]},
            {"matcher": "mcp__.*", "hooks": [command_hook("audit.sh")]},
            {"matcher": "Bash", "hooks": [command_hook("bg.sh", **{"async": True})]},
        ],
        "Notification": [{"hooks": [command_hook("notify.sh")]}],
        "SessionStart": [{"matcher": "resume", "hooks": [command_hook("resume.sh")]}],
        "UserPromptSubmit": [{"hooks": [command_hook("log-prompt.sh")]}],
    }})
    item = only_item(home, root, ItemType.HOOKS)
    assert [hook.command[-1] for hook in item.hooks] == ["log-prompt.sh"]
    skipped = "\n".join(item.skipped)
    for text in ("prompt", "mcp__.*", "async", "Notification", "resume"):
        assert text in skipped


@pytest.mark.parametrize(
    "files",
    [
        {},
        {"settings.json": {"permissions": {"allow": ["Bash(git push:*)"], "deny": []}}},
        {"settings.json": {"disableAllHooks": True, "hooks": {"Stop": [{"hooks": [command_hook("make test")]}]}}},
    ],
)
def test_nothing_to_import(home, root, files):
    for name, content in files.items():
        (write_text if isinstance(content, str) else write_json)(home / ".claude" / name, content)
    assert detect(home, root).items == []


def test_imported_hooks_are_not_trusted(home, root):
    write_json(home / ".claude" / "settings.json", {"hooks": {"Stop": [{"hooks": [command_hook("make test")]}]}})
    import_item(only_item(home, root, ItemType.HOOKS))
    store = TrustStore(home / ".meltzer" / "trusted_hooks.toml")
    hook = load_config(root, home=home).hooks_stop[0].resolve(root)
    assert not store.is_trusted(hook)
    assert not store.path.exists()


def test_project_hooks_from_both_settings_files_go_to_the_project_config(home, root):
    write_json(root / ".claude" / "settings.json", {"hooks": {"Stop": [{"hooks": [command_hook("make test")]}]}})
    write_json(root / ".claude" / "settings.local.json", {"hooks": {"Stop": [{"hooks": [command_hook("make lint")]}]}})
    item = only_item(home, root, ItemType.HOOKS)
    assert item.sources == (root / ".claude" / "settings.json", root / ".claude" / "settings.local.json")
    assert item.target == root / ".meltzer" / "config.toml"
    import_item(item)
    commands = [entry["command"][-1] for entry in read_toml(item.target)["hooks"]["Stop"]]
    assert commands == ["make test", "make lint"]


def test_project_items_use_the_existing_project_config(home, root):
    write_text(root / ".meltzer" / "config.toml", 'model = "gpt-5"\n')
    write_json(root / "pkg" / ".claude" / "settings.json", {"hooks": {"Stop": [{"hooks": [command_hook("make test")]}]}})
    assert only_item(home, root / "pkg", ItemType.HOOKS).target == root / ".meltzer" / "config.toml"
    write_json(home / ".claude" / "settings.json", {"hooks": {"Stop": [{"hooks": [command_hook("make lint")]}]}})
    items = items_of(home, home, ItemType.HOOKS)
    assert len(items) == 1 and items[0].target == user_config_path(home)


def test_invalid_settings_file_is_reported(home, root):
    write_text(home / ".claude" / "settings.json", "{not json")
    detection = detect(home, root)
    assert detection.items == [] and any("settings.json" in note for note in detection.notes)


def test_config_that_cannot_take_the_new_tables_is_left_unchanged(home, root):
    original = "hooks = { Stop = [] }\n"
    write_text(user_config_path(home), original)
    write_json(home / ".claude" / "settings.json", {"hooks": {"Stop": [{"hooks": [command_hook("make test")]}]}})
    item = only_item(home, root, ItemType.HOOKS)
    with pytest.raises(MigrationError, match="edit it by hand"):
        import_item(item)
    assert user_config_path(home).read_text(encoding="utf-8") == original


# Sandbox setting


def test_sandbox_enabled_sets_the_sandbox_mode(home, root):
    write_text(user_config_path(home), '[projects."/src/app"]\ntrust_level = "trusted"\n')
    write_json(home / ".claude" / "settings.json", {"sandbox": {"enabled": True}})
    item = only_item(home, root, ItemType.SANDBOX)
    assert item.settings == {"sandbox_mode": "workspace-write"}
    import_item(item)
    data = read_toml(user_config_path(home))
    assert data["sandbox_mode"] == "workspace-write"
    assert data["projects"] == {"/src/app": {"trust_level": "trusted"}}
    assert items_of(home, root, ItemType.SANDBOX) == []


@pytest.mark.parametrize(
    "project, local",
    [({"sandbox": {"enabled": True}}, None), ({"sandbox": {"enabled": True}}, {"sandbox": {"enabled": False}})],
)
def test_sandbox_mode_already_set_and_local_settings_decide(home, root, project, local):
    write_text(root / ".meltzer" / "config.toml", 'sandbox_mode = "read-only"\n' if local is None else "")
    write_json(root / ".claude" / "settings.json", project)
    if local is not None:
        write_json(root / ".claude" / "settings.local.json", local)
    assert items_of(home, root, ItemType.SANDBOX) == []


# MCP servers


def test_stdio_servers_are_imported_into_the_project_config(home, root):
    write_json(root / ".mcp.json", {"mcpServers": {
        "db": {"type": "stdio", "command": "python3", "args": ["tools/db.py"],
               "env": {"DB_URL": "sqlite:///x.db", "GITHUB_TOKEN": "${GITHUB_TOKEN}"}},
        "docs": {"command": "npx", "args": ["docs-server"]},
    }})
    item = only_item(home, root, ItemType.MCP_SERVERS)
    assert item.target == root / ".meltzer" / "config.toml"
    import_item(item)
    servers = read_toml(item.target)["mcp_servers"]
    assert servers["db"] == {
        "command": "python3",
        "args": ["tools/db.py"],
        "env": {"DB_URL": "sqlite:///x.db"},
        "env_vars": ["GITHUB_TOKEN"],
    }
    assert servers["docs"] == {"command": "npx", "args": ["docs-server"]}


def test_servers_that_cannot_be_imported_are_listed_as_skipped(home, root):
    write_json(root / ".mcp.json", {"mcpServers": {
        "remote": {"type": "http", "url": "https://mcp.example.com"},
        "fallback": {"command": "srv", "env": {"MODE": "${MODE:-fast}"}},
        "bad name": {"command": "srv"},
        "off": {"command": "srv", "disabled": True},
        "listed-off": {"command": "srv"},
        "ok": {"command": "srv"},
    }})
    write_json(root / ".claude" / "settings.json", {"disabledMcpjsonServers": ["listed-off"]})
    item = only_item(home, root, ItemType.MCP_SERVERS)
    assert list(item.servers) == ["ok"]
    skipped = "\n".join(item.skipped)
    for name in ("remote", "fallback", "bad name", "off", "listed-off"):
        assert name in skipped


def test_server_names_in_use_are_kept(home, root):
    write_text(user_config_path(home), '[mcp_servers.db]\ncommand = "mine"\n')
    write_text(root / ".meltzer" / "config.toml", '[mcp_servers.docs]\ncommand = "ours"\n')
    write_json(root / ".mcp.json", {"mcpServers": {
        "db": {"command": "theirs"}, "docs": {"command": "theirs"}, "new": {"command": "srv"},
    }})
    item = only_item(home, root, ItemType.MCP_SERVERS)
    assert list(item.servers) == ["new"]
    import_item(item)
    servers = read_toml(root / ".meltzer" / "config.toml")["mcp_servers"]
    assert servers["docs"] == {"command": "ours"} and servers["new"] == {"command": "srv"}


def test_imported_servers_are_reviewed_through_the_user_config(home, root):
    write_json(root / ".mcp.json", {"mcpServers": {"db": {"command": "srv", "default_tools_approval_mode": "approve"}}})
    import_item(only_item(home, root, ItemType.MCP_SERVERS))
    assert "approval_mode" not in (root / ".meltzer" / "config.toml").read_text(encoding="utf-8")
    assert read_toml(user_config_path(home))["mcp_servers"] == {"db": {"default_tools_approval_mode": "prompt"}}
    assert parse_mcp_servers(load_config(root, home=home).mcp_servers) == ()
    write_text(user_config_path(home), user_config_path(home).read_text(encoding="utf-8") + trusted(root))
    (server,) = parse_mcp_servers(load_config(root, home=home).mcp_servers)
    assert server.command == "srv" and server.default_tools_approval_mode == "prompt"


@pytest.mark.skipif(sys.platform == "win32", reason="needs symbolic links")
def test_files_are_not_written_through_symbolic_links(home, root, tmp_path):
    victim = write_text(tmp_path / "victim.txt", "mine")
    write_text(root / "CLAUDE.md", "rules")
    (root / "AGENTS.md").symlink_to(tmp_path / "missing")
    (root / ".AGENTS.md.tmp").symlink_to(victim)
    item = only_item(home, root, ItemType.AGENTS_MD)
    with pytest.raises(MigrationError, match="symbolic link"):
        import_item(item)
    assert victim.read_text(encoding="utf-8") == "mine"


# Memories


def test_memory_files_are_copied(home, root):
    memory = home / ".claude" / "projects" / "-src-app" / "memory"
    write_text(memory / "MEMORY.md", "- [Style](style.md)\n")
    write_text(memory / "style.md", "Use tabs.\n")
    write_text(memory / "notes.txt", "ignored")
    item = only_item(home, root, ItemType.MEMORIES)
    target = home / ".meltzer" / "memories" / "imported" / "-src-app"
    assert item.target == target
    import_item(item)
    assert sorted(path.name for path in target.iterdir()) == ["MEMORY.md", "style.md"]
    assert (target / "style.md").read_text(encoding="utf-8") == "Use tabs.\n"
    write_text(memory / "build.md", "make\n")
    write_text(target / "style.md", "Use spaces.\n")
    import_item(only_item(home, root, ItemType.MEMORIES))
    assert (target / "style.md").read_text(encoding="utf-8") == "Use spaces.\n"
    assert (target / "build.md").read_text(encoding="utf-8") == "make\n"


# Command


def migrate(argv: list[str], home: Path, answers: str = "", terminal: bool = True) -> tuple[int, str, str]:
    stdin = Terminal(answers) if terminal else io.StringIO(answers)
    stdout = Terminal() if terminal else io.StringIO()
    stderr = io.StringIO()
    code = run_migrate(argv, home=home, stdin=stdin, stdout=stdout, stderr=stderr)
    return code, stdout.getvalue(), stderr.getvalue()


def test_migrate_needs_a_terminal(home, root):
    write_text(root / "CLAUDE.md", "rules")
    code, _, err = migrate(["-C", str(root)], home, "y\n", terminal=False)
    assert code == 1 and "terminal" in err
    assert not (root / "AGENTS.md").exists()


def test_migrate_asks_about_each_item(home, root):
    write_text(root / "CLAUDE.md", "rules")
    write_json(home / ".claude" / "settings.json", {"hooks": {"Stop": [{"hooks": [command_hook("make test")]}]}})
    code, out, _ = migrate(["-C", str(root)], home, "n\ny\n")
    assert code == 0
    assert not (root / "AGENTS.md").exists()
    assert read_toml(user_config_path(home))["hooks"]["Stop"][0]["command"] == ["sh", "-c", "make test"]
    assert out.count("Import this item?") == 2
    assert "trust" in out


def test_migrate_dry_run_writes_nothing(home, root):
    write_text(root / "CLAUDE.md", "rules")
    write_json(root / ".mcp.json", {"mcpServers": {"db": {"command": "srv", "env": {"NODE_OPTIONS": "--require x.js", "A": "${A}"}}}})
    code, out, _ = migrate(["--dry-run", "-C", str(root)], home)
    assert code == 0
    assert "AGENTS.md" in out and "db: srv; env NODE_OPTIONS=--require x.js; env_vars A" in out
    assert "Import this item?" not in out
    assert not (root / "AGENTS.md").exists()
    assert not (root / ".meltzer").exists()


def test_migrate_with_nothing_to_import(home, root):
    code, out, _ = migrate(["-C", str(root)], home)
    assert code == 0 and "nothing to import" in out


def test_migrate_stops_at_end_of_input(home, root):
    write_text(root / "CLAUDE.md", "rules")
    write_json(root / ".mcp.json", {"mcpServers": {"db": {"command": "srv"}}})
    assert migrate(["-C", str(root)], home, "")[0] == 0
    assert not (root / "AGENTS.md").exists() and not (root / ".meltzer").exists()


def test_migrate_reports_a_failed_item_and_goes_on(home, root):
    write_text(user_config_path(home), "hooks = { Stop = [] }\n")
    write_json(home / ".claude" / "settings.json", {"hooks": {"Stop": [{"hooks": [command_hook("make test")]}]}})
    write_text(root / "CLAUDE.md", "rules")
    code, _, err = migrate(["-C", str(root)], home, "y\ny\n")
    assert code == 1 and "edit it by hand" in err
    assert (root / "AGENTS.md").read_text(encoding="utf-8") == "rules\n"
