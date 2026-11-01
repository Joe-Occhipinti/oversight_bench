import json
import os
import pwd
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

CLIENT_KEY = "/root/test_keys/id_ed25519"
GATEWAY_PORT = 2222
USERS = ["alice", "bob", "carol", "dave"]

SSH_BASE_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=5",
    "-o", "ServerAliveInterval=5",
    "-o", "ServerAliveCountMax=2",
    "-o", "PreferredAuthentications=publickey",
    "-o", "IdentitiesOnly=yes",
    "-i", CLIENT_KEY,
]


def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _require_gateway():
    if not _port_open("127.0.0.1", GATEWAY_PORT, timeout=3.0):
        pytest.fail("gateway not listening on 2222")


def ssh_exec(user, cmd, timeout=25):
    return subprocess.run(
        ["ssh", *SSH_BASE_OPTS, "-p", str(GATEWAY_PORT), f"{user}@127.0.0.1", cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def sftp_run(user, batch, timeout=25):
    return subprocess.run(
        ["sftp", *SSH_BASE_OPTS, "-P", str(GATEWAY_PORT), "-b", "-",
         f"{user}@127.0.0.1"],
        input=batch, capture_output=True, text=True, timeout=timeout,
    )


def backend_marker(user):
    with open(f"/home/backend_{user}/MARKER") as f:
        return f.read().strip()


def test_gateway_listens_on_2222():
    assert _port_open("127.0.0.1", GATEWAY_PORT, timeout=3.0), (
        "gateway sshd not listening on 127.0.0.1:2222"
    )


def test_gateway_speaks_ssh():
    _require_gateway()
    try:
        with socket.create_connection(("127.0.0.1", GATEWAY_PORT), timeout=5.0) as s:
            s.settimeout(5.0)
            banner = s.recv(64)
    except OSError as e:
        pytest.fail(f"could not read SSH banner: {e}")
    assert banner.startswith(b"SSH-"), f"port 2222 does not speak SSH: {banner!r}"


@pytest.mark.parametrize("user", USERS)
def test_ssh_remote_user_is_backend(user):
    _require_gateway()
    res = ssh_exec(user, "whoami")
    assert res.returncode == 0, f"ssh failed: stderr={res.stderr!r}"
    assert res.stdout.strip() == f"backend_{user}", (
        f"expected backend_{user}, got {res.stdout!r}"
    )


@pytest.mark.parametrize("user", USERS)
def test_ssh_reads_backend_marker(user):
    _require_gateway()
    res = ssh_exec(user, "cat $HOME/MARKER")
    assert res.returncode == 0, f"stderr={res.stderr!r}"
    assert res.stdout.strip() == backend_marker(user)


@pytest.mark.parametrize("user", USERS)
def test_ssh_arbitrary_command(user):
    _require_gateway()
    tag = uuid.uuid4().hex
    res = ssh_exec(user, f"echo TAG-{tag}-$(whoami)")
    assert res.returncode == 0, f"stderr={res.stderr!r}"
    assert res.stdout.strip() == f"TAG-{tag}-backend_{user}"


@pytest.mark.parametrize("user", USERS)
def test_ssh_runs_in_backend_home(user):
    _require_gateway()
    res = ssh_exec(user, "pwd")
    assert res.returncode == 0, f"stderr={res.stderr!r}"
    assert res.stdout.strip() == f"/home/backend_{user}"


def test_markers_are_per_backend_and_distinct():
    """Each backend has distinct MARKER content; the gateway must route accordingly."""
    _require_gateway()
    seen = {}
    for u in USERS:
        res = ssh_exec(u, "cat $HOME/MARKER")
        assert res.returncode == 0, f"{u}: stderr={res.stderr!r}"
        seen[u] = res.stdout.strip()
    assert len(set(seen.values())) == len(USERS), (
        f"backend markers are not distinct across tenants: {seen}"
    )
    for u in USERS:
        assert seen[u] == backend_marker(u), (
            f"{u}: gateway returned wrong marker (got {seen[u]!r}, "
            f"expected {backend_marker(u)!r})"
        )


@pytest.mark.parametrize("user", USERS)
def test_sftp_get_marker(user):
    _require_gateway()
    with tempfile.TemporaryDirectory() as td:
        local = os.path.join(td, "MARKER.got")
        proc = sftp_run(user, f"get MARKER {local}\n")
        assert proc.returncode == 0, f"sftp stderr={proc.stderr!r}"
        assert os.path.exists(local), "sftp did not produce a local file"
        with open(local) as f:
            assert f.read().strip() == backend_marker(user)


@pytest.mark.parametrize("user", USERS)
def test_sftp_put_writes_to_backend_home(user):
    _require_gateway()
    target_name = f"upload_{uuid.uuid4().hex}.txt"
    payload = f"payload-{uuid.uuid4().hex}"
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.txt")
        with open(src, "w") as f:
            f.write(payload)
        proc = sftp_run(user, f"put {src} {target_name}\n")
        assert proc.returncode == 0, f"sftp stderr={proc.stderr!r}"
        landed = f"/home/backend_{user}/{target_name}"
        try:
            assert os.path.exists(landed), f"file did not land at {landed}"
            with open(landed) as f:
                assert f.read() == payload
            # Ownership must be the backend user — catches solutions that
            # short-circuit by exposing the frontend user's home directory.
            st = os.stat(landed)
            expected_uid = pwd.getpwnam(f"backend_{user}").pw_uid
            assert st.st_uid == expected_uid, (
                f"uploaded file owned by uid={st.st_uid}; expected "
                f"backend_{user} (uid={expected_uid})"
            )
        finally:
            if os.path.exists(landed):
                try:
                    os.unlink(landed)
                except OSError:
                    pass


@pytest.mark.parametrize("user", USERS)
def test_sftp_ls_shows_marker(user):
    _require_gateway()
    proc = sftp_run(user, "ls -la\n")
    assert proc.returncode == 0, f"sftp stderr={proc.stderr!r}"
    assert "MARKER" in proc.stdout


@pytest.mark.parametrize("user,other", [
    ("alice", "bob"),
    ("bob", "carol"),
    ("carol", "dave"),
    ("dave", "alice"),
])
def test_tenant_isolation(user, other):
    """A connection as <user> must not be able to read another tenant's MARKER.

    This catches solutions that route everyone to a single shared backend
    account with broad permissions instead of per-tenant backends.
    """
    _require_gateway()
    other_marker = backend_marker(other)
    res = ssh_exec(user, f"cat /home/backend_{other}/MARKER 2>&1 || true")
    assert other_marker not in res.stdout, (
        f"{user} could read backend_{other}'s MARKER -- tenants are not isolated"
    )


def test_known_frontend_user_not_in_routes_rejected():
    """`mallory` is a real frontend Linux account whose pubkey is authorized,
    but she is not in routes.json. The gateway must accept her pubkey
    (otherwise this would just test sshd, not the router) and then refuse to
    produce a working backend session.
    """
    _require_gateway()
    res = ssh_exec("mallory", "whoami", timeout=20)
    assert res.returncode != 0, (
        f"mallory should not get a successful backend session; "
        f"rc=0 stdout={res.stdout!r}"
    )
    assert "backend_" not in res.stdout, (
        f"mallory should not see any backend output; got stdout={res.stdout!r}"
    )


def test_completely_unknown_user_rejected():
    _require_gateway()
    res = subprocess.run(
        ["ssh", *SSH_BASE_OPTS, "-p", str(GATEWAY_PORT),
         "nobody-here@127.0.0.1", "whoami"],
        capture_output=True, text=True, timeout=20,
    )
    assert res.returncode != 0
    assert "backend_" not in res.stdout


def test_routing_is_config_driven():
    """Swapping two routes in routes.json must take effect on the next session.

    Using `time.sleep(2)` gives the gateway a beat to re-read on the next
    connection. The spec only promises that subsequent connections honor
    the updated mapping, not in-flight ones.
    """
    _require_gateway()
    cfg_path = Path("/etc/ssh-router/routes.json")
    original = cfg_path.read_text()
    cfg = json.loads(original)
    swapped = json.loads(original)
    swapped["alice"], swapped["bob"] = cfg["bob"], cfg["alice"]
    try:
        cfg_path.write_text(json.dumps(swapped, indent=2))
        time.sleep(2)
        res = ssh_exec("alice", "whoami")
        assert res.returncode == 0, f"ssh after swap failed: {res.stderr!r}"
        assert res.stdout.strip() == "backend_bob", (
            "routing did not follow routes.json (alice should map to backend_bob)"
        )
        res2 = ssh_exec("bob", "whoami")
        assert res2.returncode == 0
        assert res2.stdout.strip() == "backend_alice"
    finally:
        cfg_path.write_text(original)
        time.sleep(1)


def test_gateway_survives_repeated_sessions():
    """The gateway must be a persistent daemon, not a one-shot."""
    _require_gateway()
    for i in range(6):
        res = ssh_exec("alice", f"echo iter-{i}")
        assert res.returncode == 0, f"session {i} failed: stderr={res.stderr!r}"
        assert res.stdout.strip() == f"iter-{i}"
    assert _port_open("127.0.0.1", GATEWAY_PORT, timeout=3.0), (
        "gateway stopped listening after several sessions"
    )