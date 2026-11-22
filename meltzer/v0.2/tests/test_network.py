from __future__ import annotations

import ipaddress
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from meltzer.approval.orchestrator import NetworkRequest
from meltzer.config.loader import ConfigError
from meltzer.exec import ProcessRunner
from meltzer.network.bridge import BRIDGE_HELLO, bridge_command, proxy_env
from meltzer.network.policy import NetworkApprovals, NetworkPolicy, is_local_address, normalize_host
from meltzer.network.proxy import SOCKET_NAME, NetworkProxy
from meltzer.sandbox import bwrap
from meltzer.sandbox.bwrap import PROXY_SOCKET_DIR, SandboxSetupError, build_bwrap_args
from meltzer.sandbox.policy import NetworkMode, SandboxMode, SandboxPolicy

from conftest import needs_bwrap, skip_without_bwrap

CLIENT = Path(__file__).parent / "fixtures" / "proxy_client.py"


@dataclass
class Verdict:
    run: bool
    reason: str
    for_session: bool = False


class Reviewer:
    """Stands in for orchestrator.decide_network."""

    def __init__(self, approve: bool, for_session: bool = False):
        self.approve = approve
        self.for_session = for_session
        self.requests: list[NetworkRequest] = []

    def __call__(self, request: NetworkRequest) -> Verdict:
        self.requests.append(request)
        return Verdict(self.approve, "approved by user" if self.approve else "denied by user", self.for_session)


# --- hosts and patterns ------------------------------------------------------------------


def test_normalize_host():
    assert normalize_host("Example.COM.") == "example.com"
    assert normalize_host("[2001:DB8::1]") == "2001:db8::1"
    assert normalize_host("10.0.0.1") == "10.0.0.1"
    assert normalize_host("[::ffff:8.8.8.8]") == "8.8.8.8"
    assert normalize_host("[2001:db8::1%eth0]") == "2001:db8::1"
    for bad in ("", ".", "a b", "exa_mple/x", "host:80", "010.000.000.001", "2130706433", "0x7f.1"):
        with pytest.raises(ValueError):
            normalize_host(bad)


@dataclass
class FakeConfig:
    network_allowed_domains: tuple[str, ...] = ()
    network_denied_domains: tuple[str, ...] = ()
    network_allow_local_binding: bool = False


def test_policy_entries_are_validated():
    NetworkPolicy(allowed=("pypi.org", "*.github.com", "10.0.0.5", "2001:db8::1", "[2001:db8::2]", "Example.org."))
    for bad in ("*", "**.github.com", "a.*.com", "*github.com", "10.0.0.0/8", "pypi.org:443", "http://pypi.org", ""):
        with pytest.raises(ValueError):
            NetworkPolicy(allowed=(bad,))
        with pytest.raises(ValueError):
            NetworkPolicy(denied=(bad,))
    assert not NetworkPolicy.from_config(FakeConfig()).is_allowed("pypi.org")
    policy = NetworkPolicy.from_config(FakeConfig(("pypi.org",), ("evil.example",), True))
    assert policy.is_allowed("pypi.org") and policy.is_denied("evil.example") and policy.allow_local_binding
    with pytest.raises(ConfigError, match="network.denied_domains"):
        NetworkPolicy.from_config(FakeConfig(network_denied_domains=("*",)))


def test_exact_and_subdomain_patterns():
    policy = NetworkPolicy(allowed=("pypi.org", "*.github.com"))
    assert policy.is_allowed("pypi.org")
    assert policy.is_allowed("PyPI.org.")
    assert not policy.is_allowed("files.pypi.org")
    assert policy.is_allowed("api.github.com")
    assert policy.is_allowed("a.b.github.com")
    assert not policy.is_allowed("github.com")
    assert not policy.is_allowed("evilgithub.com")
    assert not policy.is_allowed("github.com.evil.example")


def test_ip_entries_match_the_address():
    policy = NetworkPolicy(allowed=("93.184.216.34", "2001:db8::1"))
    assert policy.is_allowed("93.184.216.34")
    assert policy.is_allowed("[2001:0db8:0:0::1]")
    assert not policy.is_allowed("93.184.216.35")
    # A wildcard entry does not match IP addresses.
    assert not NetworkPolicy(allowed=("*.example.com",)).is_allowed("93.184.216.34")


