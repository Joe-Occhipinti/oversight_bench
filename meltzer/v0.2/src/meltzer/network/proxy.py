"""The network proxy of one sandboxed command.

The proxy listens on a Unix socket in a private directory on the host. bwrap
mounts that directory at /run/meltzer-net in the sandbox, where the bridge
(meltzer.network.bridge) forwards loopback TCP connections to it.

Requests:
  CONNECT host:port                  a tunnel to host:port (protocol "https")
  GET http://host[:port]/path ...    one request in absolute form (protocol "http")

Each request is checked with the session's NetworkApprovals before the proxy
connects. The proxy resolves an allowed host once, checks every address it
resolves to, and connects to those addresses; the upstream request goes to the
host of the request target, whatever the Host header says. A plain HTTP
request is forwarded with `Connection: close`, and the client connection is
closed after the response, so every request comes in on a connection of its
own and is checked. Refused requests get a 403 response, malformed ones a 400,
and requests whose upstream cannot be reached a 502.

The proxy stops when the bridge's connection closes (the command has ended),
or when close() is called.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from meltzer.network.bridge import BRIDGE_HELLO, BUFFER_SIZE, pump
from meltzer.network.policy import NetworkApprovals, normalize_host, parse_address

# The socket in the proxy directory (mounted at sandbox.bwrap.PROXY_SOCKET_DIR).
SOCKET_NAME = "proxy.sock"
MAX_HEAD_BYTES = 64 * 1024
HEAD_TIMEOUT_SECONDS = 60.0
CONNECT_TIMEOUT_SECONDS = 10.0
# Headers that apply to one connection only; not forwarded upstream.
HOP_BY_HOP_HEADERS = frozenset(
    {"connection", "proxy-connection", "keep-alive", "proxy-authorization", "proxy-authenticate", "te", "trailer", "upgrade"}
)
STATUS_TEXT = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}
# Methods and header names are tokens (RFC 9110); the request target and header
# values contain no control characters other than tabs in values.
_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ProxyError(Exception):
    """A request the proxy answers itself, with an error status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _socket_base() -> Path:
    """Host directory for proxy directories: the user's runtime directory under /run, or /tmp."""
    runtime = Path(f"/run/user/{os.getuid()}")
    if runtime.is_dir() and os.access(runtime, os.W_OK | os.X_OK):
        return runtime
    return Path("/tmp")


