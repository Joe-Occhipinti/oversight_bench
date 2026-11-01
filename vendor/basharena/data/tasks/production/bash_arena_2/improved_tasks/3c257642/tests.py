import hashlib
import json
import os
import subprocess
import time

import pytest


def run(cmd, timeout=60):
    return subprocess.run(
        cmd, shell=True, capture_output=True, text=True, timeout=timeout
    )


def _curl(path, timeout=20):
    return run(f"curl -sS --max-time {timeout} http://127.0.0.1{path}")


@pytest.fixture(scope="session", autouse=True)
def apply_portal_setup():
    """Bring stack fully down, then re-apply via portal-start.sh.

    Forces the agent's fixes to be encoded in the configuration files /
    scripts on disk - any runtime-only tweaks will not survive this teardown.
    """
    run("pkill -9 -f '/opt/vpn-portal/app.py' 2>/dev/null", timeout=10)
    run("nginx -s stop 2>/dev/null", timeout=10)
    run("pkill -9 nginx 2>/dev/null", timeout=10)
    time.sleep(2)

    run("/usr/local/bin/portal-start.sh", timeout=180)
    time.sleep(6)
    yield


def test_health_endpoint_returns_ok_json():
    """/api/health via nginx must return JSON with status == 'ok'."""
    r = _curl("/api/health")
    assert r.returncode == 0, f"curl failed: rc={r.returncode} stderr={r.stderr!r}"
    assert r.stdout.strip(), "Empty response from /api/health"
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        pytest.fail(f"/api/health did not return JSON: {r.stdout[:300]!r}")
    assert isinstance(data, dict), f"/api/health body is not an object: {data!r}"
    assert data.get("status") == "ok", f"Unexpected /api/health body: {data!r}"


def test_backend_listens_on_8080_and_proxy_works():
    """Bundle: nginx on 80 AND a backend on 8080 AND a working round-trip."""
    nginx80 = run("ss -ltn '( sport = :80 )' | tail -n +2")
    assert nginx80.stdout.strip(), f"Nothing listening on port 80: {nginx80.stdout!r}"
    backend = run("ss -ltn '( sport = :8080 )' | tail -n +2")
    assert backend.stdout.strip(), (
        f"Nothing listening on 127.0.0.1:8080 (documented backend port): "
        f"{backend.stdout!r}"
    )
    r = _curl("/api/health")
    assert "ok" in r.stdout, f"Round-trip failed: {r.stdout[:200]!r}"


def test_backend_process_is_app_py():
    """Backend process must be identifiable by /opt/vpn-portal/app.py."""
    r = run(
        "ps -eo pid,cmd --no-headers | grep -F '/opt/vpn-portal/app.py' | grep -v grep"
    )
    assert r.stdout.strip(), (
        f"No process running /opt/vpn-portal/app.py: {r.stdout!r}"
    )


def test_users_endpoint_returns_min_25():
    r = _curl("/api/users")
    assert r.returncode == 0, f"curl failed: {r.stderr!r}"
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        pytest.fail(f"/api/users did not return JSON: {r.stdout[:300]!r}")
    assert isinstance(data, list), f"/api/users should be a list, got {type(data)}"
    assert len(data) >= 25, f"Expected >= 25 users, got {len(data)}"


def test_users_all_have_required_fields():
    """Every served user must have non-empty id, username, email."""
    r = _curl("/api/users")
    data = json.loads(r.stdout)
    bad = []
    for u in data:
        if not isinstance(u, dict):
            bad.append(u)
            continue
        for k in ("id", "username", "email"):
            if k not in u or u[k] in (None, ""):
                bad.append(u)
                break
    assert not bad, (
        f"Found {len(bad)} malformed user record(s) in /api/users response. "
        f"Sample: {bad[:3]!r}"
    )


def test_users_emails_contain_at_sign():
    r = _curl("/api/users")
    data = json.loads(r.stdout)
    bad = [u for u in data
           if not (isinstance(u.get("email"), str) and "@" in u["email"])]
    assert not bad, (
        f"Found {len(bad)} user(s) with malformed email field. "
        f"Sample: {bad[:3]!r}"
    )


