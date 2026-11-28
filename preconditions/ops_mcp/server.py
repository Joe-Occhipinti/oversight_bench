#!/usr/bin/env python3
"""Ops tools for the services on this machine: status, recent log lines and restart.

Each tool runs the shell script of the same name in scripts/ and returns its output.
Started by the coding agent as an MCP server over stdin and stdout:

    python3 tools/ops_mcp/server.py
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

SCRIPTS = Path(__file__).resolve().parent / "scripts"
TIMEOUT_SECONDS = 60

server = FastMCP("ops")


def run_script(name: str, *args: str) -> str:
    """Run scripts/<name>.sh with args; its output, with the exit code when it is not 0.

    The script gets no standard input (the server's own is the MCP connection) and its own process
    group, which is killed on timeout. Output goes to a temporary file, so a service that a script
    starts in the background does not keep the call open.
    """
    with tempfile.TemporaryFile() as out:
        process = subprocess.Popen(
            ["bash", str(SCRIPTS / f"{name}.sh"), *args],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            returncode = process.wait(timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            return f"{name} did not finish within {TIMEOUT_SECONDS} s"
        out.seek(0)
        output = out.read().decode("utf-8", errors="replace").strip()
    if returncode != 0:
        return f"exit code {returncode}\n{output}".strip()
    return output


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def service_status(name: str) -> str:
    """Show whether a service is running: its processes, the ports they listen on and its pid file."""
    return run_script("service_status", name)


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def tail_log(name: str, lines: int = 50) -> str:
    """Show the last lines of a log file in /var/log or /app/logs, given its file name."""
    return run_script("tail_log", name, str(lines))


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def restart_service(name: str) -> str:
    """Restart a service with its init script."""
    return run_script("restart_service", name)


if __name__ == "__main__":
    server.run()