def test_deny_wins_over_allow():
    policy = NetworkPolicy(allowed=("*.example.com", "api.example.com"), denied=("api.example.com",))
    assert policy.is_denied("api.example.com")
    assert policy.is_allowed("www.example.com") and not policy.is_denied("www.example.com")


@pytest.mark.parametrize("host", ["8.8.8.8", "[::ffff:8.8.8.8]", "::ffff:808:808"])
def test_denied_address_matches_its_ipv4_mapped_form(host):
    policy = NetworkPolicy(denied=("8.8.8.8",))
    assert policy.is_denied(host)
    assert policy.check_address(ipaddress.ip_address(host.strip("[]"))) is not None


def test_denied_address_matches_without_its_scope_id():
    policy = NetworkPolicy(denied=("2001:db8::1",))
    assert policy.is_denied("[2001:db8::1%eth0]")
    assert policy.check_address(ipaddress.ip_address("2001:db8::1%eth0")) is not None


@pytest.mark.parametrize(
    "address, local",
    [(address, True) for address in (
        "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "224.0.0.1", "0.0.0.0",
        "::1", "fe80::1", "fc00::1", "ff02::1", "::ffff:127.0.0.1", "100.64.0.1", "64:ff9b::7f00:1", "::7f00:1",
    )] + [(address, False) for address in ("93.184.216.34", "8.8.8.8", "2606:4700::1111")],
)
def test_local_and_private_addresses(address, local):
    assert is_local_address(ipaddress.ip_address(address)) is local


def test_address_checks():
    policy = NetworkPolicy(allowed=("10.0.0.5",), denied=("93.184.216.99",))
    ip = ipaddress.ip_address
    assert policy.check_address(ip("93.184.216.34")) is None
    assert policy.check_address(ip("10.0.0.5")) is None
    assert policy.check_address(ip("10.0.0.6")) is not None
    assert policy.check_address(ip("127.0.0.1")) is not None
    assert policy.check_address(ip("93.184.216.99")) is not None
    local = NetworkPolicy(allow_local_binding=True)
    assert local.check_address(ip("127.0.0.1")) is None
    assert local.check_address(ip("::1")) is None
    assert local.check_address(ip("192.168.1.1")) is not None


# --- decisions -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "policy, host, allowed",
    [
        (NetworkPolicy(allowed=("pypi.org",)), "pypi.org", True),
        (NetworkPolicy(denied=("*.evil.example",)), "a.evil.example", False),
    ],
)
def test_listed_hosts_are_decided_without_review(policy, host, allowed):
    reviewer = Reviewer(not allowed)
    decision = NetworkApprovals(policy, reviewer).check(host, 443, "https", "curl x", Path("/w"))
    assert decision.allowed is allowed and (allowed or "denied" in decision.reason)
    assert reviewer.requests == []


def test_unlisted_host_goes_to_the_reviewer():
    reviewer = Reviewer(True)
    approvals = NetworkApprovals(NetworkPolicy(), reviewer)
    decision = approvals.check("example.org", 443, "https", "curl https://example.org", Path("/w"))
    assert decision.allowed
    assert reviewer.requests == [NetworkRequest("example.org", 443, "https", "curl https://example.org", Path("/w"))]


def test_approvals_for_the_session_are_cached_by_host_port_and_protocol():
    reviewer = Reviewer(True, for_session=True)
    approvals = NetworkApprovals(NetworkPolicy(), reviewer)
    for _ in range(3):
        assert approvals.check("example.org", 443, "https", "curl", Path("/w")).allowed
    assert len(reviewer.requests) == 1
    approvals.check("example.org", 80, "http", "curl", Path("/w"))
    approvals.check("example.org", 8443, "https", "curl", Path("/w"))
    assert len(reviewer.requests) == 3


def test_other_approvals_are_not_cached():
    reviewer = Reviewer(True)
    approvals = NetworkApprovals(NetworkPolicy(), reviewer)
    for _ in range(3):
        assert approvals.check("example.org", 443, "https", "curl", Path("/w")).allowed
    assert len(reviewer.requests) == 3


def test_cached_approvals_belong_to_one_session():
    first, second = Reviewer(True, True), Reviewer(True, True)
    policy = NetworkPolicy()
    NetworkApprovals(policy, first).check("example.org", 443, "https", "curl", Path("/w"))
    NetworkApprovals(policy, second).check("example.org", 443, "https", "curl", Path("/w"))
    assert len(first.requests) == 1 and len(second.requests) == 1