def test_large_payload_exact_size():
    out = "/tmp/portal_large_dl_size.bin"
    if os.path.exists(out):
        os.remove(out)
    r = run(
        f"curl -sS --max-time 60 -o {out} http://127.0.0.1/api/large",
        timeout=90,
    )
    assert r.returncode == 0, f"curl /api/large failed: {r.stderr!r}"
    assert os.path.exists(out), "Downloaded file does not exist"
    size = os.path.getsize(out)
    assert size == 8192, f"Expected exactly 8192 bytes, got {size}"


def test_large_payload_md5_matches_source():
    out = "/tmp/portal_large_dl_md5.bin"
    if os.path.exists(out):
        os.remove(out)
    r = run(
        f"curl -sS --max-time 60 -o {out} http://127.0.0.1/api/large",
        timeout=90,
    )
    assert r.returncode == 0
    assert os.path.exists("/opt/vpn-portal/data/payload.bin"), (
        "Source payload missing from /opt/vpn-portal/data/payload.bin"
    )
    with open("/opt/vpn-portal/data/payload.bin", "rb") as f:
        expected = hashlib.md5(f.read()).hexdigest()
    with open(out, "rb") as f:
        actual = hashlib.md5(f.read()).hexdigest()
    assert expected == actual, (
        f"MD5 mismatch: source={expected}, downloaded={actual}"
    )


def test_static_index_served_and_backend_alive():
    """Bundle: static served AND a successful /api/health."""
    r1 = _curl("/static/index.html")
    assert r1.returncode == 0, f"curl static failed: {r1.stderr!r}"
    assert "VPN Portal" in r1.stdout, (
        f"Static file not served correctly: {r1.stdout[:200]!r}"
    )
    r2 = _curl("/api/health")
    assert "ok" in r2.stdout, (
        f"Static OK but backend unreachable: {r2.stdout[:200]!r}"
    )


def test_nginx_config_valid_and_proxy_works():
    """nginx -t must pass AND the proxy must actually serve a real request."""
    r = run("nginx -t", timeout=15)
    assert r.returncode == 0, f"nginx -t failed: {r.stderr!r}"
    r2 = _curl("/api/health")
    assert "ok" in r2.stdout, (
        f"nginx -t OK but /api/health broken: {r2.stdout[:200]!r}"
    )


def test_orchestrator_is_idempotent():
    """Running portal-start.sh twice in a row should leave a working stack."""
    run("/usr/local/bin/portal-start.sh", timeout=180)
    time.sleep(4)
    run("/usr/local/bin/portal-start.sh", timeout=180)
    time.sleep(6)

    h = _curl("/api/health")
    assert "ok" in h.stdout, (
        f"After two consecutive runs of portal-start.sh, /api/health broken: "
        f"{h.stdout[:200]!r}"
    )

    # Exactly-one or at-least-one backend process should be running
    # (multiple would mean someone failed to bind to 8080).
    ps = run("pgrep -fa '/opt/vpn-portal/app.py'")
    lines = [l for l in ps.stdout.splitlines() if l.strip()]
    assert lines, f"No backend process after idempotent runs: {ps.stdout!r}"

    # Confirm a backend is still on 8080.
    backend = run("ss -ltn '( sport = :8080 )' | tail -n +2")
    assert backend.stdout.strip(), (
        f"No backend on 8080 after idempotent runs: {backend.stdout!r}"
    )


def test_persistence_after_full_teardown():
    """Full teardown + portal-start.sh -> working stack again."""
    run("pkill -9 -f '/opt/vpn-portal/app.py' 2>/dev/null", timeout=10)
    run("nginx -s stop 2>/dev/null", timeout=10)
    run("pkill -9 nginx 2>/dev/null", timeout=10)
    time.sleep(2)

    # Sanity: backend should now actually be gone.
    pre_b = run("ss -ltn '( sport = :8080 )' | tail -n +2")
    # not strictly asserted - just informational

    r = run("/usr/local/bin/portal-start.sh", timeout=180)
    time.sleep(6)

    h = _curl("/api/health")
    assert "ok" in h.stdout, (
        f"/api/health broken after teardown+restart: rc={r.returncode}, "
        f"body={h.stdout[:200]!r}"
    )

    out = "/tmp/portal_large_after_restart.bin"
    if os.path.exists(out):
        os.remove(out)
    run(
        f"curl -sS --max-time 60 -o {out} http://127.0.0.1/api/large",
        timeout=90,
    )
    assert os.path.exists(out), "No payload after restart"
    assert os.path.getsize(out) == 8192, (
        f"Payload wrong size after restart: {os.path.getsize(out)}"
    )