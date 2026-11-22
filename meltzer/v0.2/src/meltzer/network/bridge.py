"""Bridge from loopback TCP inside the sandbox to meltzer's network proxy.

A sandboxed command with network mode `proxy` runs in its own network
namespace, where only the loopback interface exists. bwrap starts this script
instead of the command:

  python3 -I bridge.py --socket /run/meltzer-net/proxy.sock -- bash -c "$CMD"

The script listens on a free port on 127.0.0.1, connects to the proxy's Unix
socket once to tell the proxy it is running (the proxy stops when that
connection closes), and forks. The child forwards every TCP connection on
the port to the Unix socket; the parent sets the proxy environment variables
to the port and replaces itself with the command. The child ends with the
sandbox, when the command exits.

This file uses only the standard library: it runs with `python3 -I`, outside
the meltzer package.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
from pathlib import Path

# Sent on the bridge's first connection to the proxy.
BRIDGE_HELLO = b"MELTZER-BRIDGE\r\n\r\n"
LOOPBACK = "127.0.0.1"
NO_PROXY = "localhost,127.0.0.1,::1"
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
BUFFER_SIZE = 65536


def proxy_env(port: int) -> dict[str, str]:
    """The proxy environment variables for a bridge listening on port."""
    url = f"http://{LOOPBACK}:{port}"
    env = {}
    for name in PROXY_VARS:
        env[name] = url
        env[name.lower()] = url
    env["NO_PROXY"] = NO_PROXY
    env["no_proxy"] = NO_PROXY
    return env


def bridge_command(command: list[str], socket_path: Path) -> list[str]:
    """The argv that starts the bridge and then `command`, inside the sandbox."""
    return [sys.executable, "-I", str(Path(__file__).resolve()), "--socket", str(socket_path), "--", *command]


def pump(source: socket.socket, target: socket.socket) -> None:
    try:
        while True:
            data = source.recv(BUFFER_SIZE)
            if not data:
                break
            target.sendall(data)
    except OSError:
        pass
    finally:
        try:
            target.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _forward(client: socket.socket, socket_path: str) -> None:
    with client:
        try:
            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            upstream.connect(socket_path)
        except OSError:
            return
        with upstream:
            back = threading.Thread(target=pump, args=(upstream, client), daemon=True)
            back.start()
            pump(client, upstream)
            back.join()


def _serve(listener: socket.socket, socket_path: str) -> None:
    while True:
        try:
            client, _ = listener.accept()
        except OSError:
            return
        threading.Thread(target=_forward, args=(client, socket_path), daemon=True).start()


def _detach() -> None:
    """Leave the command's session and terminal, so signals for the command do not reach the bridge."""
    os.setsid()
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    os.close(devnull)


def main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[0] != "--socket" or argv[2] != "--":
        print("usage: bridge.py --socket PATH -- COMMAND...", file=sys.stderr)
        return 2
    socket_path, command = argv[1], argv[3:]
    try:
        listener = socket.create_server((LOOPBACK, 0))
        control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        control.connect(socket_path)
        control.sendall(BRIDGE_HELLO)
    except OSError as exc:
        print(f"meltzer: cannot start the network proxy bridge: {exc}", file=sys.stderr)
        return 1
    port = listener.getsockname()[1]
    if os.fork() == 0:
        try:
            _detach()
            _serve(listener, socket_path)
        finally:
            os._exit(0)
    listener.close()
    control.close()
    env = {**os.environ, **proxy_env(port)}
    try:
        os.execvpe(command[0], command, env)
    except OSError as exc:
        print(f"meltzer: cannot run {command[0]}: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
