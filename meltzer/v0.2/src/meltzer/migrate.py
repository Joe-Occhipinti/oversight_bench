"""Imports Claude Code settings: `meltzer migrate [--dry-run] [-C DIR]`.

Items, from where to where (DIR is the directory given with -C, or the
current directory):

  DIR/CLAUDE.md                     -> DIR/AGENTS.md, when that is missing or empty;
                                       "CLAUDE.md" and "Claude Code" are renamed
  ~/.claude/settings.json           -> ~/.meltzer/config.toml
      command hooks                 -> [[hooks.<Event>]]
      sandbox.enabled = true        -> sandbox_mode = "workspace-write"
  DIR/.claude/settings.json and settings.local.json
                                    -> the project config: the nearest .meltzer/config.toml,
                                       or DIR/.meltzer/config.toml
  DIR/.mcp.json                     -> [mcp_servers.<name>] in the project config (local servers),
                                       and default_tools_approval_mode = "prompt" for each in the user config
  ~/.claude/projects/<key>/memory/*.md
                                    -> ~/.meltzer/memories/imported/<key>/

Hooks of the events PreToolUse, PostToolUse, SessionStart, Stop,
UserPromptSubmit and SessionEnd are imported; each runs as `sh -c <command>`.
Claude Code tool names in matchers become meltzer tool names.

The command lists what it found and asks about each item; it runs only in a
terminal. Config files only gain entries: hooks already configured, keys
already set and server names already in use are left as they are, and a file
is replaced only once the result reads back as intended. Imported hooks run
once the user trusts them, as any other hook.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, TextIO

from meltzer.config.loader import (
    CONFIG_DIR_NAME,
    CONFIG_FILE_NAME,
    ConfigError,
    find_project_config,
    meltzer_home,
    user_config_path,
)
from meltzer.config.trust import table_path
from meltzer.hooks import (
    MATCH_ALL,
    POST_TOOL_USE,
    PRE_TOOL_USE,
    SESSION_END,
    SESSION_START,
    STOP,
    USER_PROMPT_SUBMIT,
    HookConfigError,
    HookDefinition,
    parse_hook_entries,
)
from meltzer.mcp.config import SERVER_NAME_PATTERN, ToolApprovalMode, parse_mcp_servers
from meltzer.memories import IMPORTED_DIR_NAME, MEMORIES_DIR_NAME
from meltzer.sandbox.policy import resolve
from meltzer.tomlwrite import toml_string

EXIT_OK = 0
EXIT_ERROR = 1

CLAUDE_DIR = ".claude"
CLAUDE_MD = "CLAUDE.md"
AGENTS_MD = "AGENTS.md"
SETTINGS_FILE = "settings.json"
LOCAL_SETTINGS_FILE = "settings.local.json"
MCP_FILE = ".mcp.json"
PROJECTS_DIR = "projects"
MEMORY_DIR = "memory"
IMPORTED_MEMORIES_DIR = Path(MEMORIES_DIR_NAME) / IMPORTED_DIR_NAME

PRODUCT_NAME = "Claude Code"
PRODUCT = "meltzer"
PROJECT_DIR_VARIABLE = re.compile(r"\bCLAUDE_PROJECT_DIR\b")

IMPORTED_EVENTS = (PRE_TOOL_USE, POST_TOOL_USE, SESSION_START, STOP, USER_PROMPT_SUBMIT, SESSION_END)
TOOL_MATCHER_EVENTS = frozenset({PRE_TOOL_USE, POST_TOOL_USE})
# meltzer tools for Claude Code tool names; other names are kept.
TOOL_NAMES = {
    "Bash": ("shell", "exec_command"),
    "Edit": ("apply_patch",),
    "MultiEdit": ("apply_patch",),
    "Write": ("apply_patch",),
}
MATCH_ALL_PATTERNS = frozenset({"", MATCH_ALL, ".*"})
TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
HOOK_FIELDS = frozenset({"type", "command", "timeout", "statusMessage", "async"})
HOOK_SHELL = ("sh", "-c")

SANDBOX_MODE_KEY = "sandbox_mode"
WORKSPACE_WRITE = "workspace-write"
HOOKS_KEY = "hooks"
MCP_SERVERS_KEY = "mcp_servers"
MCP_DEFAULT_APPROVAL_KEY = "default_tools_approval_mode"
BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


class MigrationError(Exception):
    """An item cannot be imported; nothing was written for it."""


class ItemType(StrEnum):
    AGENTS_MD = "AGENTS.md"
    HOOKS = "hooks"
    SANDBOX = "sandbox setting"
    MCP_SERVERS = "MCP servers"
    MEMORIES = "memories"


@dataclass(frozen=True)
class MigrationItem:
    """Something that can be imported, with what it would add to its target."""

    type: ItemType
    sources: tuple[Path, ...]
    target: Path
    # Entries the item adds, one line each.
    entries: tuple[str, ...]
    # Entries of the sources that cannot be imported, with the reason.
    skipped: tuple[str, ...] = ()
    text: str = ""
    settings: dict[str, str] = field(default_factory=dict)
    hooks: tuple[HookDefinition, ...] = ()
    servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Configs whose server names are in use besides the target.
    other_configs: tuple[Path, ...] = ()
    # MCP servers: the user config, which sets their approval mode.
    user_config: Path | None = None
    files: tuple[tuple[Path, Path], ...] = ()

    def heading(self) -> str:
        sources = ", ".join(str(path) for path in self.sources)
        return f"{self.type}: {sources} -> {self.target}"


@dataclass
class Detection:
    items: list[MigrationItem] = field(default_factory=list)
    # Files that could not be read, and entries that cannot be imported outside any item.
    notes: list[str] = field(default_factory=list)


# Instructions


def _replace_word(text: str, word: str, replacement: str) -> str:
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(word)}(?![A-Za-z0-9_])", re.IGNORECASE)
    return pattern.sub(replacement, text)


def rewrite_instructions(text: str) -> str:
    """Instructions written for Claude Code, with the file and product names of meltzer."""
    text = _replace_word(text, CLAUDE_MD, AGENTS_MD)
    return _replace_word(text, PRODUCT_NAME, PRODUCT)


def _is_missing_or_empty(path: Path) -> bool:
    if not path.exists():
        return True
    try:
        return path.is_file() and not path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return False


# Claude Code files


def _read_json(path: Path, notes: list[str]) -> dict[str, Any] | None:
    """The JSON object in path; None, with a note, when the file is not one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        notes.append(f"cannot read {path}: {exc}")
        return None
    if not isinstance(data, dict):
        notes.append(f"cannot read {path}: expected a JSON object")
        return None
    return data


