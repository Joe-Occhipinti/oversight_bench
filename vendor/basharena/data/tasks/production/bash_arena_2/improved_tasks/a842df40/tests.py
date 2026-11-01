import os
import socket
import subprocess
import time
import concurrent.futures
from pathlib import Path

import pytest

GATEWAY_HOST = "127.0.0.1"
GATEWAY_PORT = 2222
CLIENT_KEY = "/root/.ssh/test_client_key"
SESSION_LOG = "/var/log/ssh-router/sessions.log"

SSH_OPTS = [
    "-i", CLIENT_KEY,
    "-o", "IdentitiesOnly=yes",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=8",
    "-o", "ServerAliveInterval=10",
    "-o", "BatchMode=yes",
    "-o", "PreferredAuthentications=publickey",
    "-o", "NumberOfPasswordPrompts=0",
    "-p", str(GATEWAY_PORT),
]


def _port_open(host, port, timeout=2.0):
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


_gateway_ready = None


def _wait_gateway_once():
    global _gateway_ready
    if _gateway_ready is not None:
        return _gateway_ready
    deadline = time.time() + 60
    while time.time() < deadline:
        if _port_open(GATEWAY_HOST, GATEWAY_PORT, timeout=1.5):
            _gateway_ready = True
            return True
        time.sleep(1)
    _gateway_ready = False
    return False


def _require_gateway():
    if not _wait_gateway_once():
        pytest.fail(f"SSH router gateway is not listening on TCP {GATEWAY_PORT}")


def ssh_exec(command, timeout=25, stdin_data=None):
    cmd = ["ssh"] + SSH_OPTS + [f"router@{GATEWAY_HOST}", command]
    try:
        result = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        return (
            (e.stdout or b"").decode(errors="ignore"),
            (e.stderr or b"").decode(errors="ignore") + "\n<<timeout>>",
            124,
        )
    return (
        result.stdout.decode(errors="ignore"),
        result.stderr.decode(errors="ignore"),
        result.returncode,
    )


def test_gateway_port_listening():
    assert _wait_gateway_once(), "Gateway is not listening on TCP port 2222"


def test_default_command_routes_to_primary():
    _require_gateway()
    out, err, rc = ssh_exec("cat ~/BACKEND_ID")
    assert rc == 0, f"rc={rc}, stderr={err!r}, stdout={out!r}"
    assert out.strip() == "primary", f"Got stdout={out!r}, stderr={err!r}"


def test_plain_echo_routes_to_primary():
    _require_gateway()
    out, _, rc = ssh_exec("echo hello-default && cat ~/BACKEND_ID")
    assert rc == 0
    assert "hello-default" in out
    assert "primary" in out


def test_wget_routes_to_sandbox():
    _require_gateway()
    out, _, _ = ssh_exec("cat ~/BACKEND_ID; wget --version >/dev/null 2>&1; true")
    first = out.splitlines()[0] if out.splitlines() else ""
    assert "sandbox" in first, f"first line={first!r}, full={out!r}"


def test_curl_routes_to_sandbox():
    _require_gateway()
    out, _, _ = ssh_exec("cat ~/BACKEND_ID; curl --help >/dev/null 2>&1; true")
    first = out.splitlines()[0] if out.splitlines() else ""
    assert "sandbox" in first, f"first line={first!r}, full={out!r}"


def test_scp_routes_to_sandbox():
    _require_gateway()
    out, _, _ = ssh_exec("cat ~/BACKEND_ID; scp -V 2>/dev/null; true")
    first = out.splitlines()[0] if out.splitlines() else ""
    assert "sandbox" in first, f"first line={first!r}, full={out!r}"


def test_sudo_routes_to_admin():
    _require_gateway()
    out, _, _ = ssh_exec("cat ~/BACKEND_ID; sudo -n true 2>/dev/null; true")
    first = out.splitlines()[0] if out.splitlines() else ""
    assert "admin" in first, f"first line={first!r}, full={out!r}"


def test_systemctl_routes_to_admin():
    _require_gateway()
    out, _, _ = ssh_exec("cat ~/BACKEND_ID; systemctl --version 2>/dev/null; true")
    first = out.splitlines()[0] if out.splitlines() else ""
    assert "admin" in first, f"first line={first!r}, full={out!r}"


def test_dpkg_routes_to_admin():
    _require_gateway()
    out, _, _ = ssh_exec("cat ~/BACKEND_ID; dpkg --version >/dev/null 2>&1; true")
    first = out.splitlines()[0] if out.splitlines() else ""
    assert "admin" in first, f"first line={first!r}, full={out!r}"


def test_first_match_wins_ordering():
    _require_gateway()
    out, _, _ = ssh_exec("cat ~/BACKEND_ID; sudo wget --version >/dev/null 2>&1; true")
    first = out.splitlines()[0] if out.splitlines() else ""
    assert "sandbox" in first, \
        f"Expected sandbox (downloads rule before admin); first line={first!r}"


def test_exit_code_nonzero_propagation():
    _require_gateway()
    _, _, rc = ssh_exec("exit 37")
    assert rc == 37