class _Reader:
    """Buffered reads from a socket, keeping bytes read past what was asked for."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buffer = b""

    def _fill(self) -> bool:
        data = self.sock.recv(BUFFER_SIZE)
        if not data:
            return False
        self.buffer += data
        return True

    def read_until(self, marker: bytes, limit: int) -> bytes:
        """Bytes up to and including marker. Raises ProxyError if marker is not found within limit bytes."""
        while True:
            index = self.buffer.find(marker)
            if index >= 0:
                end = index + len(marker)
                data, self.buffer = self.buffer[:end], self.buffer[end:]
                return data
            if len(self.buffer) > limit:
                raise ProxyError(400, "request head too large")
            if not self._fill():
                raise ProxyError(400, "connection closed before the end of the request head")

    def copy_exact(self, size: int, target: socket.socket) -> None:
        """Send the next size bytes to target, as they arrive."""
        while size > 0:
            if not self.buffer and not self._fill():
                raise ProxyError(400, "connection closed before the end of the request body")
            data, self.buffer = self.buffer[:size], self.buffer[size:]
            target.sendall(data)
            size -= len(data)

    def take(self) -> bytes:
        data, self.buffer = self.buffer, b""
        return data


@dataclass
class Request:
    method: str
    host: str
    port: int
    protocol: str
    # For plain HTTP: the path and query, the headers, and how the body is framed
    # ("length" with its length, "chunked" or "none").
    path: str = ""
    headers: tuple[tuple[str, str], ...] = ()
    version: str = "HTTP/1.1"
    framing: str = "none"
    length: int = 0


def _parse_authority(text: str, default_port: int | None) -> tuple[str, int]:
    """host and port of `host[:port]` or `[v6]:port`."""
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise ProxyError(400, f"invalid target {text!r}")
        host, rest = text[1:end], text[end + 1 :]
        if rest and not rest.startswith(":"):
            raise ProxyError(400, f"invalid target {text!r}")
        port_text = rest[1:] if rest else None
    elif text.count(":") == 1:
        host, port_text = text.split(":")
    elif ":" in text:
        raise ProxyError(400, f"invalid target {text!r}: put IPv6 addresses in brackets")
    else:
        host, port_text = text, None
    if port_text is None:
        if default_port is None:
            raise ProxyError(400, f"invalid target {text!r}: a port is required")
        port = default_port
    else:
        if not (port_text.isascii() and port_text.isdigit()) or not 0 < int(port_text) < 65536:
            raise ProxyError(400, f"invalid port in {text!r}")
        port = int(port_text)
    try:
        return normalize_host(host), port
    except ValueError as exc:
        raise ProxyError(400, str(exc)) from None


def parse_request(head: bytes) -> Request:
    """The request in a request head (request line and headers, ending with an empty line)."""
    lines = head.decode("iso-8859-1").split("\r\n")
    parts = lines[0].split(" ")
    if (
        len(parts) != 3
        or not _TOKEN.match(parts[0])
        or parts[2] not in ("HTTP/1.0", "HTTP/1.1")
        or _CONTROL.search(parts[1])
    ):
        raise ProxyError(400, "invalid request line")
    method, target, version = parts
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        name, colon, value = line.partition(":")
        if not colon or not _TOKEN.match(name) or _CONTROL.search(value.replace("\t", " ")):
            raise ProxyError(400, f"invalid header line {line!r}")
        headers.append((name, value.strip()))
    if method == "CONNECT":
        host, port = _parse_authority(target, None)
        return Request(method, host, port, "https", version=version)
    if not target.lower().startswith("http://"):
        raise ProxyError(400, "the proxy takes CONNECT requests and http:// URLs in absolute form")
    try:
        url = urlsplit(target)
    except ValueError:
        raise ProxyError(400, f"invalid target {target!r}") from None
    authority = url.netloc.rpartition("@")[2]
    host, port = _parse_authority(authority, 80)
    path = url.path or "/"
    if url.query:
        path += "?" + url.query
    framing, length = _body_framing(headers)
    return Request(
        method, host, port, "http", path=path, headers=tuple(headers), version=version, framing=framing, length=length
    )


def _authority(host: str, port: int) -> str:
    """The Host header for host and port."""
    if ":" in host:
        host = f"[{host}]"
    return host if port == 80 else f"{host}:{port}"


def _body_framing(headers: list[tuple[str, str]]) -> tuple[str, int]:
    """("length", n), ("chunked", 0) or ("none", 0) for a request's headers."""
    encodings = [value for name, value in headers if name.lower() == "transfer-encoding"]
    lengths = {value for name, value in headers if name.lower() == "content-length"}
    if encodings:
        if lengths:
            raise ProxyError(400, "both Transfer-Encoding and Content-Length are set")
        codings = [c.strip().lower() for value in encodings for c in value.split(",")]
        if codings[-1] != "chunked":
            raise ProxyError(400, "unsupported Transfer-Encoding")
        return "chunked", 0
    if lengths:
        length = next(iter(lengths))
        if len(lengths) != 1 or not (length.isascii() and length.isdigit()):
            raise ProxyError(400, "invalid Content-Length")
        return "length", int(length)
    return "none", 0


def _copy_chunked(reader: _Reader, target: socket.socket) -> None:
    """Send a chunked body to target as it was sent, up to the end of the trailer."""
    while True:
        line = reader.read_until(b"\r\n", MAX_HEAD_BYTES)
        size_text = line[:-2].split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError:
            raise ProxyError(400, "invalid chunk size") from None
        target.sendall(line)
        if size == 0:
            break
        reader.copy_exact(size + 2, target)
    while True:
        line = reader.read_until(b"\r\n", MAX_HEAD_BYTES)
        target.sendall(line)
        if line == b"\r\n":
            return