def _read_settings(paths: list[Path], notes: list[str]) -> list[tuple[Path, dict[str, Any]]]:
    settings = []
    for path in paths:
        if path.is_file():
            data = _read_json(path, notes)
            if data is not None:
                settings.append((path, data))
    return settings


def _last_value(settings: list[tuple[Path, dict[str, Any]]], *keys: str) -> Any:
    """The value at keys in the last settings file that sets it."""
    found = None
    for _, data in settings:
        value: Any = data
        for key in keys:
            value = value.get(key) if isinstance(value, dict) else None
        if value is not None:
            found = value
    return found


def _matcher(event: str, raw: Any) -> tuple[str | None, str]:
    """The meltzer matcher for a Claude Code matcher, or (None, reason)."""
    if raw is not None and not isinstance(raw, str):
        return None, f"matcher {raw!r} is not a string"
    text = (raw or "").strip()
    if text in MATCH_ALL_PATTERNS:
        return MATCH_ALL, ""
    parts = [part.strip() for part in text.split("|")]
    if event in TOOL_MATCHER_EVENTS:
        names: list[str] = []
        for part in parts:
            if not TOOL_NAME.match(part):
                return None, f"matcher {raw!r} is a pattern; meltzer matchers are tool names"
            for name in TOOL_NAMES.get(part, (part,)):
                if name not in names:
                    names.append(name)
        return "|".join(names), ""
    if event == SESSION_START and "startup" in parts:
        return MATCH_ALL, ""
    return None, f"matcher {raw!r} has no meltzer equivalent"