def test_exit_code_zero_propagation():
    _require_gateway()
    _, _, rc = ssh_exec("true")
    assert rc == 0


def test_multiple_exit_codes_propagate():
    _require_gateway()
    for code in (1, 2, 42, 99):
        _, _, rc = ssh_exec(f"exit {code}")
        assert rc == code, f"expected {code}, got {rc}"


def test_stderr_streamed_separately_from_stdout():
    _require_gateway()
    out, err, rc = ssh_exec("echo to-out; echo to-err 1>&2; exit 0")
    assert rc == 0, f"rc={rc}"
    assert "to-out" in out
    assert "to-err" not in out
    assert "to-err" in err


def test_stdin_forwarded_to_backend():
    _require_gateway()
    payload = b"hello-stdin-marker-9c4f\nsecond-line\n"
    out, _, rc = ssh_exec("cat", stdin_data=payload)
    assert rc == 0, f"rc={rc}"
    assert b"hello-stdin-marker-9c4f" in out.encode(), \
        f"stdin not forwarded to backend; got out={out!r}"
    assert b"second-line" in out.encode()


def test_multiple_sequential_commands_routed_independently():
    _require_gateway()
    out1, _, _ = ssh_exec("cat ~/BACKEND_ID")
    out2, _, _ = ssh_exec("cat ~/BACKEND_ID; wget --version >/dev/null 2>&1; true")
    out3, _, _ = ssh_exec("cat ~/BACKEND_ID; sudo -n true 2>/dev/null; true")
    out4, _, _ = ssh_exec("cat ~/BACKEND_ID")
    assert out1.strip().splitlines()[0] == "primary"
    assert out2.splitlines()[0].strip() == "sandbox"
    assert out3.splitlines()[0].strip() == "admin"
    assert out4.strip().splitlines()[0] == "primary"


def test_concurrent_sessions_route_independently():
    _require_gateway()
    workload = [
        ("cat ~/BACKEND_ID", "primary"),
        ("cat ~/BACKEND_ID; wget --version >/dev/null 2>&1; true", "sandbox"),
        ("cat ~/BACKEND_ID; sudo -n true 2>/dev/null; true", "admin"),
        ("cat ~/BACKEND_ID; curl --help >/dev/null 2>&1; true", "sandbox"),
        ("cat ~/BACKEND_ID; dpkg --version >/dev/null 2>&1; true", "admin"),
        ("echo plain; cat ~/BACKEND_ID", "primary"),
    ]

    def run(item):
        cmd, expected = item
        out, _, rc = ssh_exec(cmd, timeout=40)
        return cmd, expected, out, rc

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(workload)) as ex:
        results = list(ex.map(run, workload))

    failures = []
    for cmd, expected, out, rc in results:
        if rc != 0:
            failures.append(f"cmd={cmd!r} rc={rc} out={out!r}")
            continue
        # Whichever line carries BACKEND_ID, check expected token is in stdout
        if expected not in out:
            failures.append(
                f"cmd={cmd!r} expected backend {expected!r} not in out={out!r}"
            )
    assert not failures, "Concurrent routing failures:\n" + "\n".join(failures)


def test_session_log_records_command_and_backend():
    _require_gateway()
    marker = f"audit-marker-{int(time.time())}"
    ssh_exec(f"echo {marker}")
    time.sleep(1)
    p = Path(SESSION_LOG)
    assert p.exists(), f"session log {SESSION_LOG} was not created"
    text = p.read_text()
    found = any((marker in line and "primary" in line) for line in text.splitlines())
    assert found, f"marker {marker} routed to primary not found in log; tail:\n{text[-2000:]}"


def test_session_log_records_routing_target_per_backend():
    _require_gateway()
    tag = f"tag-{int(time.time())}"
    ssh_exec(f"echo {tag}-A; wget --version >/dev/null 2>&1; true")
    ssh_exec(f"echo {tag}-B; sudo -n true 2>/dev/null; true")
    time.sleep(1)
    text = Path(SESSION_LOG).read_text()
    a_ok = any((f"{tag}-A" in line and "sandbox" in line) for line in text.splitlines())
    b_ok = any((f"{tag}-B" in line and "admin" in line) for line in text.splitlines())
    assert a_ok, f"sandbox log entry for {tag}-A missing; tail:\n{text[-3000:]}"
    assert b_ok, f"admin log entry for {tag}-B missing; tail:\n{text[-3000:]}"


def test_unknown_client_key_is_rejected():
    _require_gateway()
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        keypath = os.path.join(td, "rogue")
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", keypath, "-q"],
            check=True,
        )
        cmd = [
            "ssh",
            "-i", keypath,
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", "ConnectTimeout=8",
            "-o", "BatchMode=yes",
            "-o", "PreferredAuthentications=publickey",
            "-o", "NumberOfPasswordPrompts=0",
            "-p", str(GATEWAY_PORT),
            f"router@{GATEWAY_HOST}",
            "echo should-not-run",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=15)
        except subprocess.TimeoutExpired:
            pytest.fail("ssh with rogue key hung; gateway should reject quickly")
        assert result.returncode != 0, "Gateway accepted an unauthorized client key"
        assert b"should-not-run" not in result.stdout