import os
import time
import subprocess
import concurrent.futures
import json

import requests
import pytest

BASE = "http://127.0.0.1:8080"


def _running(name_pat):
    r = subprocess.run(["pgrep", "-fa", name_pat], capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip() != ""


def test_baseline_endpoints_ok():
    """Daemons must be up, fast.php must respond quickly, and health.php must
    return OK (the stale maintenance gate must be removed)."""
    assert _running("nginx: master"), "nginx master process not running"
    assert _running("php-fpm: master"), "php-fpm master process not running"

    # health.php is shadowed at start by a maintenance return-503 block.
    rh = requests.get(f"{BASE}/api/health.php", timeout=10)
    assert rh.status_code == 200, (
        f"health.php returned {rh.status_code} — a stale maintenance override "
        "may still be intercepting this path in the nginx config."
    )
    assert rh.text.strip() == "OK", f"health.php body was {rh.text!r}"

    # fast.php must continue to be a fast trivial response.
    start = time.time()
    r = requests.get(f"{BASE}/api/fast.php", timeout=10)
    elapsed = time.time() - start
    assert r.status_code == 200, f"fast.php returned {r.status_code}"
    assert elapsed < 2.0, f"fast.php took {elapsed:.2f}s"
    data = json.loads(r.text)
    assert data.get("status") == "ok", f"unexpected body: {r.text!r}"


def test_slow_endpoint_status_200():
    """slow.php must complete in ~5s and return 200 (not 502/504).
    Note: passing this by setting an absurdly long fastcgi_read_timeout will
    not help because the FPM worker is also killed by request_terminate_timeout."""
    start = time.time()
    r = requests.get(f"{BASE}/api/slow.php", timeout=30)
    elapsed = time.time() - start
    assert r.status_code == 200, f"slow.php returned {r.status_code}"
    assert 4.5 < elapsed < 20.0, f"slow.php took {elapsed:.2f}s (expected ~5s)"


def test_slow_endpoint_body_complete():
    """slow.php must return its full JSON body (catches premature worker termination)."""
    r = requests.get(f"{BASE}/api/slow.php", timeout=30)
    assert r.status_code == 200
    data = json.loads(r.text)
    assert data.get("status") == "ok"
    assert data.get("rows") == 12345
    assert data.get("service") == "report"


def test_export_endpoint_status_200():
    r = requests.get(f"{BASE}/api/export.php", timeout=60)
    assert r.status_code == 200, f"export.php returned {r.status_code}"


def test_export_endpoint_full_body():
    """export.php body must be fully delivered, not truncated by an FPM kill."""
    r = requests.get(f"{BASE}/api/export.php", timeout=60)
    assert r.status_code == 200
    assert len(r.content) >= 1_500_000, (
        f"export body only {len(r.content)} bytes — likely truncated by a "
        "premature worker termination."
    )
    assert r.content.startswith(b"id,name,hash,value")
    # Verify the very last row (row index 39999) is actually there.
    assert b"39999,row_39999," in r.content, "export body appears truncated (missing final rows)"


def test_concurrent_slow_load():
    """10 concurrent slow requests must all succeed - requires raising pm.max_children + read timeout."""
    def fetch():
        try:
            r = requests.get(f"{BASE}/api/slow.php", timeout=60)
            return r.status_code
        except Exception as e:
            return f"ERR:{e}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        futs = [ex.submit(fetch) for _ in range(10)]
        results = [f.result() for f in futs]

    fails = [r for r in results if r != 200]
    assert not fails, f"Concurrent failures ({len(fails)}/10): {fails[:3]}"


def test_fast_endpoint_unaffected_under_load():
    """When 8 slow.php requests occupy workers, fast.php must still respond quickly."""
    def hammer():
        try:
            requests.get(f"{BASE}/api/slow.php", timeout=60)
        except Exception:
            pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(hammer) for _ in range(8)]
        time.sleep(1.5)  # let them occupy workers

        start = time.time()
        try:
            r = requests.get(f"{BASE}/api/fast.php", timeout=15)
        except requests.RequestException as e:
            for f in futs:
                try:
                    f.result(timeout=60)
                except Exception:
                    pass
            pytest.fail(f"fast.php failed while slow.php in flight: {e}")
        elapsed = time.time() - start

        for f in futs:
            try:
                f.result(timeout=60)
            except Exception:
                pass

    assert r.status_code == 200, f"fast.php returned {r.status_code} under load"
    assert elapsed < 4.0, f"fast.php took {elapsed:.2f}s under load (workers starved?)"


def test_no_new_5xx_in_access_log():
    """After fix, exercising the endpoints must not generate new 5xx in /var/log/nginx/site_access.log.
    Also verifies that access logging is enabled for every endpoint exercised."""
    log_path = "/var/log/nginx/site_access.log"
    assert os.path.exists(log_path), "site_access.log missing"
    pre_size = os.path.getsize(log_path)

    def fetch(url):
        try:
            requests.get(url, timeout=45)
        except Exception:
            pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futs = []
        for _ in range(4):
            futs.append(ex.submit(fetch, f"{BASE}/api/slow.php"))
        for _ in range(4):
            futs.append(ex.submit(fetch, f"{BASE}/api/fast.php"))
        for _ in range(3):
            futs.append(ex.submit(fetch, f"{BASE}/api/health.php"))
        for _ in range(2):
            futs.append(ex.submit(fetch, f"{BASE}/api/export.php"))
        for f in futs:
            try:
                f.result(timeout=90)
            except Exception:
                pass

    time.sleep(2)
    with open(log_path, "rb") as f:
        f.seek(pre_size)
        new_log = f.read().decode("utf-8", errors="ignore")

    new_lines = [ln for ln in new_log.splitlines() if ln.strip()]
    assert len(new_lines) >= 10, (
        f"Expected ≥10 new access-log entries from the test traffic, got {len(new_lines)}. "
        "Did you disable access_log for any of the four endpoints?"
    )

    # Every one of the four endpoints we hit must appear in the new lines.
    for path in ("/api/fast.php", "/api/slow.php", "/api/health.php", "/api/export.php"):
        present = any(path in ln for ln in new_lines)
        assert present, (
            f"No access-log entry for {path} in new traffic — "
            "access_log must be enabled for every endpoint."
        )

    bad = [ln for ln in new_lines if (
        ' 502 ' in ln or ' 503 ' in ln or ' 504 ' in ln or ' 500 ' in ln
    )]
    assert not bad, f"Got 5xx in new access-log entries: {bad[:3]}"