def _hook_command(entry: Any) -> tuple[tuple[str, ...] | None, str]:
    """The command of a Claude Code hook entry, or (None, reason)."""
    if not isinstance(entry, dict):
        return None, f"{entry!r} is not an object"
    kind = entry.get("type", "command")
    if kind != "command":
        return None, f"{kind!r} hooks are not supported; only command hooks are imported"
    unknown = sorted(set(entry) - HOOK_FIELDS)
    if unknown:
        return None, f"unsupported field(s) {', '.join(unknown)}"
    if entry.get("async") is True:
        return None, "async hooks are not supported"
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip():
        return None, "no command"
    return (*HOOK_SHELL, PROJECT_DIR_VARIABLE.sub("MELTZER_PROJECT_DIR", command.strip())), ""


def convert_hooks(settings: list[tuple[Path, dict[str, Any]]]) -> tuple[list[HookDefinition], list[str]]:
    """The command hooks of the settings files as meltzer hooks. Returns (hooks, skipped)."""
    hooks: list[HookDefinition] = []
    skipped: list[str] = []
    if _last_value(settings, "disableAllHooks") is True:
        return hooks, skipped
    for path, data in settings:
        events = data.get(HOOKS_KEY)
        if events is None:
            continue
        if not isinstance(events, dict):
            skipped.append(f"{path}: 'hooks' is not an object")
            continue
        for event in events:
            if event not in IMPORTED_EVENTS:
                skipped.append(f"{path}: {event} hooks are not imported")
        for event in IMPORTED_EVENTS:
            groups = events.get(event)
            if groups is None:
                continue
            if not isinstance(groups, list):
                skipped.append(f"{path}: {event}: expected a list of matcher groups")
                continue
            for group in groups:
                hooks.extend(_convert_group(path, event, group, skipped))
    return hooks, skipped


def _convert_group(path: Path, event: str, group: Any, skipped: list[str]) -> list[HookDefinition]:
    if not isinstance(group, dict) or set(group) - {"matcher", "hooks"}:
        skipped.append(f"{path}: {event}: unsupported matcher group {group!r}")
        return []
    matcher, reason = _matcher(event, group.get("matcher"))
    entries = group.get("hooks")
    if not isinstance(entries, list):
        skipped.append(f"{path}: {event}: 'hooks' is not a list")
        return []
    hooks = []
    for entry in entries:
        command, why = _hook_command(entry)
        if command is None or matcher is None:
            label = entry.get("command", entry) if isinstance(entry, dict) else entry
            skipped.append(f"{path}: {event} hook {label!r}: {why or reason}")
            continue
        hooks.append(HookDefinition(event=event, command=command, matcher=matcher))
    return hooks


def _placeholder(value: str) -> str | None:
    """The variable name of a value that is exactly ${NAME}, else None."""
    match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
    return match.group(1) if match else None


def _convert_server(
    name: str, entry: Any, disabled: set[str], enabled: list[str]
) -> tuple[dict[str, Any] | None, str]:
    """A [mcp_servers.<name>] entry for a server of .mcp.json, or (None, reason)."""
    if not SERVER_NAME_PATTERN.match(name):
        return None, "invalid server name; meltzer names use letters, digits, '_' and '-'"
    if not isinstance(entry, dict):
        return None, "not an object"
    if entry.get("disabled") is True or entry.get("enabled") is False or name in disabled:
        return None, "disabled"
    if enabled and name not in enabled:
        return None, "not in enabledMcpjsonServers"
    if entry.get("type", "stdio") != "stdio" or "command" not in entry:
        return None, "only local servers started with a command are imported"
    command = entry["command"]
    args = entry.get("args", [])
    if not isinstance(command, str) or not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return None, "'command' must be a string and 'args' a list of strings"
    if "${" in command or any("${" in arg for arg in args):
        return None, "command uses ${...} expansion"
    env: dict[str, str] = {}
    env_vars: list[str] = []
    raw_env = entry.get("env", {})
    if not isinstance(raw_env, dict):
        return None, "'env' is not an object"
    for key, value in raw_env.items():
        if not isinstance(value, str):
            return None, f"env {key} is not a string"
        if _placeholder(value) == key:
            env_vars.append(key)
        elif "${" in value:
            return None, f"env {key} uses ${{...}} expansion"
        else:
            env[key] = value
    server: dict[str, Any] = {"command": command}
    if args:
        server["args"] = list(args)
    if env:
        server["env"] = env
    if env_vars:
        server["env_vars"] = env_vars
    try:
        parse_mcp_servers({name: server})
    except ConfigError as exc:
        return None, str(exc)
    return server, ""


