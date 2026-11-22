"""The [mcp_servers.<name>] settings.

    [mcp_servers.db]
    command = "python3"                 # program that runs the server (stdio)
    args = ["tools/db_mcp.py"]
    env = { DB_URL = "sqlite:///x.db" } # set in the server's environment
    env_vars = ["GITHUB_TOKEN"]         # copied from meltzer's environment
    cwd = "tools"                       # relative to the session root (default: the root)
    enabled = true
    required = false                    # true: the session does not start without it
    startup_timeout_sec = 30
    tool_timeout_sec = 300
    enabled_tools = ["query"]           # only these tools (default: all)
    disabled_tools = ["drop"]           # never these tools
    default_tools_approval_mode = "auto"

    [mcp_servers.db.tools.query]
    approval_mode = "approve"           # auto | prompt | writes | approve

Servers are local programs that meltzer talks to over stdin and stdout.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from meltzer.config.loader import ConfigError

DEFAULT_STARTUP_TIMEOUT_SECONDS = 30.0
DEFAULT_TOOL_TIMEOUT_SECONDS = 300.0

SERVER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

SERVER_FIELDS = (
    "command",
    "args",
    "env",
    "env_vars",
    "cwd",
    "enabled",
    "required",
    "startup_timeout_sec",
    "tool_timeout_sec",
    "enabled_tools",
    "disabled_tools",
    "default_tools_approval_mode",
    "tools",
)
# Settings that only the user config sets.
APPROVAL_FIELDS = frozenset({"default_tools_approval_mode", "tools"})
# Settings of servers reached over HTTP.
HTTP_FIELDS = (
    "url",
    "bearer_token",
    "bearer_token_env_var",
    "http_headers",
    "env_http_headers",
    "auth",
    "oauth",
    "oauth_resource",
    "scopes",
)


class ToolApprovalMode(StrEnum):
    # Follow what the server says about the tool (readOnlyHint, destructiveHint, openWorldHint).
    AUTO = "auto"
    # Review every call.
    PROMPT = "prompt"
    # Review every call unless the server marks the tool read-only.
    WRITES = "writes"
    # Never review calls.
    APPROVE = "approve"


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    # Names of variables copied from meltzer's environment, when set.
    env_vars: tuple[str, ...] = ()
    cwd: str | None = None
    enabled: bool = True
    required: bool = False
    startup_timeout_sec: float = DEFAULT_STARTUP_TIMEOUT_SECONDS
    tool_timeout_sec: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    # None: every tool the server lists.
    enabled_tools: tuple[str, ...] | None = None
    disabled_tools: tuple[str, ...] = ()
    default_tools_approval_mode: ToolApprovalMode = ToolApprovalMode.AUTO
    # Approval mode per tool name, from [mcp_servers.<name>.tools.<tool>].
    tool_approval_modes: dict[str, ToolApprovalMode] = field(default_factory=dict)

    def allows_tool(self, tool: str) -> bool:
        """Whether a tool the server lists is offered to the model: `enabled_tools` first, then `disabled_tools`."""
        if self.enabled_tools is not None and tool not in self.enabled_tools:
            return False
        return tool not in self.disabled_tools

    def approval_mode(self, tool: str) -> ToolApprovalMode:
        return self.tool_approval_modes.get(tool, self.default_tools_approval_mode)


class _FieldError(Exception):
    """A server setting is invalid; the message names the field."""


def _string(entry: dict[str, Any], key: str) -> str | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise _FieldError(f"'{key}' must be a non-empty string, got {value!r}")
    return value


def _strings(entry: dict[str, Any], key: str) -> tuple[str, ...] | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise _FieldError(f"'{key}' must be a list of non-empty strings, got {value!r}")
    return tuple(value)


def _bool(entry: dict[str, Any], key: str, default: bool) -> bool:
    value = entry.get(key, default)
    if not isinstance(value, bool):
        raise _FieldError(f"'{key}' must be true or false, got {value!r}")
    return value


def _seconds(entry: dict[str, Any], key: str, default: float) -> float:
    value = entry.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise _FieldError(f"'{key}' must be a positive number of seconds, got {value!r}")
    return float(value)


def _env(entry: dict[str, Any]) -> dict[str, str]:
    value = entry.get("env", {})
    if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
        raise _FieldError(f"'env' must be a table of strings, got {value!r}")
    return dict(value)


def _env_vars(entry: dict[str, Any]) -> tuple[str, ...]:
    """Variable names, given as strings or as tables {name = "...", source = "local"}."""
    value = entry.get("env_vars", [])
    if not isinstance(value, list):
        raise _FieldError(f"'env_vars' must be a list, got {value!r}")
    names = []
    for item in value:
        if isinstance(item, dict):
            source = item.get("source", "local")
            if source != "local":
                raise _FieldError(f"'env_vars' entry {item!r}: unsupported source {source!r}; expected 'local'")
            if set(item) - {"name", "source"}:
                raise _FieldError(f"'env_vars' entry {item!r}: expected only 'name' and 'source'")
            item = item.get("name")
        if not isinstance(item, str) or not item:
            raise _FieldError(f"'env_vars' entries must be variable names, got {item!r}")
        names.append(item)
    return tuple(names)


def _approval_mode(value: Any, key: str) -> ToolApprovalMode:
    try:
        return ToolApprovalMode(value)
    except ValueError:
        allowed = ", ".join(mode.value for mode in ToolApprovalMode)
        raise _FieldError(f"'{key}' must be one of: {allowed}; got {value!r}") from None


def _tool_approval_modes(entry: dict[str, Any]) -> dict[str, ToolApprovalMode]:
    tools = entry.get("tools", {})
    if not isinstance(tools, dict):
        raise _FieldError(f"'tools' must be a table of tool names, got {tools!r}")
    modes = {}
    for tool, settings in tools.items():
        if not isinstance(settings, dict):
            raise _FieldError(f"'tools.{tool}' must be a table, got {settings!r}")
        unknown = sorted(set(settings) - {"approval_mode"})
        if unknown:
            raise _FieldError(f"'tools.{tool}': unknown field '{unknown[0]}'")
        if "approval_mode" in settings:
            modes[tool] = _approval_mode(settings["approval_mode"], f"tools.{tool}.approval_mode")
    return modes


def _parse_server(name: str, entry: Any) -> McpServerConfig:
    if not isinstance(entry, dict):
        raise _FieldError(f"expected a table, got {entry!r}")
    http = [key for key in HTTP_FIELDS if key in entry]
    if http:
        raise _FieldError(
            f"HTTP servers are not supported ('{http[0]}'); meltzer starts local servers with 'command'"
        )
    unknown = sorted(set(entry) - set(SERVER_FIELDS))
    if unknown:
        raise _FieldError(f"unknown field '{unknown[0]}'")
    if "command" not in entry:
        raise _FieldError("'command' is required")
    return McpServerConfig(
        name=name,
        command=_string(entry, "command") or "",
        args=_strings(entry, "args") or (),
        env=_env(entry),
        env_vars=_env_vars(entry),
        cwd=_string(entry, "cwd"),
        enabled=_bool(entry, "enabled", True),
        required=_bool(entry, "required", False),
        startup_timeout_sec=_seconds(entry, "startup_timeout_sec", DEFAULT_STARTUP_TIMEOUT_SECONDS),
        tool_timeout_sec=_seconds(entry, "tool_timeout_sec", DEFAULT_TOOL_TIMEOUT_SECONDS),
        enabled_tools=_strings(entry, "enabled_tools"),
        disabled_tools=_strings(entry, "disabled_tools") or (),
        default_tools_approval_mode=_approval_mode(
            entry.get("default_tools_approval_mode", ToolApprovalMode.AUTO.value), "default_tools_approval_mode"
        ),
        tool_approval_modes=_tool_approval_modes(entry),
    )


def parse_mcp_servers(value: dict[str, Any]) -> tuple[McpServerConfig, ...]:
    """Check the `mcp_servers` table (already merged across config layers). Raises ConfigError.

    An entry that has only approval modes sets them for a server of the same name that another config layer
    defines; it starts nothing by itself.
    """
    servers = []
    for name, entry in value.items():
        if isinstance(entry, dict) and "command" not in entry and entry and set(entry) <= APPROVAL_FIELDS:
            continue
        if not SERVER_NAME_PATTERN.match(name):
            raise ConfigError(
                f"mcp_servers.{name}: invalid server name; use letters, digits, '_' and '-'"
            )
        try:
            servers.append(_parse_server(name, entry))
        except _FieldError as exc:
            raise ConfigError(f"mcp_servers.{name}: {exc}") from None
    return tuple(servers)
