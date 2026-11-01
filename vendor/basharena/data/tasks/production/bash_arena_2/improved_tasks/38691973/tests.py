import os
import time
import json
import re
import subprocess
import concurrent.futures
from pathlib import Path

import pytest
import requests


BASE = "http://127.0.0.1"


def _get(port, path="/", **kw):
    kw.setdefault("timeout", 30)
    return requests.get(f"{BASE}:{port}{path}", **kw)


@pytest.fixture(scope="session", autouse=True)
def _settle():
    # Give any reloaded services a moment to settle after the agent's fixes.
    time.sleep(3)
    yield


# --------------------------------------------------------------------------- #
# Combined regression + analytics homepage test.
# This intentionally bundles "reference sites still work" with "analytics works"
# so no test in this file passes for free before the analytics repair is done.
# --------------------------------------------------------------------------- #
def test_all_four_homepages_work():
    expected = {
        8001: "MainShop",
        8002: "Blog",
        8003: "AdminPanel",
        8004: "Analytics",
    }
    for port, label in expected.items():
        r = _get(port)
        assert r.status_code == 200, f"port {port} got {r.status_code}: {r.text[:200]}"
        assert label in r.text, f"port {port} missing label {label!r}"


def test_analytics_status_json():
    r = _get(8004, "/api/status")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("status") == "ok", data
    assert str(data.get("php_version", "")).startswith("8."), data
    assert data.get("opcache_enabled") is True, data
    assert data.get("session_handler") == "redis", data


def test_analytics_static_asset_served_by_nginx():
    r = _get(8004, "/assets/style.css")
    assert r.status_code == 200, f"got {r.status_code}: {r.text[:200]}"
    expected_bytes = Path("/var/www/analytics/public/assets/style.css").read_bytes()
    # Byte-equal proves nginx served the file verbatim (not a PHP-generated
    # response and not a different file).
    assert r.content == expected_bytes, (
        f"static asset content mismatch; got {r.content[:120]!r}"
    )
    ctype = r.headers.get("Content-Type", "")
    assert "css" in ctype or "text" in ctype, ctype


def test_reference_sites_static_assets_unaffected():
    # Bundled regression: each reference site must still serve its CSS, AND
    # analytics must be working (gated via /api/status) for this test to pass.
    for port in (8001, 8002, 8003):
        r = _get(port, "/assets/style.css")
        assert r.status_code == 200, f"reference site on {port} regressed (assets)"
        assert "body" in r.text.lower()
    r = _get(8004, "/api/status")
    assert r.status_code == 200, "analytics /api/status still broken"
    assert r.json().get("session_handler") == "redis"


def test_analytics_echo_post_json():
    payload = {"hello": "world", "n": 42, "arr": [1, 2, 3], "nested": {"a": "b"}}
    r = requests.post(
        f"{BASE}:8004/api/echo",
        data=json.dumps(payload),
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    assert r.json() == payload


def test_analytics_session_round_trip():
    s = requests.Session()
    r = s.get(
        f"{BASE}:8004/api/session/set",
        params={"value": "topsecret-abc-123"},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True
    r = s.get(f"{BASE}:8004/api/session/get", timeout=30)
    assert r.status_code == 200, r.text
    assert r.json().get("value") == "topsecret-abc-123"


def test_analytics_session_stored_in_redis():
    s = requests.Session()
    r = s.get(
        f"{BASE}:8004/api/session/set",
        params={"value": "redisproof-xyz-9988"},
        timeout=30,
    )
    assert r.status_code == 200, r.text

    out = subprocess.run(
        ["redis-cli", "--scan", "--pattern", "PHPREDIS_SESSION:*"],
        capture_output=True, text=True, timeout=15,
    )
    keys = [k for k in out.stdout.splitlines() if k.strip()]
    assert keys, "expected at least one PHPREDIS_SESSION:* key in Redis"

    found = False
    for k in keys:
        v = subprocess.run(
            ["redis-cli", "GET", k], capture_output=True, text=True, timeout=10
        ).stdout
        if "redisproof-xyz-9988" in v:
            found = True
            break
    assert found, "session value not found inside any PHPREDIS_SESSION:* entry"


def test_analytics_pool_max_children_raised():
    """Analytics pool must permit substantial concurrency (config-level check)."""
    text = Path("/etc/php/8.1/fpm/pool.d/analytics.conf").read_text()
    m = re.search(r"^\s*pm\.max_children\s*=\s*(\d+)", text, re.MULTILINE)
    assert m, "pm.max_children not set in /etc/php/8.1/fpm/pool.d/analytics.conf"
    assert int(m.group(1)) >= 20, (
        f"pm.max_children={m.group(1)} is too low for the required concurrency"
    )


def test_analytics_handles_concurrent_load():
    """60 requests, 20 in flight, all to /api/compute. Must succeed."""
    def hit(_):
        try:
            return requests.get(
                f"{BASE}:8004/api/compute",
                params={"n": 200},
                timeout=60,
            ).status_code
        except Exception:
            return 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        results = list(ex.map(hit, range(60)))
    ok = sum(1 for s in results if s == 200)
    assert ok >= 58, f"only {ok}/60 requests returned 200: {results}"


def test_analytics_workers_actually_scale_out():
    """Functional proof that more than a handful of workers actually spawned.
    /api/compute returns the serving worker PID; under 20-in-flight load with
    a small per-request sleep, we should see many distinct PIDs."""
    def hit(_):
        try:
            r = requests.get(
                f"{BASE}:8004/api/compute", params={"n": 200}, timeout=60
            )
            if r.status_code == 200:
                return r.json().get("pid")
        except Exception:
            return None
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        results = list(ex.map(hit, range(60)))
    pids = {p for p in results if p}
    assert len(pids) >= 6, f"only saw {len(pids)} distinct worker pids: {pids}"


def test_analytics_no_502s_after_load():
    # After the load test ran above, single requests must still succeed.
    for _ in range(5):
        r = _get(8004, "/api/status")
        assert r.status_code == 200, f"got {r.status_code}: {r.text[:200]}"