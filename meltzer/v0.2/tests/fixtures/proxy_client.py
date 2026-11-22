"""HTTP client for the network tests, run inside the sandbox.

  proxy_client.py get URL                GET URL through $HTTP_PROXY, print the response
  proxy_client.py connect HOST:PORT PATH CONNECT through $HTTP_PROXY, then GET PATH in the tunnel
  proxy_client.py direct HOST:PORT       connect to HOST:PORT without the proxy
"""

import os
import socket
import sys
from urllib.parse import urlsplit


def proxy_address() -> tuple[str, int]:
    parts = urlsplit(os.environ["HTTP_PROXY"])
    return parts.hostname, parts.port


def read_all(sock: socket.socket) -> bytes:
    chunks = []
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def read_head(sock: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(1)
        if not chunk:
            break
        data += chunk
    return data


def main(argv: list[str]) -> int:
    action = argv[0]
    if action == "get":
        url = argv[1]
        with socket.create_connection(proxy_address(), timeout=10) as sock:
            sock.sendall(f"GET {url} HTTP/1.1\r\nHost: {urlsplit(url).netloc}\r\nConnection: close\r\n\r\n".encode())
            sys.stdout.write(read_all(sock).decode("utf-8", errors="replace"))
        return 0
    if action == "connect":
        target, path = argv[1], argv[2]
        with socket.create_connection(proxy_address(), timeout=10) as sock:
            sock.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
            head = read_head(sock)
            sys.stdout.write(head.decode("utf-8", errors="replace"))
            if head.startswith(b"HTTP/1.1 200"):
                sock.sendall(f"GET {path} HTTP/1.1\r\nHost: {target}\r\nConnection: close\r\n\r\n".encode())
                sys.stdout.write(read_all(sock).decode("utf-8", errors="replace"))
            else:
                sys.stdout.write(read_all(sock).decode("utf-8", errors="replace"))
        return 0
    if action == "direct":
        host, _, port = argv[1].rpartition(":")
        try:
            socket.create_connection((host, int(port)), timeout=3).close()
        except OSError as exc:
            print(f"FAILED {exc}")
            return 1
        print("CONNECTED")
        return 0
    print(f"unknown action {action}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
