"""The MCP servers of a session.

Every enabled server in `mcp_servers` starts when the session starts and stops
when it ends; a subagent uses the servers of its parent. Each server runs in
its own process group, with:

  environment  HOME LOGNAME PATH SHELL USER LANG LC_ALL TERM TMPDIR TZ from
               meltzer's environment, the variables named in `env_vars`, and `env`
  cwd          `cwd` (relative to the session root), or the session root
  stderr       ~/.meltzer/logs/<session>/mcp-<name>.log

A server that fails to start, or does not answer `initialize` and `tools/list`
within `startup_timeout_sec`, is reported with a notice and left out; if it is
`required`, the session does not start. A server that exits is not restarted.

Each tool is offered to the model as `mcp__<server>__<tool>`.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from meltzer.config.loader import ConfigError
from meltzer.mcp.client import McpError, StdioClient
from meltzer.mcp.config import McpServerConfig, ToolApprovalMode

DEFAULT_ENV_VARS = ("HOME", "LOGNAME", "PATH", "SHELL", "USER", "LANG", "LC_ALL", "TERM", "TMPDIR", "TZ")
WINDOWS_ENV_VARS = (
    "PATH", "PATHEXT", "SHELL", "COMSPEC", "SYSTEMROOT", "SYSTEMDRIVE", "USERNAME", "USERDOMAIN",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    "PROGRAMDATA", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP", "TMPDIR", "POWERSHELL", "PWSH",
)

TOOL_NAME_PREFIX = "mcp__"
TOOL_NAME_SEPARATOR = "__"
MAX_TOOL_NAME_CHARS = 64
HASH_SUFFIX_CHARS = 12


@dataclass(frozen=True)
class McpToolInfo:
    server: str
    # Name of the tool on its server.
    name: str
    # Name of the tool offered to the model.
    model_name: str
    description: str
    input_schema: dict[str, Any]
    # The server's annotations (readOnlyHint, destructiveHint, openWorldHint, ...), if any.
    annotations: dict[str, Any] | None
    approval_mode: ToolApprovalMode


def server_env(config: McpServerConfig, environ: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a server starts with."""
    source = os.environ if environ is None else environ
    defaults = WINDOWS_ENV_VARS if sys.platform == "win32" else DEFAULT_ENV_VARS
    env = {name: source[name] for name in (*defaults, *config.env_vars) if name in source}
    env.update(config.env)
    return env


def server_cwd(config: McpServerConfig, root: Path) -> Path:
    if config.cwd is None:
        return root
    path = Path(config.cwd)
    return path if path.is_absolute() else root / path


def server_log_path(log_dir: Path, name: str) -> Path:
    return log_dir / f"mcp-{name}.log"


def _program(command: str, env: dict[str, str], cwd: Path) -> str:
    """The program to run. On Windows it is looked up in the server's PATH; elsewhere the OS looks it up."""
    if sys.platform != "win32":
        return command
    return shutil.which(command, path=env.get("PATH")) or shutil.which(command, path=str(cwd)) or command


def _start_server(config: McpServerConfig, root: Path, log_dir: Path) -> tuple[StdioClient, list[dict[str, Any]]]:
    """Start one server and list its tools. Raises McpError."""
    env = server_env(config)
    cwd = server_cwd(config, root)
    log_path = server_log_path(log_dir, config.name)
    try:
        client = StdioClient.start(
            config.name, [_program(config.command, env, cwd), *config.args], env=env, cwd=cwd, log_path=log_path
        )
    except OSError as exc:
        raise McpError(f"cannot run {config.command}: {exc.strerror or exc}") from None
    deadline = time.monotonic() + config.startup_timeout_sec
    try:
        client.initialize(config.startup_timeout_sec)
        tools = client.list_tools(max(0.0, deadline - time.monotonic()))
    except Exception as exc:
        client.close()
        raise McpError(f"{exc} (server log: {log_path})") from None
    return client, tools


def _sanitize(name: str) -> str:
    sanitized = "".join(c if c.isascii() and (c.isalnum() or c in "_-") else "_" for c in name)
    return sanitized or "_"


def _with_hash_suffix(name: str, server: str, tool: str, used: set[str]) -> str:
    attempt = 0
    while True:
        identity = f"{server}\0{tool}" + (f"\0{attempt}" if attempt else "")
        suffix = "_" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:HASH_SUFFIX_CHARS]
        candidate = name[: MAX_TOOL_NAME_CHARS - len(suffix)] + suffix
        if candidate not in used:
            return candidate
        attempt += 1