def test_reviewer_denial_refuses_and_asks_again_next_time():
    reviewer = Reviewer(False)
    approvals = NetworkApprovals(NetworkPolicy(), reviewer)
    decision = approvals.check("example.org", 443, "https", "curl", Path("/w"))
    assert not decision.allowed and "denied by user" in decision.reason
    approvals.check("example.org", 443, "https", "curl", Path("/w"))
    assert len(reviewer.requests) == 2


def test_without_a_reviewer_unlisted_hosts_are_refused():
    approvals = NetworkApprovals(NetworkPolicy(allowed=("pypi.org",)))
    assert approvals.check("pypi.org", 443, "https", "pip", Path("/w")).allowed
    decision = approvals.check("example.org", 443, "https", "curl", Path("/w"))
    assert not decision.allowed and "allow list" in decision.reason


def test_local_ip_literal_is_refused_before_review():
    reviewer = Reviewer(True)
    approvals = NetworkApprovals(NetworkPolicy(), reviewer)
    assert not approvals.check("169.254.169.254", 80, "http", "curl", Path("/w")).allowed
    assert not approvals.check("::1", 80, "http", "curl", Path("/w")).allowed
    assert reviewer.requests == []
    listed = NetworkApprovals(NetworkPolicy(allowed=("10.0.0.5",)), reviewer)
    assert listed.check("10.0.0.5", 80, "http", "curl", Path("/w")).allowed
    local = NetworkApprovals(NetworkPolicy(allow_local_binding=True), reviewer)
    assert local.check("127.0.0.1", 8000, "http", "curl", Path("/w")).allowed
    assert len(reviewer.requests) == 1


def test_requests_for_other_hosts_are_not_held_up_by_a_review():
    release = threading.Event()
    asked: list[str] = []

    def decide(request: NetworkRequest) -> Verdict:
        asked.append(request.host)
        if request.host == "slow.example":
            release.wait(5)
        return Verdict(True, "approved")

    approvals = NetworkApprovals(NetworkPolicy(), decide)
    slow = threading.Thread(target=approvals.check, args=("slow.example", 443, "https", "curl", Path("/w")))
    slow.start()
    while "slow.example" not in asked:
        time.sleep(0.01)
    assert approvals.check("other.example", 443, "https", "curl", Path("/w")).allowed
    release.set()
    slow.join()


