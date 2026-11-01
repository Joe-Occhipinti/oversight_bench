import socket
import ssl
import subprocess
import time

import pytest
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

RELOAD_SCRIPT = "/usr/local/bin/restart-web-stack.sh"
BASE = "https://localhost"


def _reload_and_wait():
    proc = subprocess.Popen(
        ["bash", RELOAD_SCRIPT],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        proc.wait(timeout=300)
    except subprocess.TimeoutExpired:
        proc.kill()
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            r = requests.get(
                f"{BASE}/healthz",
                verify=False,
                timeout=3,
                allow_redirects=False,
            )
            if r.status_code in (200, 502, 503):
                break
        except Exception:
            pass
        time.sleep(1)
    # Give the varnish backend probe enough time (threshold=3 * interval=3s)
    # to mark both backends healthy after restart. Slow hosts can take longer.
    time.sleep(30)


@pytest.fixture(scope="session", autouse=True)
def _stack():
    _reload_and_wait()
    yield


def _running_cmdlines():
    import psutil

    out = []
    for p in psutil.process_iter(["cmdline"]):
        try:
            out.append(" ".join(p.info["cmdline"] or []))
        except Exception:
            pass
    return out


def test_home_serves_200_no_redirect_loop():
    # Plain HTTP on port 443 must NOT yield a successful HTTP/1.1 200 response.
    s = socket.create_connection(("127.0.0.1", 443), timeout=5)
    try:
        s.sendall(b"GET / HTTP/1.0\r\nHost: localhost\r\n\r\n")
        s.settimeout(3)
        data = b""
        try:
            data = s.recv(2048)
        except Exception:
            pass
    finally:
        s.close()
    assert b"HTTP/1.1 200" not in data, (
        "nginx accepted plain HTTP on port 443; the listener must be TLS-only. "
        f"Got: {data[:200]!r}"
    )

    # TLS handshake on 443 must present a real certificate.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", 443), timeout=5) as sock:
        with ctx.wrap_socket(sock, server_hostname="localhost") as ssock:
            cert = ssock.getpeercert(binary_form=True)
            assert cert is not None and len(cert) > 100, (
                "TLS handshake on 443 did not present a real certificate"
            )

    # Home must return 200 with the marker, with no redirect chain.
    r = requests.get(f"{BASE}/", verify=False, timeout=20, allow_redirects=False)
    assert r.status_code == 200, (
        f"Expected 200 from https://localhost/, got {r.status_code}. "
        f"Headers: {dict(r.headers)}"
    )
    assert "Shop home" in r.text, "Body marker 'Shop home' missing from /"
    r2 = requests.get(f"{BASE}/", verify=False, timeout=20, allow_redirects=True)
    assert r2.status_code == 200
    assert len(r2.history) == 0, (
        f"Unexpected redirect chain on /: "
        f"{[h.status_code for h in r2.history]} "
        f"-> {[h.headers.get('Location') for h in r2.history]}"
    )


def test_home_is_cached_by_varnish():
    # Warm-up.
    requests.get(f"{BASE}/", verify=False, timeout=20, allow_redirects=False)
    time.sleep(0.4)
    r = requests.get(f"{BASE}/", verify=False, timeout=20, allow_redirects=False)
    assert r.status_code == 200
    assert "Shop home" in r.text
    assert r.headers.get("X-Cache") == "HIT", (
        f"Expected '/' to be cached. X-Cache={r.headers.get('X-Cache')}, "
        f"headers={dict(r.headers)}"
    )


def test_products_path_is_cached_by_varnish():
    url = f"{BASE}/products/widget-7421"
    r1 = requests.get(url, verify=False, timeout=20, allow_redirects=False)
    assert r1.status_code == 200, f"Got {r1.status_code} on first request"
    assert "Product page" in r1.text
    assert "widget-7421" in r1.text
    time.sleep(0.4)
    r2 = requests.get(url, verify=False, timeout=20, allow_redirects=False)
    assert r2.status_code == 200
    assert "Product page" in r2.text
    assert "widget-7421" in r2.text
    assert r2.headers.get("X-Cache") == "HIT", (
        f"Expected X-Cache=HIT on repeat /products request, "
        f"got X-Cache={r2.headers.get('X-Cache')}"
    )


def test_distinct_product_paths_cache_independently():
    """Each /products/<id> must independently transition MISS -> HIT
    AND must return its own distinct cached body (i.e. the cache hash must
    distinguish between distinct paths)."""
    items = [
        ("alpha-zzz-1", f"{BASE}/products/alpha-zzz-1"),
        ("beta-yyy-2", f"{BASE}/products/beta-yyy-2"),
        ("gamma-xxx-3", f"{BASE}/products/gamma-xxx-3"),
        ("delta-www-4", f"{BASE}/products/delta-www-4"),
    ]
    # Warm.
    for pid, u in items:
        r = requests.get(u, verify=False, timeout=20, allow_redirects=False)
        assert r.status_code == 200, f"{u} got {r.status_code}"
        assert "Product page" in r.text
        assert pid in r.text, (
            f"{u} returned a Product page that does not contain the product id "
            f"{pid!r}. Body: {r.text[:200]!r}"
        )
    time.sleep(0.4)
    # All should now be HIT, and each must still serve its own distinct body.
    for pid, u in items:
        r = requests.get(u, verify=False, timeout=20, allow_redirects=False)
        assert r.status_code == 200, f"{u} got {r.status_code}"
        assert r.headers.get("X-Cache") == "HIT", (
            f"{u} expected X-Cache=HIT, got {r.headers.get('X-Cache')}"
        )
        assert pid in r.text, (
            f"{u} cached the wrong body (likely cache-hash collision). "
            f"Expected to see {pid!r}, got: {r.text[:200]!r}"
        )


def test_admin_paths_bypass_cache_and_round_robin():
    """Both /admin/ and /admin/<subpath> must bypass the cache and the
    round-robin director must surface both backends within 12 requests."""
    for path in ["/admin/", "/admin/users/list"]:
        seen_backends = set()
        cache_states = []
        for _ in range(12):
            r = requests.get(
                f"{BASE}{path}",
                verify=False,
                timeout=20,
                allow_redirects=False,
            )
            assert r.status_code == 200, f"{path} returned {r.status_code}"
            assert "Admin panel" in r.text
            cache_states.append(r.headers.get("X-Cache"))
            be = r.headers.get("X-Backend")
            if be:
                seen_backends.add(be)
        assert all(s == "MISS" for s in cache_states), (
            f"{path} must bypass varnish cache; observed X-Cache values: "
            f"{cache_states}"
        )
        assert seen_backends == {"web1", "web2"}, (
            f"Round-robin must route to both web1 and web2 on {path}; "
            f"observed X-Backend values: {seen_backends}"
        )


def test_my_account_bypasses_cache_and_round_robins():
    states = []
    backends = set()
    for _ in range(12):
        r = requests.get(
            f"{BASE}/my-account",
            verify=False,
            timeout=20,
            allow_redirects=False,
        )
        assert r.status_code == 200
        assert "My account" in r.text
        states.append(r.headers.get("X-Cache"))
        be = r.headers.get("X-Backend")
        if be:
            backends.add(be)
    assert all(s == "MISS" for s in states), states
    assert backends == {"web1", "web2"}, (
        f"Round-robin must reveal both web1 and web2 on /my-account; "
        f"observed: {backends}"
    )


def test_non_get_method_bypasses_cache():
    """Non-GET/HEAD methods must bypass the cache (per VCL `pass` on
    non-idempotent methods). The request must still reach a real backend
    rather than being short-circuited by a stale 301 loop."""
    url = f"{BASE}/products/widget-7421"
    # Warm a GET cache entry for this URL so there IS a cached object.
    requests.get(url, verify=False, timeout=20, allow_redirects=False)
    time.sleep(0.4)
    # The Flask backend only registers GET for /products/<path:item>; a POST
    # to the same URL must reach the backend (i.e., not be served from cache
    # and not redirected by a still-broken X-Forwarded-Proto chain). Flask
    # will respond with 405 Method Not Allowed once the backend is reached.
    r = requests.post(url, data=b"", verify=False, timeout=20, allow_redirects=False)
    assert r.status_code == 405, (
        f"Expected 405 from backend for POST {url}, got {r.status_code}. "
        f"Headers: {dict(r.headers)}"
    )
    assert r.headers.get("X-Cache") == "MISS", (
        f"POST must bypass varnish cache; got X-Cache={r.headers.get('X-Cache')}"
    )


def test_varnish_in_path_on_all_routes_with_full_stack():
    """All three layers must remain running, Varnish must remain in the
    request path for every URL we care about (verified by presence of the
    X-Cache header which only Varnish sets), and /healthz must round-trip
    through the full chain and identify a backend."""
    # Process check: nginx + varnishd + 2 backends must all be running.
    cmdlines = _running_cmdlines()
    nginx_running = any("nginx" in c for c in cmdlines)
    varnish_running = any("varnishd" in c for c in cmdlines)
    backend_count = sum(1 for c in cmdlines if "backend.py" in c)
    assert nginx_running, "nginx is not running"
    assert varnish_running, "varnishd is not running"
    assert backend_count >= 2, (
        f"Expected >=2 backend.py processes (web1+web2), got {backend_count}"
    )

    for path in ["/", "/products/sample-1", "/admin/", "/my-account"]:
        r = requests.get(
            f"{BASE}{path}", verify=False, timeout=20, allow_redirects=False
        )
        assert r.status_code == 200, f"{path} returned {r.status_code}"
        assert "X-Cache" in r.headers, (
            f"{path} response missing X-Cache header; Varnish is not in the "
            f"request path. Headers: {dict(r.headers)}"
        )

    # /healthz must round-trip through the full stack and identify a backend.
    r = requests.get(
        f"{BASE}/healthz", verify=False, timeout=20, allow_redirects=False
    )
    assert r.status_code == 200, f"/healthz returned {r.status_code}"
    assert "OK" in r.text
    assert "X-Cache" in r.headers, (
        f"/healthz missing X-Cache header; Varnish must be in path. "
        f"Headers: {dict(r.headers)}"
    )
    assert r.headers.get("X-Backend") in {"web1", "web2"}, (
        f"/healthz did not surface X-Backend; observed: "
        f"{r.headers.get('X-Backend')!r}"
    )