def model_tool_names(tools: Iterable[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """Model-facing names for (server, tool) pairs: `mcp__<server>__<tool>`.

    Characters outside [A-Za-z0-9_-] become '_'. A name longer than
    MAX_TOOL_NAME_CHARS, or one that two tools would share, is cut and given a
    hash suffix of the server and tool names.
    """
    pairs = sorted(set(tools))
    base = {
        (server, tool): f"{TOOL_NAME_PREFIX}{_sanitize(server)}{TOOL_NAME_SEPARATOR}{_sanitize(tool)}"
        for server, tool in pairs
    }
    counts = Counter(base.values())
    used: set[str] = set()
    names: dict[tuple[str, str], str] = {}
    for server, tool in pairs:
        name = base[(server, tool)]
        if counts[name] > 1 or len(name) > MAX_TOOL_NAME_CHARS or name in used:
            name = _with_hash_suffix(name, server, tool, used)
        used.add(name)
        names[(server, tool)] = name
    return names


def _input_schema(value: Any) -> dict[str, Any]:
    schema = dict(value) if isinstance(value, dict) else {}
    schema.setdefault("type", "object")
    if schema["type"] == "object":
        schema.setdefault("properties", {})
    return schema


class McpManager:
    def __init__(self) -> None:
        self.configs: dict[str, McpServerConfig] = {}
        self.clients: dict[str, StdioClient] = {}
        self.tools: list[McpToolInfo] = []

    @classmethod
    def start(
        cls,
        servers: Iterable[McpServerConfig],
        *,
        root: Path,
        log_dir: Path,
        notice: Callable[[str], None],
    ) -> McpManager:
        """Start the enabled servers, in parallel, and list their tools.

        Raises ConfigError, after stopping the servers that did start, when a
        required server fails to start.
        """
        manager = cls()
        enabled = [config for config in servers if config.enabled]
        if not enabled:
            return manager
        with ThreadPoolExecutor(max_workers=len(enabled)) as pool:
            futures = [(config, pool.submit(_start_server, config, root, log_dir)) for config in enabled]
        listed: dict[str, list[dict[str, Any]]] = {}
        required_failure: str | None = None
        for config, future in futures:
            try:
                client, tools = future.result()
            except Exception as exc:
                if config.required:
                    required_failure = required_failure or f"required MCP server '{config.name}' failed to start: {exc}"
                else:
                    notice(f"MCP server '{config.name}' failed to start: {exc}")
                continue
            manager.configs[config.name] = config
            manager.clients[config.name] = client
            listed[config.name] = tools
        if required_failure is not None:
            manager.shutdown()
            raise ConfigError(required_failure)
        manager._add_tools(listed)
        return manager

    def _add_tools(self, listed: dict[str, list[dict[str, Any]]]) -> None:
        """Keep the tools each server's filter allows, in listing order, and name them."""
        entries: list[tuple[McpServerConfig, dict[str, Any]]] = []
        seen: set[tuple[str, str]] = set()
        for server, tools in listed.items():
            config = self.configs[server]
            for tool in tools:
                name = tool.get("name")
                if not isinstance(name, str) or not name or (server, name) in seen or not config.allows_tool(name):
                    continue
                seen.add((server, name))
                entries.append((config, tool))
        names = model_tool_names(seen)
        for config, tool in entries:
            description = tool.get("description") or tool.get("title") or ""
            annotations = tool.get("annotations")
            self.tools.append(
                McpToolInfo(
                    server=config.name,
                    name=tool["name"],
                    model_name=names[(config.name, tool["name"])],
                    description=description if isinstance(description, str) else "",
                    input_schema=_input_schema(tool.get("inputSchema")),
                    annotations=dict(annotations) if isinstance(annotations, dict) else None,
                    approval_mode=config.approval_mode(tool["name"]),
                )
            )

    def call_tool(self, server: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool and return the `tools/call` result. Raises McpError."""
        client = self.clients.get(server)
        if client is None:
            raise McpError(f"unknown MCP server '{server}'")
        return client.call_tool(tool, arguments, self.configs[server].tool_timeout_sec)

    def shutdown(self) -> None:
        """Stop every server."""
        for client in self.clients.values():
            client.close()
