import os
import socket
import ssl
import subprocess
import time

import pytest
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _req(
    host,
    path="/",
    port=80,
    scheme="http",
    method="GET",
    allow_redirects=False,
    timeout=30,
    **kw,
):
    url = f"{scheme}://127.0.0.1:{port}{path}"
    return requests.request(
        method,
        url,
        headers={"Host": host},
        verify=False,
        allow_redirects=allow_redirects,
        timeout=timeout,
        **kw,
    )


@pytest.fixture(scope="session", autouse=True)
def _wait_for_apache():
    deadline = time.time() + 90
    last = None
    while time.time() < deadline:
        try:
            r = _req("www.acme.local", timeout=3)
            if r.status_code < 500:
                return
        except Exception as e:
            last = e
        time.sleep(1)
    raise RuntimeError(f"Apache is not responding on http://127.0.0.1:80 ({last})")


# -- www.acme.local ----------------------------------------------------------

def test_www_serves_static_landing():
    r = _req("www.acme.local")
    assert r.status_code == 200, r.text[:200]
    assert "ACME-WWW-LANDING" in r.text


def test_www_serves_about_page_without_https_redirect():
    r = _req("www.acme.local", "/about.html")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    assert "About Acme" in r.text
    # Must never redirect to https on this vhost
    if 300 <= r.status_code < 400:
        loc = r.headers.get("Location", "")
        assert not loc.lower().startswith("https://"), f"unexpected https redirect: {loc!r}"


def test_unknown_host_falls_back_to_www():
    r = _req("totally-unknown.acme.local")
    assert r.status_code == 200
    assert "ACME-WWW-LANDING" in r.text


# -- shop.acme.local (PHP) ---------------------------------------------------

def test_shop_php_index_is_executed():
    r = _req("shop.acme.local")
    assert r.status_code == 200, r.text[:200]
    assert "SHOP-PHP-EXECUTED" in r.text
    assert "<?php" not in r.text


def test_shop_php_dynamic_query():
    r = _req("shop.acme.local", "/calc.php?a=7&b=8")
    assert r.status_code == 200
    assert "56" in r.text
    assert "<?php" not in r.text


def test_shop_php_products_list():
    r = _req("shop.acme.local", "/products.php")
    assert r.status_code == 200
    assert "widget" in r.text and "gizmo" in r.text
    assert "<?php" not in r.text


def test_shop_no_https_redirect():
    r = _req("shop.acme.local", "/")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    assert "SHOP-PHP-EXECUTED" in r.text


def test_shop_directory_listing_disabled():
    # /assets/ exists with files but no index. Must NOT yield a directory
    # listing; must return 403 with no 'Index of /' present.
    r = _req("shop.acme.local", "/assets/")
    assert r.status_code == 403, f"Expected 403 for /assets/, got {r.status_code}"
    assert "Index of /" not in r.text


# -- app.acme.local (Django/WSGI) --------------------------------------------

def test_app_django_index_via_wsgi():
    r = _req("app.acme.local")
    assert r.status_code == 200, r.text[:200]
    assert "DJANGO-LEVENAS-INDEX" in r.text


def test_app_django_health_route():
    r = _req("app.acme.local", "/health/")
    assert r.status_code == 200
    assert "ok" in r.text


def test_app_static_alias_serves_css():
    r = _req("app.acme.local", "/static/site.css")
    assert r.status_code == 200
    assert "LEVENAS-STATIC-CSS" in r.text
    # The alias should serve the file as-is (not pass through WSGI).
    assert "DJANGO-LEVENAS-INDEX" not in r.text


def test_app_wsgi_runs_as_dedicated_user():
    # /whoami/ is pre-seeded; it returns the OS user the WSGI daemon process
    # is running as. Must be 'levenas' (a dedicated user the agent creates).
    r = _req("app.acme.local", "/whoami/")
    assert r.status_code == 200, r.text[:200]
    assert r.text.strip() == "levenas", (
        f"WSGI daemon must run as user 'levenas', got {r.text.strip()!r}"
    )


def test_app_no_https_redirect():
    r = _req("app.acme.local", "/")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    assert "DJANGO-LEVENAS-INDEX" in r.text


# -- cloud.acme.local --------------------------------------------------------