def convert_mcp_servers(
    data: dict[str, Any], settings: list[tuple[Path, dict[str, Any]]]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """The local servers of a .mcp.json file. Returns (servers by name, skipped)."""
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return {}, ["'mcpServers' is not an object"]
    disabled = _last_value(settings, "disabledMcpjsonServers")
    enabled = _last_value(settings, "enabledMcpjsonServers")
    disabled_names = {n for n in disabled if isinstance(n, str)} if isinstance(disabled, list) else set()
    enabled_names = [n for n in enabled if isinstance(n, str)] if isinstance(enabled, list) else []
    converted: dict[str, dict[str, Any]] = {}
    skipped: list[str] = []
    for name, entry in servers.items():
        server, reason = _convert_server(name, entry, disabled_names, enabled_names)
        if server is None:
            skipped.append(f"server {name!r}: {reason}")
        else:
            converted[name] = server
    return converted, skipped


# meltzer config files


def _read_config(path: Path) -> tuple[str, dict[str, Any]]:
    if not path.is_file():
        return "", {}
    try:
        text = path.read_text(encoding="utf-8")
        return text, tomllib.loads(text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise MigrationError(f"cannot read {path}: {exc}") from None


def _server_names(path: Path) -> set[str]:
    servers = _read_config(path)[1].get(MCP_SERVERS_KEY, {})
    return set(servers) if isinstance(servers, dict) else set()


@dataclass
class _Additions:
    settings: dict[str, str] = field(default_factory=dict)
    hooks: list[HookDefinition] = field(default_factory=list)
    servers: dict[str, dict[str, Any]] = field(default_factory=dict)


def _new_entries(path: Path, data: dict[str, Any], item: MigrationItem) -> _Additions:
    """The settings, hooks and servers of item that the config in path does not have yet."""
    additions = _Additions()
    for key, value in item.settings.items():
        if key not in data:
            additions.settings[key] = value
    configured = data.get(HOOKS_KEY, {})
    if item.hooks and not isinstance(configured, dict):
        raise MigrationError(f"{path}: '{HOOKS_KEY}' must be a table")
    known: set[HookDefinition] = set()
    for event in {hook.event for hook in item.hooks}:
        try:
            known.update(parse_hook_entries(event, configured.get(event, [])))
        except HookConfigError as exc:
            raise MigrationError(f"{path}: hooks.{event}: {exc}") from None
    for hook in item.hooks:
        if hook not in known:
            additions.hooks.append(hook)
            known.add(hook)
    if not item.servers:
        return additions
    configured_servers = data.get(MCP_SERVERS_KEY, {})
    if not isinstance(configured_servers, dict):
        raise MigrationError(f"{path}: '{MCP_SERVERS_KEY}' must be a table")
    taken = set(configured_servers)
    for other in item.other_configs:
        if resolve(other) != resolve(path):
            taken |= _server_names(other)
    for name, server in item.servers.items():
        if name not in taken:
            additions.servers[name] = server
    return additions


def _key(name: str) -> str:
    return name if BARE_KEY.match(name) else toml_string(name)


def _value(value: Any) -> str:
    if isinstance(value, str):
        return toml_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    return "{ " + ", ".join(f"{_key(k)} = {_value(v)}" for k, v in value.items()) + " }"


def _hook_entry(hook: HookDefinition) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    if hook.event in TOOL_MATCHER_EVENTS and hook.matcher != MATCH_ALL:
        entry["matcher"] = hook.matcher
    entry["command"] = list(hook.command)
    return entry


def _render(text: str, additions: _Additions) -> str:
    """text with the top-level settings inserted before the first table and the tables appended."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    if additions.settings:
        assignments = [f"{_key(key)} = {_value(value)}\n" for key, value in additions.settings.items()]
        first_table = next((n for n, line in enumerate(lines) if table_path(line) is not None), None)
        if first_table is None:
            lines.extend(assignments)
        else:
            lines[first_table:first_table] = [*assignments, "\n"]
    tables: list[str] = []
    for hook in additions.hooks:
        body = "".join(f"{key} = {_value(value)}\n" for key, value in _hook_entry(hook).items())
        tables.append(f"[[{HOOKS_KEY}.{hook.event}]]\n{body}")
    for name, server in additions.servers.items():
        body = "".join(f"{_key(key)} = {_value(value)}\n" for key, value in server.items())
        tables.append(f"[{MCP_SERVERS_KEY}.{_key(name)}]\n{body}")
    result = "".join(lines)
    for table in tables:
        result += ("\n" if result else "") + table
    return result


def _expected(data: dict[str, Any], additions: _Additions) -> dict[str, Any]:
    expected = copy.deepcopy(data)
    expected.update(additions.settings)
    for hook in additions.hooks:
        expected.setdefault(HOOKS_KEY, {}).setdefault(hook.event, []).append(_hook_entry(hook))
    for name, server in additions.servers.items():
        expected.setdefault(MCP_SERVERS_KEY, {})[name] = copy.deepcopy(server)
    return expected


def _write_atomically(path: Path, text: str) -> None:
    """Replace path with text through a new file in the same directory. Raises MigrationError for a symbolic link."""
    if path.is_symlink() or path.parent.is_symlink():
        raise MigrationError(f"cannot write {path}: it or its directory is a symbolic link")
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(name, mode)
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def _update_config(item: MigrationItem) -> _Additions:
    """Add the new entries of item to its target config; the file is replaced only if the result reads back."""
    text, data = _read_config(item.target)
    additions = _new_entries(item.target, data, item)
    if not (additions.settings or additions.hooks or additions.servers):
        return additions
    updated = _render(text, additions)
    try:
        saved = tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        raise MigrationError(f"cannot update {item.target}; edit it by hand: {exc}") from None
    if saved != _expected(data, additions):
        raise MigrationError(f"cannot update {item.target}; edit it by hand")
    try:
        _write_atomically(item.target, updated)
    except OSError as exc:
        raise MigrationError(f"cannot write {item.target}: {exc.strerror}") from None
    return additions


# Detection


def _hook_line(hook: HookDefinition) -> str:
    matcher = f" [{hook.matcher}]" if hook.event in TOOL_MATCHER_EVENTS else ""
    return f"{hook.event}{matcher}: {hook.command[-1]}"


def _server_line(name: str, server: dict[str, Any]) -> str:
    parts = [" ".join([f"{name}:", server["command"], *server.get("args", [])])]
    if server.get("env"):
        parts.append("env " + " ".join(f"{key}={value}" for key, value in server["env"].items()))
    if server.get("env_vars"):
        parts.append("env_vars " + " ".join(server["env_vars"]))
    return "; ".join(parts)


def _config_item(kind: ItemType, sources: tuple[Path, ...], target: Path, **payload: Any) -> MigrationItem | None:
    """An item for a config target with only the entries the target does not have; None when there are none."""
    draft = MigrationItem(type=kind, sources=sources, target=target, entries=(), **payload)
    additions = _new_entries(target, _read_config(target)[1], draft)
    entries = [f"{key} = {_value(value)}" for key, value in additions.settings.items()]
    entries += [_hook_line(hook) for hook in additions.hooks]
    entries += [_server_line(name, server) for name, server in additions.servers.items()]
    if not entries:
        return None
    return MigrationItem(
        type=kind,
        sources=sources,
        target=target,
        entries=tuple(entries),
        skipped=payload.get("skipped", ()),
        settings=additions.settings,
        hooks=tuple(additions.hooks),
        servers=additions.servers,
        other_configs=payload.get("other_configs", ()),
        user_config=payload.get("user_config"),
    )


def _settings_items(
    settings: list[tuple[Path, dict[str, Any]]], target: Path, detection: Detection
) -> None:
    sources = tuple(path for path, _ in settings)
    hooks, skipped = convert_hooks(settings)
    try:
        hooks_item = _config_item(ItemType.HOOKS, sources, target, hooks=tuple(hooks), skipped=tuple(skipped))
        sandbox_item = None
        if _last_value(settings, "sandbox", "enabled") is True:
            sandbox_item = _config_item(ItemType.SANDBOX, sources, target, settings={SANDBOX_MODE_KEY: WORKSPACE_WRITE})
    except MigrationError as exc:
        detection.notes.append(str(exc))
        return
    if hooks_item is None:
        detection.notes.extend(f"not imported: {line}" for line in skipped)
    detection.items.extend(item for item in (hooks_item, sandbox_item) if item is not None)


def project_config_target(root: Path, home: Path | None = None) -> Path:
    """The project config that project items go to: the one sessions in root load, else root/.meltzer/config.toml."""
    return find_project_config(root, home) or root / CONFIG_DIR_NAME / CONFIG_FILE_NAME


def _memory_items(home: Path, detection: Detection) -> None:
    projects = home / CLAUDE_DIR / PROJECTS_DIR
    if not projects.is_dir():
        return
    for project in sorted(projects.iterdir()):
        memory = project / MEMORY_DIR
        if not memory.is_dir():
            continue
        target = meltzer_home(home) / IMPORTED_MEMORIES_DIR / project.name
        files = tuple(
            (path, target / path.name)
            for path in sorted(memory.glob("*.md"))
            if path.is_file() and not (target / path.name).exists()
        )
        if files:
            detection.items.append(
                MigrationItem(
                    type=ItemType.MEMORIES,
                    sources=(memory,),
                    target=target,
                    entries=tuple(source.name for source, _ in files),
                    files=files,
                )
            )


def detect(home: Path, root: Path) -> Detection:
    """The items that can be imported from the Claude Code files of home and root."""
    detection = Detection()

    claude_md = root / CLAUDE_MD
    agents_md = root / AGENTS_MD
    if claude_md.is_file() and _is_missing_or_empty(agents_md):
        try:
            text = claude_md.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            detection.notes.append(f"cannot read {claude_md}: {exc.strerror}")
            text = ""
        if text.strip():
            detection.items.append(
                MigrationItem(
                    type=ItemType.AGENTS_MD,
                    sources=(claude_md,),
                    target=agents_md,
                    entries=(f"{len(text.splitlines())} lines",),
                    text=rewrite_instructions(text),
                )
            )

    user_dir = home / CLAUDE_DIR
    user_settings = _read_settings([user_dir / SETTINGS_FILE], detection.notes)
    _settings_items(user_settings, user_config_path(home), detection)

    project_dir = root / CLAUDE_DIR
    project_target = project_config_target(root, home)
    project_settings: list[tuple[Path, dict[str, Any]]] = []
    if resolve(project_dir) != resolve(user_dir):
        project_settings = _read_settings(
            [project_dir / SETTINGS_FILE, project_dir / LOCAL_SETTINGS_FILE], detection.notes
        )
        _settings_items(project_settings, project_target, detection)

    mcp_file = root / MCP_FILE
    if mcp_file.is_file():
        data = _read_json(mcp_file, detection.notes)
        if data is not None:
            servers, skipped = convert_mcp_servers(data, project_settings)
            skipped = [f"{mcp_file}: {line}" for line in skipped]
            try:
                item = _config_item(
                    ItemType.MCP_SERVERS,
                    (mcp_file,),
                    project_target,
                    servers=servers,
                    skipped=tuple(skipped),
                    other_configs=(user_config_path(home),),
                    user_config=user_config_path(home),
                )
            except MigrationError as exc:
                detection.notes.append(str(exc))
                item = None
            if item is not None:
                detection.items.append(item)
            else:
                detection.notes.extend(f"not imported: {line}" for line in skipped)

    _memory_items(home, detection)
    return detection


# Import


def import_item(item: MigrationItem) -> list[str]:
    """Write one item to its target. Returns lines describing what was written. Raises MigrationError."""
    if item.type == ItemType.AGENTS_MD:
        if not _is_missing_or_empty(item.target):
            return [f"{item.target} is no longer empty; not changed"]
        text = item.text if item.text.endswith("\n") else item.text + "\n"
        try:
            _write_atomically(item.target, text)
        except OSError as exc:
            raise MigrationError(f"cannot write {item.target}: {exc.strerror}") from None
        return [f"wrote {item.target}"]
    if item.type == ItemType.MEMORIES:
        copied = []
        try:
            item.target.mkdir(parents=True, exist_ok=True)
            for source, target in item.files:
                if target.exists():
                    continue
                shutil.copyfile(source, target)
                copied.append(target.name)
        except OSError as exc:
            raise MigrationError(f"cannot copy memories to {item.target}: {exc.strerror}") from None
        return [f"copied {len(copied)} file(s) to {item.target}"]
    if item.user_config is not None:
        approvals = {name: {MCP_DEFAULT_APPROVAL_KEY: ToolApprovalMode.PROMPT.value} for name in item.servers}
        _update_config(replace(item, target=item.user_config, servers=approvals, other_configs=(), user_config=None))
        item = replace(item, other_configs=())
    additions = _update_config(item)
    count = len(additions.settings) + len(additions.hooks) + len(additions.servers)
    if not count:
        return [f"{item.target} already has these entries; not changed"]
    return [f"added {count} entr{'y' if count == 1 else 'ies'} to {item.target}"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meltzer migrate", description="Import settings from Claude Code.")
    parser.add_argument("--dry-run", action="store_true", help="list what would be imported; write nothing")
    parser.add_argument("-C", "--cd", metavar="DIR", help="import the project files of DIR")
    return parser


def _ask(stdin: TextIO, stdout: TextIO) -> bool | None:
    """Ask whether to import an item. None if input ended before an answer."""
    while True:
        print("Import this item? [y]es / [n]o: ", end="", file=stdout, flush=True)
        line = stdin.readline()
        if not line:
            print("", file=stdout)
            return None
        answer = line.strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def _show(number: int, item: MigrationItem, stdout: TextIO) -> None:
    print("", file=stdout)
    print(f"{number}. {item.heading()}", file=stdout)
    for line in item.entries:
        print(f"     {line}", file=stdout)
    for line in item.skipped:
        print(f"   not imported: {line}", file=stdout)


def run_migrate(
    argv: list[str],
    *,
    home: Path | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Entry point for `meltzer migrate ...`; argv excludes `migrate`."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    args = build_parser().parse_args(argv)
    if not (stdin.isatty() and stdout.isatty()):
        print("meltzer migrate: run it in a terminal; it asks before importing each item", file=stderr)
        return EXIT_ERROR
    home = home if home is not None else Path.home()
    root = Path(args.cd).expanduser() if args.cd else Path(os.getcwd())
    if not root.is_dir():
        print(f"meltzer migrate: directory does not exist: {root}", file=stderr)
        return EXIT_ERROR
    detection = detect(home, root.resolve())
    for note in detection.notes:
        print(f"warning: {note}", file=stderr)
    if not detection.items:
        print("nothing to import from Claude Code", file=stdout)
        return EXIT_OK
    print(f"Found {len(detection.items)} item(s) to import from Claude Code.", file=stdout)
    if args.dry_run:
        for number, item in enumerate(detection.items, 1):
            _show(number, item, stdout)
        print("\ndry run: nothing was written", file=stdout)
        return EXIT_OK
    status = EXIT_OK
    imported_hooks = False
    for number, item in enumerate(detection.items, 1):
        _show(number, item, stdout)
        answer = _ask(stdin, stdout)
        if answer is None:
            break
        if not answer:
            continue
        try:
            lines = import_item(item)
        except MigrationError as exc:
            print(f"meltzer migrate: {exc}", file=stderr)
            status = EXIT_ERROR
            continue
        for line in lines:
            print(f"   {line}", file=stdout)
        imported_hooks = imported_hooks or item.type == ItemType.HOOKS
    if imported_hooks:
        print(
            "\nImported hooks run once you trust them; the next interactive session asks about each one.",
            file=stdout,
        )
    return status