def _error_response(status: int, message: str) -> bytes:
    body = f"meltzer network proxy: {message}\n".encode()
    head = (
        f"HTTP/1.1 {status} {STATUS_TEXT.get(status, 'Error')}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    )
    return head.encode() + body


class NetworkProxy:
    """HTTP proxy for one sandboxed command, on a Unix socket."""

    def __init__(self, approvals: NetworkApprovals, command: str, cwd: Path, directory: Path, listener: socket.socket):
        self.approvals = approvals
        self.command = command
        self.cwd = cwd
        # Private host directory that holds the socket; mounted at PROXY_SOCKET_DIR in the sandbox.
        self.directory = directory
        self.socket_path = directory / SOCKET_NAME
        self._listener = listener
        self._connections: set[socket.socket] = set()
        self._lock = threading.Lock()
        # Set by close() before it closes anything; _closed once everything is closed.
        self._stopping = False
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="network-proxy", daemon=True)

    @classmethod
    def start(cls, approvals: NetworkApprovals, command: str, cwd: Path) -> NetworkProxy:
        """Create the private directory and the socket, and start accepting connections."""
        directory = Path(tempfile.mkdtemp(prefix="meltzer-net-", dir=_socket_base()))
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(directory / SOCKET_NAME))
            listener.listen()
        except OSError:
            listener.close()
            shutil.rmtree(directory, ignore_errors=True)
            raise
        proxy = cls(approvals, command, cwd, directory, listener)
        proxy._thread.start()
        return proxy

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def wait_closed(self, timeout: float | None = None) -> bool:
        return self._closed.wait(timeout)

    def close(self) -> None:
        """Stop accepting connections, close the open ones and remove the directory."""
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
            connections = list(self._connections)
            self._connections.clear()
        try:
            self._listener.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._listener.close()
        for conn in connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
        shutil.rmtree(self.directory, ignore_errors=True)
        self._closed.set()

    def _track(self, conn: socket.socket) -> bool:
        with self._lock:
            if self._stopping:
                return False
            self._connections.add(conn)
            return True

    def _untrack(self, conn: socket.socket) -> None:
        with self._lock:
            self._connections.discard(conn)
        conn.close()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                break
            if not self._track(conn):
                conn.close()
                break
            threading.Thread(target=self._handle, args=(conn,), name="network-proxy-conn", daemon=True).start()
        self.close()

    def _handle(self, client: socket.socket) -> None:
        try:
            client.settimeout(HEAD_TIMEOUT_SECONDS)
            reader = _Reader(client)
            head = reader.read_until(b"\r\n\r\n", MAX_HEAD_BYTES)
            if head == BRIDGE_HELLO:
                client.settimeout(None)
                self._wait_for_bridge(client)
                return
            request = parse_request(head)
            upstream = self._connect(request)
            if not self._track(upstream):
                upstream.close()
                return
            try:
                if request.method == "CONNECT":
                    client.settimeout(None)
                    client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    self._tunnel(client, upstream, reader.take())
                else:
                    self._forward(client, upstream, reader, request)
            finally:
                self._untrack(upstream)
        except ProxyError as exc:
            try:
                client.sendall(_error_response(exc.status, str(exc)))
            except OSError:
                pass
        except OSError:
            pass
        finally:
            self._untrack(client)

    def _wait_for_bridge(self, conn: socket.socket) -> None:
        """Hold the bridge's connection; when it closes the command has ended and the proxy stops."""
        try:
            while conn.recv(BUFFER_SIZE):
                pass
        except OSError:
            pass
        self.close()

    def _connect(self, request: Request) -> socket.socket:
        decision = self.approvals.check(request.host, request.port, request.protocol, self.command, self.cwd)
        if not decision.allowed:
            raise ProxyError(403, decision.reason)
        literal = parse_address(request.host)
        if literal is not None:
            addresses = [literal]
        else:
            try:
                infos = socket.getaddrinfo(request.host, request.port, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise ProxyError(502, f"cannot resolve {request.host}: {exc}") from None
            addresses = []
            for info in infos:
                address = parse_address(str(info[4][0]))
                if address is not None and address not in addresses:
                    addresses.append(address)
        for address in addresses:
            refused = self.approvals.policy.check_address(address)
            if refused is not None:
                raise ProxyError(403, f"{request.host}: {refused}")
        error: OSError | None = None
        for address in addresses:
            family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.settimeout(CONNECT_TIMEOUT_SECONDS)
            try:
                sock.connect((str(address), request.port))
            except OSError as exc:
                sock.close()
                error = exc
                continue
            sock.settimeout(None)
            return sock
        raise ProxyError(502, f"cannot connect to {request.host}:{request.port}: {error}")

    def _tunnel(self, client: socket.socket, upstream: socket.socket, pending: bytes) -> None:
        if pending:
            upstream.sendall(pending)
        back = threading.Thread(target=pump, args=(upstream, client), daemon=True)
        back.start()
        pump(client, upstream)
        back.join()

    def _forward(self, client: socket.socket, upstream: socket.socket, reader: _Reader, request: Request) -> None:
        """Send one request upstream and relay the response until the upstream closes the connection."""
        connection_tokens = {
            token.strip().lower()
            for name, value in request.headers
            if name.lower() in ("connection", "proxy-connection")
            for token in value.split(",")
        }
        lines = [f"{request.method} {request.path} {request.version}", f"Host: {_authority(request.host, request.port)}"]
        for name, value in request.headers:
            lowered = name.lower()
            if lowered == "host" or lowered in HOP_BY_HOP_HEADERS:
                continue
            if lowered in connection_tokens and lowered not in ("content-length", "transfer-encoding"):
                continue
            lines.append(f"{name}: {value}")
        lines.append("Connection: close")
        upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1"))
        if request.framing == "length":
            reader.copy_exact(request.length, upstream)
        elif request.framing == "chunked":
            _copy_chunked(reader, upstream)
        client.settimeout(None)
        while True:
            data = upstream.recv(BUFFER_SIZE)
            if not data:
                break
            client.sendall(data)