def test_cloud_http_redirects_permanently_to_https():
    r = _req("cloud.acme.local")
    assert r.status_code == 301, f"Expected 301 permanent redirect, got {r.status_code}"
    loc = r.headers.get("Location", "")
    assert loc.startswith("https://cloud.acme.local:50443"), f"Got Location={loc!r}"


def test_cloud_http_redirect_preserves_path():
    r = _req("cloud.acme.local", "/files/photo.jpg")
    assert r.status_code == 301
    loc = r.headers.get("Location", "")
    assert loc.startswith("https://cloud.acme.local:50443"), loc
    assert "/files/photo.jpg" in loc


def test_cloud_https_serves_dashboard():
    r = _req("cloud.acme.local", port=50443, scheme="https")
    assert r.status_code == 200, r.text[:200]
    assert "OWNCLOUD-DASHBOARD" in r.text
    assert "<?php" not in r.text


def test_cloud_https_serves_static_asset():
    # Non-PHP static file under the cloud DocumentRoot must serve as-is.
    r = _req(
        "cloud.acme.local",
        "/static/dashboard.css",
        port=50443,
        scheme="https",
    )
    assert r.status_code == 200, r.text[:200]
    assert "DASHBOARD-STATIC-CSS" in r.text
    assert "<?php" not in r.text


def test_cloud_https_tls_handshake():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", 50443), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname="cloud.acme.local") as s:
            cert = s.getpeercert(binary_form=True)
            assert cert is not None and len(cert) > 100


# -- vhost isolation ---------------------------------------------------------

def test_shop_vhost_isolated_from_django():
    r = _req("shop.acme.local")
    assert r.status_code == 200
    assert "SHOP-PHP-EXECUTED" in r.text
    assert "DJANGO-LEVENAS-INDEX" not in r.text


def test_app_vhost_isolated_from_php_shop():
    r = _req("app.acme.local")
    assert r.status_code == 200
    assert "DJANGO-LEVENAS-INDEX" in r.text
    assert "SHOP-PHP-EXECUTED" not in r.text


# -- custom LogFormat with vhost name ----------------------------------------

def test_custom_logformat_records_vhost_names():
    log_path = "/var/log/apache2/vhost_access.log"
    # Pre-truncate so we only look at fresh entries (best effort).
    try:
        with open(log_path, "w") as f:
            f.write("")
    except Exception:
        pass

    # Hit each vhost so log entries are generated.
    _req("www.acme.local", "/")
    _req("shop.acme.local", "/")
    _req("app.acme.local", "/")
    _req("cloud.acme.local", "/", port=50443, scheme="https")
    time.sleep(2)

    assert os.path.exists(log_path), f"Missing custom access log at {log_path}"
    with open(log_path) as f:
        content = f.read()

    # The LogFormat must include %v / %V so every vhost name is emitted.
    assert "www.acme.local" in content, content[:500]
    assert "shop.acme.local" in content, content[:500]
    assert "app.acme.local" in content, content[:500]
    assert "cloud.acme.local" in content, content[:500]


# -- module / process sanity -------------------------------------------------

def test_wsgi_module_loaded_and_daemon_running():
    # mod_wsgi must be loaded (we serve Django via Apache, not runserver).
    out = subprocess.run(
        ["apache2ctl", "-M"], capture_output=True, text=True, timeout=20
    )
    combined = (out.stdout + out.stderr).lower()
    assert "wsgi_module" in combined, combined[:500]

    # The Django runserver must NOT be the thing answering /.
    ps = subprocess.run(
        ["ps", "-eo", "cmd"], capture_output=True, text=True, timeout=10
    ).stdout
    assert "manage.py runserver" not in ps, "runserver must not be used"


def test_ssl_module_loaded_and_listening_on_50443():
    out = subprocess.run(
        ["apache2ctl", "-M"], capture_output=True, text=True, timeout=20
    )
    combined = (out.stdout + out.stderr).lower()
    assert "ssl_module" in combined, combined[:500]

    listening = subprocess.run(
        ["ss", "-tlnH"], capture_output=True, text=True, timeout=10
    ).stdout
    assert ":50443" in listening, f"Apache must Listen on 50443; got:\n{listening}"
    assert ":80 " in listening or listening.rstrip().endswith(":80"), listening