def test_concurrent_requests_for_one_host_are_reviewed_once():
    calls: list[NetworkRequest] = []

    def slow(request: NetworkRequest) -> Verdict:
        calls.append(request)
        time.sleep(0.2)
        return Verdict(True, "approved", for_session=True)

    approvals = NetworkApprovals(NetworkPolicy(), slow)
    threads = [
        threading.Thread(target=approvals.check, args=("example.org", 443, "https", "curl", Path("/w")))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(calls) == 1


# --- bridge and bwrap arguments ----------------------------------------------------------------


def test_proxy_env():
    env = proxy_env(41234)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        assert env[name] == "http://127.0.0.1:41234"
    assert env["NO_PROXY"] == env["no_proxy"] == "localhost,127.0.0.1,::1"


def test_bridge_command_runs_the_command_after_the_bridge():
    argv = bridge_command(["bash", "-c", "echo hi"], PROXY_SOCKET_DIR / SOCKET_NAME)
    assert argv[0] == sys.executable and "-I" in argv
    assert argv[-4:] == ["--", "bash", "-c", "echo hi"]
    assert str(PROXY_SOCKET_DIR / SOCKET_NAME) in argv


def test_bwrap_binds_the_proxy_directory_and_keeps_the_network_namespace(root, tmp_path, monkeypatch):
    monkeypatch.setattr(bwrap.shutil, "which", lambda name: "/usr/bin/bwrap")
    proxy_dir = tmp_path / "net"
    proxy_dir.mkdir()
    policy = SandboxPolicy.for_session(SandboxMode.WORKSPACE_WRITE, root, network=NetworkMode.PROXY)
    args = build_bwrap_args(policy, ["true"], root, status_fd=7, proxy_dir=proxy_dir)
    assert "--unshare-net" in args
    index = args.index(str(proxy_dir))
    assert args[index - 1 : index + 2] == ["--ro-bind", str(proxy_dir), PROXY_SOCKET_DIR.as_posix()]
    # Mounted on top of the empty /run.
    assert all(i < index for i, arg in enumerate(args) if arg == "--tmpfs" and args[i + 1] == "/run")
    # Without a proxy directory a proxy-mode command has no network at all.
    plain = build_bwrap_args(policy, ["true"], root, status_fd=7)
    assert "--unshare-net" in plain and PROXY_SOCKET_DIR.as_posix() not in plain
    # Other network modes never bind a proxy directory.
    for mode in (NetworkMode.NONE, NetworkMode.FULL):
        other = SandboxPolicy.for_session(SandboxMode.WORKSPACE_WRITE, root, network=mode)
        args = build_bwrap_args(other, ["true"], root, status_fd=7, proxy_dir=proxy_dir)
        assert PROXY_SOCKET_DIR.as_posix() not in args and ("--unshare-net" in args) == (mode == NetworkMode.NONE)


# --- the proxy on the host ----------------------------------------------------------------------


class Upstream:
    """A local HTTP server that records the requests it receives."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str | None]] = []
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                upstream.requests.append(("GET", self.path, self.headers.get("Host")))
                self._reply(b"hello from host\n")

            def do_POST(self) -> None:
                body = b""
                if self.headers.get("Transfer-Encoding") == "chunked":
                    while size := int(self.rfile.readline(), 16):
                        body += self.rfile.read(size + 2)[:-2]
                    self.rfile.readline()
                else:
                    body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                upstream.requests.append(("POST", self.path, body.decode()))
                self._reply(b"got " + body + b"\n")

            def _reply(self, body: bytes) -> None:
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def upstream():
    server = Upstream()
    yield server
    server.close()


def exchange(proxy: NetworkProxy, data: bytes) -> bytes:
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(10)
        sock.connect(str(proxy.socket_path))
        sock.sendall(data)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)


@pytest.fixture
def start_proxy():
    if not hasattr(socket, "AF_UNIX") or sys.platform == "win32":
        pytest.skip("needs Unix sockets")
    started: list[NetworkProxy] = []

    def start(policy: NetworkPolicy, reviewer=None) -> NetworkProxy:
        started.append(NetworkProxy.start(NetworkApprovals(policy, reviewer), "curl test", Path("/work")))
        return started[-1]

    yield start
    for proxy in started:
        proxy.close()


def test_proxy_forwards_an_allowed_http_request(upstream, start_proxy):
    proxy = start_proxy(NetworkPolicy(allowed=("127.0.0.1",)))
    url = f"http://127.0.0.1:{upstream.port}/index.html?x=1"
    response = exchange(proxy, f"GET {url} HTTP/1.1\r\nHost: 127.0.0.1\r\nProxy-Connection: keep-alive\r\n\r\n".encode())
    assert response.startswith(b"HTTP/1.1 200") and response.endswith(b"hello from host\n")
    assert upstream.requests == [("GET", "/index.html?x=1", f"127.0.0.1:{upstream.port}")]


def test_proxy_forwards_a_request_body(upstream, start_proxy):
    proxy = start_proxy(NetworkPolicy(allowed=("127.0.0.1",)))
    url = f"http://127.0.0.1:{upstream.port}/upload"
    chunked = f"POST {url} HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n".encode()
    sized = f"POST {url} HTTP/1.1\r\nContent-Length: 5\r\n\r\nworld".encode()
    assert exchange(proxy, sized).endswith(b"got world\n")
    assert exchange(proxy, chunked).endswith(b"got hello\n")
    assert [request[2] for request in upstream.requests] == ["world", "hello"]


def test_proxy_uses_the_request_target_not_the_host_header(upstream, start_proxy):
    proxy = start_proxy(NetworkPolicy(allowed=("127.0.0.1",)))
    url = f"http://127.0.0.1:{upstream.port}/"
    exchange(proxy, f"GET {url} HTTP/1.1\r\nHost: other.example\r\n\r\n".encode())
    assert upstream.requests == [("GET", "/", f"127.0.0.1:{upstream.port}")]


def test_proxy_tunnels_connect(upstream, start_proxy):
    proxy = start_proxy(NetworkPolicy(allowed=("127.0.0.1",)))
    target = f"127.0.0.1:{upstream.port}"
    request = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\nGET /tunnel HTTP/1.1\r\nHost: {target}\r\nConnection: close\r\n\r\n"
    response = exchange(proxy, request.encode())
    assert response.startswith(b"HTTP/1.1 200")
    assert response.endswith(b"hello from host\n")
    assert upstream.requests == [("GET", "/tunnel", target)]


def test_proxy_sends_an_unlisted_host_to_the_reviewer(upstream, start_proxy):
    reviewer = Reviewer(True)
    proxy = start_proxy(NetworkPolicy(allow_local_binding=True), reviewer)
    target = f"localhost:{upstream.port}"
    request = f"CONNECT {target} HTTP/1.1\r\n\r\nGET / HTTP/1.1\r\nHost: {target}\r\nConnection: close\r\n\r\n"
    response = exchange(proxy, request.encode())
    assert response.startswith(b"HTTP/1.1 200") and response.endswith(b"hello from host\n")
    assert reviewer.requests == [NetworkRequest("localhost", upstream.port, "https", "curl test", Path("/work"))]


@pytest.mark.parametrize(
    "policy, approves, reviews, reason",
    [
        (NetworkPolicy(denied=("localhost",), allow_local_binding=True), True, 0, b""),
        (NetworkPolicy(allow_local_binding=True), False, 1, b"denied by user"),
        (NetworkPolicy(allowed=("localhost",)), True, None, b""),
    ],
    ids=["denied-host", "reviewer-denies", "resolves-to-a-local-address"],
)
def test_proxy_refuses_what_policy_or_reviewer_refuse(upstream, start_proxy, policy, approves, reviews, reason):
    reviewer = Reviewer(approves)
    proxy = start_proxy(policy, reviewer)
    response = exchange(proxy, f"GET http://localhost:{upstream.port}/ HTTP/1.1\r\n\r\n".encode())
    assert response.startswith(b"HTTP/1.1 403") and reason in response
    assert upstream.requests == [] and reviews in (None, len(reviewer.requests))


def test_every_request_on_a_connection_is_checked(upstream, start_proxy):
    reviewer = Reviewer(False)
    proxy = start_proxy(NetworkPolicy(allowed=("127.0.0.1",)), reviewer)
    first = f"GET http://127.0.0.1:{upstream.port}/first HTTP/1.1\r\n\r\n"
    second = f"GET http://localhost:{upstream.port}/second HTTP/1.1\r\n\r\n"
    third = f"GET http://127.0.0.1:{upstream.port}/third HTTP/1.1\r\n\r\n"
    response = exchange(proxy, (first + second + third).encode())
    assert response.startswith(b"HTTP/1.1 200")
    assert [path for _, path, _ in upstream.requests] == ["/first"]


@pytest.mark.parametrize(
    "request_text",
    [
        "GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
        "GET ftp://127.0.0.1/ HTTP/1.1\r\n\r\n",
        "CONNECT 127.0.0.1 HTTP/1.1\r\n\r\n",
        "CONNECT 127.0.0.1:0 HTTP/1.1\r\n\r\n",
        "CONNECT 127.0.0.1:\u00b2 HTTP/1.1\r\n\r\n",
        "GET http://127.0.0.1:{port}/ HTTP/1.1\r\nContent-Length: \u00b2\r\n\r\n",
        "GET http://[abc/x HTTP/1.1\r\n\r\n",
        "GET http://127.0.0.1:{port}/ HTTP/1.1\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n",
        "not a request\r\n\r\n",
        "GET http://127.0.0.1:{port}/ HTTP/2\r\n\r\n",
        "GET http://127.0.0.1:{port}/ HTTP/1.1\r\nX-A: a\nX-B: b\r\n\r\n",
        "GET http://127.0.0.1:{port}/ HTTP/1.1\r\n folded: value\r\n\r\n",
    ],
)
def test_proxy_rejects_malformed_requests(upstream, start_proxy, request_text):
    proxy = start_proxy(NetworkPolicy(allowed=("127.0.0.1",)))
    response = exchange(proxy, request_text.format(port=upstream.port).encode("iso-8859-1"))
    assert response.startswith(b"HTTP/1.1 400")
    assert upstream.requests == []


def test_proxy_reports_an_unreachable_upstream(start_proxy):
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
    proxy = start_proxy(NetworkPolicy(allowed=("127.0.0.1",)))
    response = exchange(proxy, f"GET http://127.0.0.1:{port}/ HTTP/1.1\r\n\r\n".encode())
    assert response.startswith(b"HTTP/1.1 502")


def test_proxy_closes_when_the_bridge_connection_ends(start_proxy):
    proxy = start_proxy(NetworkPolicy())
    directory = proxy.directory
    assert directory.stat().st_mode & 0o777 == 0o700
    bridge = socket.socket(socket.AF_UNIX)
    bridge.connect(str(proxy.socket_path))
    bridge.sendall(BRIDGE_HELLO)
    time.sleep(0.2)
    assert not proxy.closed
    bridge.close()
    assert proxy.wait_closed(5)
    assert not directory.exists()


# --- commands in the sandbox ---------------------------------------------------------------------


def client(*args: str) -> str:
    return " ".join([sys.executable, "-I", str(CLIENT), *args])


def proxy_policy(root: Path) -> SandboxPolicy:
    return SandboxPolicy.for_session(SandboxMode.WORKSPACE_WRITE, root, network=NetworkMode.PROXY)


@pytest.fixture
def runner(root, tmp_path):
    skip_without_bwrap()
    runner = ProcessRunner(proxy_policy(root), log_dir=tmp_path / "logs", timeout=30)
    yield runner
    runner.terminate_all()


def test_allowed_host_is_reachable_through_the_proxy(root, runner, upstream):
    network = NetworkApprovals(NetworkPolicy(allowed=("127.0.0.1",)))
    result = runner.run(client("get", f"http://127.0.0.1:{upstream.port}/allowed"), root, sandboxed=True, network=network)
    assert result.exit_code == 0, result.output
    assert result.output.startswith("HTTP/1.1 200") and "hello from host" in result.output
    tunnel = runner.run(client("connect", f"127.0.0.1:{upstream.port}", "/tunnel"), root, sandboxed=True, network=network)
    assert "hello from host" in tunnel.output, tunnel.output
    assert [path for _, path, _ in upstream.requests] == ["/allowed", "/tunnel"]


def test_direct_connection_without_the_proxy_fails(root, runner, upstream):
    network = NetworkApprovals(NetworkPolicy(allowed=("127.0.0.1",)))
    result = runner.run(client("direct", f"127.0.0.1:{upstream.port}"), root, sandboxed=True, network=network)
    assert result.exit_code != 0 and "CONNECTED" not in result.output
    external = runner.run(client("direct", "93.184.216.34:80"), root, sandboxed=True, network=network)
    assert external.exit_code != 0 and "CONNECTED" not in external.output
    assert upstream.requests == []


def test_background_command_keeps_its_proxy_until_it_ends(root, runner, upstream):
    network = NetworkApprovals(NetworkPolicy(allowed=("127.0.0.1",)))
    command = f"sleep 1; {client('get', f'http://127.0.0.1:{upstream.port}/background')}"
    process = runner.start_background(command, root, sandboxed=True, network=network)
    process.process.wait(timeout=20)
    assert "hello from host" in process.log_path.read_text()
    proxy = runner.proxies[-1]
    assert proxy.wait_closed(5)
    runner.start_background("sleep 30", root, sandboxed=True, network=network)
    running = runner.proxies[-1]
    assert not running.closed
    runner.terminate_all()
    assert running.closed


def test_proxy_stops_when_the_command_cannot_start(root, runner, tmp_path):
    network = NetworkApprovals(NetworkPolicy())
    for start in (runner.run, runner.start_background):
        with pytest.raises(SandboxSetupError):
            start("true", tmp_path / "missing", sandboxed=True, network=network)
        assert runner.proxies[-1].closed


def test_argv_of_a_command_started_by_its_caller_reaches_the_proxy(root, runner, upstream):
    argv, status_read, status_write, _ = runner.build_argv(
        client("get", f"http://127.0.0.1:{upstream.port}/interactive"),
        root,
        True,
        proxy_policy(root),
        network=NetworkApprovals(NetworkPolicy(allowed=("127.0.0.1",))),
    )
    process = subprocess.Popen(argv, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, pass_fds=(status_write,))
    os.close(status_write)
    output, _ = process.communicate(timeout=20)
    os.close(status_read)
    assert b"hello from host" in output
    assert runner.proxies[-1].wait_closed(5)


@needs_bwrap
def test_other_network_modes_do_not_start_a_proxy(root, tmp_path):
    policy = SandboxPolicy.for_session(SandboxMode.WORKSPACE_WRITE, root, network=NetworkMode.NONE)
    runner = ProcessRunner(policy, log_dir=tmp_path / "logs", timeout=30)
    result = runner.run("ls -A /run; echo END", root, sandboxed=True, network=NetworkApprovals(NetworkPolicy()))
    assert result.output == "END\n" and runner.proxies == []
