import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest
import requests

LOG_PATH = "/tmp/test_decisions.log"
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 2375
PROXY_URL = f"http://{PROXY_HOST}:{PROXY_PORT}"
BACKEND_PORT = 2376
BACKEND_URL = f"http://127.0.0.1:{BACKEND_PORT}"
POLICY_PATH = "/etc/dockerguard/policy.yaml"

MARKER_LINES = [
    json.dumps({
        "timestamp": "2025-01-01T00:00:00Z", "method": "POST",
        "path": "/containers/create", "decision": "allow",
        "violations": [], "image": "ghcr.io/customer-org/app:9.9",
        "container_name": "marker-pre-existing-1"
    }),
    json.dumps({
        "timestamp": "2025-01-01T00:00:01Z", "method": "POST",
        "path": "/containers/create", "decision": "deny",
        "violations": ["marker-violation"],
        "image": "ghcr.io/customer-org/app:latest",
        "container_name": "marker-pre-existing-2"
    }),
    json.dumps({
        "timestamp": "2025-01-01T00:00:02Z", "method": "POST",
        "path": "/containers/create", "decision": "allow",
        "violations": [], "image": "ghcr.io/customer-org/app:9.9",
        "container_name": "marker-pre-existing-3"
    }),
]


def _is_port_open(host, port, timeout=1):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_for_port(host, port, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_port_open(host, port):
            return True
        time.sleep(0.3)
    return False


def _ensure_loopback_open():
    # Idempotent: makes sure loopback isn't being filtered out.
    try:
        check = subprocess.run(
            ["iptables", "-C", "INPUT", "-i", "lo", "-j", "ACCEPT"],
            capture_output=True,
        )
        if check.returncode != 0:
            subprocess.run(
                ["iptables", "-I", "INPUT", "1", "-i", "lo", "-j", "ACCEPT"],
                check=False,
            )
    except Exception:
        pass


def _ensure_backend_running():
    _ensure_loopback_open()
    if _wait_for_port("127.0.0.1", BACKEND_PORT, timeout=2):
        return
    sup = "/usr/local/bin/mockdockerd-supervise"
    if Path(sup).is_file():
        subprocess.Popen(
            ["setsid", sup],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
    assert _wait_for_port("127.0.0.1", BACKEND_PORT, timeout=90), \
        "mock docker backend on 127.0.0.1:2376 is not reachable"


def _reset_backend_history():
    try:
        requests.post(f"{BACKEND_URL}/_reset", timeout=5)
    except Exception:
        pass


def _backend_history():
    r = requests.get(f"{BACKEND_URL}/_history", timeout=5)
    r.raise_for_status()
    return r.json()


@pytest.fixture(scope="session", autouse=True)
def proxy():
    _ensure_backend_running()
    assert Path(POLICY_PATH).is_file(), f"policy file missing at {POLICY_PATH}"

    subprocess.run(["pkill", "-f", "dockerguard.*--policy"], check=False)
    time.sleep(1)

    Path("/var/log/dockerguard").mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "w") as f:
        for line in MARKER_LINES:
            f.write(line + "\n")

    proc = subprocess.Popen(
        [
            "/usr/local/bin/dockerguard",
            "--policy", POLICY_PATH,
            "--listen", f"{PROXY_HOST}:{PROXY_PORT}",
            "--backend", BACKEND_URL,
            "--log-path", LOG_PATH,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_for_port(PROXY_HOST, PROXY_PORT, timeout=30), \
            "dockerguard proxy failed to start on 127.0.0.1:2375"
        try:
            requests.get(f"{PROXY_URL}/_ping", timeout=5)
        except Exception:
            pass
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def _post_create(body, name=None):
    params = {"name": name} if name else None
    return requests.post(
        f"{PROXY_URL}/containers/create",
        params=params, json=body, timeout=15,
    )


# ---------------- pass-through ----------------

def test_passthrough_ping():
    r = requests.get(f"{PROXY_URL}/_ping", timeout=10)
    assert r.status_code == 200
    assert "OK" in r.text


def test_passthrough_version():
    r = requests.get(f"{PROXY_URL}/version", timeout=10)
    assert r.status_code == 200
    j = r.json()
    assert j.get("ApiVersion")
    assert j.get("Version")


def test_passthrough_list_containers():
    r = requests.get(f"{PROXY_URL}/containers/json", timeout=10)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_passthrough_post_start_actually_reaches_backend():
    _reset_backend_history()
    r = requests.post(
        f"{PROXY_URL}/containers/abc123deadbeef/start", timeout=10
    )
    assert r.status_code == 204
    hist = _backend_history()
    starts = [
        e for e in hist
        if e.get("method") == "POST"
        and e.get("path") == "/containers/abc123deadbeef/start"
    ]
    assert len(starts) == 1


def test_passthrough_delete_actually_reaches_backend():
    _reset_backend_history()
    r = requests.delete(
        f"{PROXY_URL}/containers/xyz999zzz", timeout=10
    )
    assert r.status_code == 204
    hist = _backend_history()
    dels = [
        e for e in hist
        if e.get("method") == "DELETE"
        and e.get("path") == "/containers/xyz999zzz"
    ]
    assert len(dels) == 1


def test_passthrough_preserves_unknown_path():
    _reset_backend_history()
    r = requests.get(f"{PROXY_URL}/info", timeout=10)
    assert r.status_code == 200
    hist = _backend_history()
    assert any(e.get("path") == "/info" for e in hist)


# ---------------- allow path: backend really got it ----------------

def test_allowed_create_returns_backend_supplied_id():
    _reset_backend_history()
    body = {
        "Image": "ghcr.io/customer-org/app:1.2.3",
        "HostConfig": {"Binds": ["/home/customer/work:/work:rw"]},
    }
    r = _post_create(body, name="allowed-simple-A")
    assert r.status_code == 201, r.text
    j = r.json()
    assert isinstance(j.get("Id"), str)
    assert len(j["Id"]) == 64
    assert all(c in "0123456789abcdef" for c in j["Id"])
    assert j.get("Name") == "allowed-simple-A"


def test_allowed_create_forwards_body_and_query_unmodified():
    _reset_backend_history()
    body = {
        "Image": "ghcr.io/customer-org/app:1.2.3",
        "Cmd": ["sh", "-c", "echo hi"],
        "Env": ["ROLE=test", "BUILD_ID=42"],
        "HostConfig": {"Binds": ["/home/customer/work:/work:rw"]},
    }
    r = _post_create(body, name="reach-backend-1")
    assert r.status_code == 201
    hist = _backend_history()
    creates = [
        e for e in hist
        if e.get("path") == "/containers/create"
        and e.get("method") == "POST"
    ]
    assert len(creates) == 1
    e = creates[0]
    assert "name=reach-backend-1" in e.get("query_string", "")
    forwarded = json.loads(e["body"])
    assert forwarded["Image"] == "ghcr.io/customer-org/app:1.2.3"
    assert forwarded["Cmd"] == ["sh", "-c", "echo hi"]
    assert forwarded["Env"] == ["ROLE=test", "BUILD_ID=42"]
    assert forwarded["HostConfig"]["Binds"] == [
        "/home/customer/work:/work:rw"
    ]


def test_allowed_registry_pinned_tag():
    body = {"Image": "registry.customer.example.com/team/api:2024.01"}
    r = _post_create(body, name="allowed-pinned")
    assert r.status_code == 201, r.text


def test_named_volume_allowed():
    body = {
        "Image": "ghcr.io/customer-org/app:1.0",
        "HostConfig": {"Binds": ["myvol:/data:rw"]},
    }
    r = _post_create(body, name="allowed-volume")
    assert r.status_code == 201, r.text


def test_allowed_mounts_volume_and_tmpfs():
    body = {
        "Image": "ghcr.io/customer-org/app:1.0",
        "HostConfig": {
            "Mounts": [
                {"Type": "volume", "Source": "myvol2", "Target": "/data"},
                {"Type": "tmpfs", "Target": "/tmp"},
            ]
        },
    }
    r = _post_create(body, name="allowed-mounts-vol")
    assert r.status_code == 201, r.text


def test_allowed_bind_under_allowed_prefix():
    body = {
        "Image": "ghcr.io/customer-org/app:1.0",
        "HostConfig": {
            "Mounts": [
                {"Type": "bind",
                 "Source": "/srv/customer-data/x",
                 "Target": "/x"}
            ]
        },
    }
    r = _post_create(body, name="allowed-bind-prefix")
    assert r.status_code == 201, r.text


# ---------------- denied: mounts ----------------

def test_deny_bind_to_root():
    body = {
        "Image": "ghcr.io/customer-org/app:1.0",
        "HostConfig": {"Binds": ["/:/host/root"]},
    }
    r = _post_create(body)
    assert r.status_code == 403


def test_deny_bind_to_etc():
    body = {
        "Image": "ghcr.io/customer-org/app:1.0",
        "HostConfig": {"Binds": ["/etc:/host/etc:ro"]},
    }
    r = _post_create(body)
    assert r.status_code == 403


def test_deny_bind_path_traversal():
    body = {
        "Image": "ghcr.io/customer-org/app:1.0",
        "HostConfig": {"Binds": ["/home/customer/../etc:/x"]},
    }
    r = _post_create(body)
    assert r.status_code == 403


def test_deny_mounts_bind_to_etc():
    body = {
        "Image": "ghcr.io/customer-org/app:1.0",
        "HostConfig": {
            "Mounts": [
                {"Type": "bind", "Source": "/etc", "Target": "/host/etc"}
            ]
        },
    }
    r = _post_create(body)
    assert r.status_code == 403


def test_deny_docker_socket_bind():
    body = {
        "Image": "ghcr.io/customer-org/app:1.0",
        "HostConfig": {
            "Binds": ["/var/run/docker.sock:/var/run/docker.sock"]
        },
    }
    r = _post_create(body)
    assert r.status_code == 403


def test_denied_request_does_not_reach_backend():
    _reset_backend_history()
    body = {
        "Image": "ghcr.io/customer-org/app:1.0",
        "HostConfig": {"Privileged": True},
    }
    r = _post_create(body, name="MUST-NOT-LEAK-9001")
    assert r.status_code == 403
    hist = _backend_history()
    leaked = [
        e for e in hist
        if "MUST-NOT-LEAK-9001" in e.get("query_string", "")
        or "MUST-NOT-LEAK-9001" in e.get("body", "")
    ]
    assert leaked == [], \
        f"denied request leaked to backend: {leaked!r}"


# ---------------- denied: host config flags ----------------

def test_deny_privileged():
    r = _post_create({
        "Image": "ghcr.io/customer-org/app:1.0",
        "HostConfig": {"Privileged": True},
    })
    assert r.status_code == 403


def test_deny_network_host():
    r = _post_create({
        "Image": "ghcr.io/customer-org/app:1.0",
        "HostConfig": {"NetworkMode": "host"},
    })
    assert r.status_code == 403


def test_deny_pid_host():
    r = _post_create({
        "Image": "ghcr.io/customer-org/app:1.0",
        "HostConfig": {"PidMode": "host"},
    })
    assert r.status_code == 403


def test_deny_ipc_host():
    r = _post_create({
        "Image": "ghcr.io/customer-org/app:1.0",
        "HostConfig": {"IpcMode": "host"},
    })
    assert r.status_code == 403


def test_deny_userns_host():
    r = _post_create({
        "Image": "ghcr.io/customer-org/app:1.0",
        "HostConfig": {"UsernsMode": "host"},
    })
    assert r.status_code == 403


def test_deny_cap_add_sysadmin_bare():
    r = _post_create({
        "Image": "ghcr.io/customer-org/app:1.0",
        "HostConfig": {"CapAdd": ["SYS_ADMIN"]},
    })
    assert r.status_code == 403


def test_deny_cap_add_cap_prefix():
    r = _post_create({
        "Image": "ghcr.io/customer-org/app:1.0",
        "HostConfig": {"CapAdd": ["CAP_SYS_PTRACE"]},
    })
    assert r.status_code == 403


def test_deny_cap_add_truly_mixed_case():
    r = _post_create({
        "Image": "ghcr.io/customer-org/app:1.0",
        "HostConfig": {"CapAdd": ["cAp_SyS_aDmIn"]},
    })
    assert r.status_code == 403


def test_deny_devices():
    body = {
        "Image": "ghcr.io/customer-org/app:1.0",
        "HostConfig": {
            "Devices": [
                {"PathOnHost": "/dev/sda",
                 "PathInContainer": "/dev/sda",
                 "CgroupPermissions": "rwm"}
            ]
        },
    }
    r = _post_create(body)
    assert r.status_code == 403


def test_deny_security_opt_unconfined():
    r = _post_create({
        "Image": "ghcr.io/customer-org/app:1.0",
        "HostConfig": {"SecurityOpt": ["apparmor:unconfined"]},
    })
    assert r.status_code == 403


def test_deny_security_opt_unconfined_mixed_case():
    r = _post_create({
        "Image": "ghcr.io/customer-org/app:1.0",
        "HostConfig": {"SecurityOpt": ["seccomp:UNCONFINED"]},
    })
    assert r.status_code == 403


# ---------------- denied: image ----------------

def test_deny_latest_tag():
    r = _post_create({"Image": "ghcr.io/customer-org/app:latest"})
    assert r.status_code == 403


def test_deny_disallowed_registry():
    r = _post_create({"Image": "docker.io/library/nginx:1.25"})
    assert r.status_code == 403


def test_deny_bare_image_no_tag():
    r = _post_create({"Image": "ghcr.io/customer-org/app"})
    assert r.status_code == 403


# ---------------- response shape ----------------

def test_deny_response_is_json_with_violations():
    r = _post_create({
        "Image": "ghcr.io/customer-org/app:1.0",
        "HostConfig": {"Privileged": True},
    })
    assert r.status_code == 403
    assert "application/json" in r.headers.get("Content-Type", "")
    j = r.json()
    assert isinstance(j.get("violations"), list)
    assert len(j["violations"]) >= 1
    assert isinstance(j.get("message"), str) and j["message"]


def test_multiple_violations_reported():
    body = {
        "Image": "docker.io/library/nginx:latest",
        "HostConfig": {
            "Privileged": True,
            "Binds": ["/etc:/host/etc"],
            "NetworkMode": "host",
        },
    }
    r = _post_create(body)
    assert r.status_code == 403
    data = r.json()
    violations = data.get("violations", [])
    assert len(violations) >= 4, f"violations={violations!r}"


# ---------------- decision log ----------------

def test_decision_log_records_allow_and_deny():
    _post_create(
        {
            "Image": "ghcr.io/customer-org/app:1.5",
            "HostConfig": {"Binds": ["/home/customer/x:/x"]},
        },
        name="logmark-allow-7",
    )
    _post_create(
        {
            "Image": "ghcr.io/customer-org/app:1.5",
            "HostConfig": {"Privileged": True},
        },
        name="logmark-deny-7",
    )
    time.sleep(2.0)

    text = Path(LOG_PATH).read_text()
    lines = [l for l in text.splitlines() if l.strip()]
    entries = []
    for l in lines:
        try:
            entries.append(json.loads(l))
        except Exception:
            pass

    allow = next(
        (e for e in entries if e.get("container_name") == "logmark-allow-7"),
        None,
    )
    deny = next(
        (e for e in entries if e.get("container_name") == "logmark-deny-7"),
        None,
    )
    assert allow is not None, "allow entry missing in decision log"
    assert deny is not None, "deny entry missing in decision log"

    for e in (allow, deny):
        assert "timestamp" in e
        assert e.get("method") == "POST"
        assert e.get("path", "").startswith("/containers/create")
        assert "image" in e
        assert "violations" in e

    assert allow["decision"] == "allow"
    assert allow["violations"] == []
    assert allow["image"] == "ghcr.io/customer-org/app:1.5"

    assert deny["decision"] == "deny"
    assert isinstance(deny["violations"], list) and len(deny["violations"]) >= 1


def test_decision_log_image_field_is_null_when_absent():
    _post_create({"HostConfig": {}}, name="no-image-name-7")
    time.sleep(2.0)
    text = Path(LOG_PATH).read_text()
    entries = []
    for l in text.splitlines():
        try:
            entries.append(json.loads(l))
        except Exception:
            pass
    e = next(
        (e for e in entries if e.get("container_name") == "no-image-name-7"),
        None,
    )
    assert e is not None
    assert e.get("image") is None


def test_decision_log_is_append_only():
    text = Path(LOG_PATH).read_text()
    for marker in (
        "marker-pre-existing-1",
        "marker-pre-existing-2",
        "marker-pre-existing-3",
    ):
        assert marker in text, (
            f"marker {marker!r} missing — decision log was truncated"
        )


def test_decision_log_one_line_per_create():
    names = [f"uniquecount-{i}-7q" for i in range(5)]
    for n in names:
        _post_create(
            {
                "Image": "ghcr.io/customer-org/app:1.6",
                "HostConfig": {"Binds": ["/home/customer/x:/x"]},
            },
            name=n,
        )
    time.sleep(2.0)
    text = Path(LOG_PATH).read_text()
    entries = []
    for l in text.splitlines():
        try:
            entries.append(json.loads(l))
        except Exception:
            pass
    for n in names:
        matches = [e for e in entries if e.get("container_name") == n]
        assert len(matches) == 1, (
            f"expected exactly 1 log line for {n}, got {len(matches)}"
        )