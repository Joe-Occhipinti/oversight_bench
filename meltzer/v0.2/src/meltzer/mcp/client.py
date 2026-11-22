"""A client for one MCP server over stdio: newline-delimited JSON-RPC 2.0.

The client starts the server process, sends `initialize`, `tools/list`,
`tools/call` and `ping` requests, and answers the server's own requests:
`ping` with an empty result, anything else with "method not found".
Notifications from the server are read and ignored.
"""

from __future__ import annotations

import itertools
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any

from meltzer import __version__

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "meltzer", "version": __version__}
METHOD_NOT_FOUND = -32601
# After stdin is closed and SIGTERM sent, how long the server has to exit before SIGKILL.
TERMINATE_GRACE_SECONDS = 2.0


class McpError(Exception):
    """A request to an MCP server failed; the message says why."""


class _Pending:
    """A request waiting for its response."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.message: dict[str, Any] | None = None
        self.error: str | None = None


def _seconds(value: float) -> str:
    return f"{value:g} seconds"


class StdioClient:
    def __init__(self, name: str, process: subprocess.Popen):
        self.name = name
        self.process = process
        self._ids = itertools.count(1)
        self._pending: dict[int, _Pending] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        # Why requests can no longer be sent; None while the server runs.
        self._closed: str | None = None
        self._stopped = False
        self._reader = threading.Thread(target=self._read, name=f"mcp-{name}", daemon=True)
        self._reader.start()

    @classmethod
    def start(cls, name: str, argv: list[str], *, env: dict[str, str], cwd: Path, log_path: Path) -> StdioClient:
        """Start the server process; its stderr is appended to log_path. Raises OSError."""
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log,
                # Own process group, so shutdown stops the server and what it started.
                start_new_session=True,
            )
        return cls(name, process)

    @property
    def running(self) -> bool:
        return self._closed is None

    # Requests

    def initialize(self, timeout: float) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
            timeout,
        )
        self.notify("notifications/initialized")
        return result

    def list_tools(self, timeout: float) -> list[dict[str, Any]]:
        """All tools of the server, following `nextCursor` across pages, within one timeout."""
        deadline = time.monotonic() + timeout
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params = {"cursor": cursor} if cursor is not None else {}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpError(f"tools/list timed out after {_seconds(timeout)}")
            result = self.request("tools/list", params, remaining)
            page = result.get("tools")
            if not isinstance(page, list):
                raise McpError("tools/list result has no list of tools")
            tools.extend(tool for tool in page if isinstance(tool, dict))
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                return tools

    def call_tool(self, name: str, arguments: dict[str, Any], timeout: float) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments}, timeout)

    def ping(self, timeout: float) -> None:
        self.request("ping", {}, timeout)

    def request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Send a request and wait for its result. Raises McpError on an error response, exit or timeout."""
        request_id = next(self._ids)
        pending = _Pending()
        with self._lock:
            if self._closed is not None:
                raise McpError(f"the server is not running ({self._closed})")
            self._pending[request_id] = pending
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            if not pending.done.wait(timeout):
                self.notify("notifications/cancelled", {"requestId": request_id, "reason": "timed out"})
                raise McpError(f"{method} timed out after {_seconds(timeout)}")
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
        if pending.error is not None:
            raise McpError(pending.error)
        message = pending.message or {}
        if "error" in message:
            error = message["error"] if isinstance(message["error"], dict) else {}
            raise McpError(f"{method} failed: {error.get('message', 'unknown error')} (code {error.get('code')})")
        result = message.get("result")
        if not isinstance(result, dict):
            raise McpError(f"{method} returned no result object")
        return result

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        try:
            self._send(message)
        except McpError:
            return

    # Transport

    def _send(self, message: dict[str, Any]) -> None:
        stdin = self.process.stdin
        assert stdin is not None
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                stdin.write(data)
                stdin.flush()
            except (OSError, ValueError) as exc:
                raise McpError(f"cannot write to the server: {exc}") from None

    def _read(self) -> None:
        stdout = self.process.stdout
        assert stdout is not None
        for line in stdout:
            try:
                message = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(message, dict):
                self._handle(message)
        try:
            self.process.wait(timeout=TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            self._fail_pending("closed its output")
            return
        self._fail_pending(f"exited with code {self.process.returncode}")

    def _handle(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method is None:
            with self._lock:
                pending = self._pending.get(message.get("id"))  # type: ignore[arg-type]
            if pending is not None:
                pending.message = message
                pending.done.set()
            return
        if "id" not in message:
            return
        if method == "ping":
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
        else:
            reply = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": METHOD_NOT_FOUND, "message": f"method not found: {method}"},
            }
        try:
            self._send(reply)
        except McpError:
            return

    def _fail_pending(self, reason: str) -> None:
        with self._lock:
            if self._closed is None:
                self._closed = reason
            pending, self._pending = list(self._pending.values()), {}
        for request in pending:
            request.error = f"the server {reason}"
            request.done.set()

    # Shutdown

    def close(self) -> None:
        """Stop the server: close its stdin, then SIGTERM its process group, then SIGKILL."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            if self._closed is None:
                self._closed = "shut down"
        stdin = self.process.stdin
        if stdin is not None:
            try:
                stdin.close()
            except OSError:
                pass
        _signal(self.process, signal.SIGTERM)
        try:
            self.process.wait(timeout=TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        _signal(self.process, getattr(signal, "SIGKILL", signal.SIGTERM))
        self.process.wait()
        self._reader.join(timeout=TERMINATE_GRACE_SECONDS)
        if self.process.stdout is not None:
            self.process.stdout.close()


def _signal(process: subprocess.Popen, sig: int) -> None:
    """Signal the server's process group (on Windows, the process)."""
    try:
        if sys.platform == "win32":
            if process.poll() is None:
                process.terminate()
            return
        os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError):
        # The group is gone, or only processes we may not signal are left in it.
        return
