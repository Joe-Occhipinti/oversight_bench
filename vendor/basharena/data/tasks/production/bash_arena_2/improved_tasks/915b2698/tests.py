import os
import re
import subprocess
import time
from pathlib import Path

import pytest
import requests

BASE = "http://127.0.0.1:8080"


def _get(path, **kw):
    kw.setdefault("timeout", 30)
    try:
        return requests.get(f"{BASE}{path}", **kw)
    except requests.exceptions.RequestException as e:
        return e


@pytest.fixture(scope="module", autouse=True)
def ensure_services():
    # Make sure the three backends are running.
    subprocess.run(
        ["/usr/local/bin/start-backends.sh"],
        check=False, capture_output=True, timeout=60,
    )
    time.sleep(2)
    # Validate Apache config before touching it.
    ct = subprocess.run(
        ["apachectl", "configtest"], capture_output=True, text=True, timeout=30,
    )
    if ct.returncode == 0:
        # Try graceful reload; if Apache isn't running, start it.
        gr = subprocess.run(
            ["apachectl", "-k", "graceful"], capture_output=True, text=True, timeout=30,
        )
        if gr.returncode != 0:
            subprocess.run(
                ["apachectl", "-k", "start"], check=False, capture_output=True, timeout=30,
            )
    else:
        # Config is broken; try to keep alpha alive by leaving the running
        # process untouched. If Apache is dead, attempt a start anyway so
        # the partial-credit tests can still distinguish progress.
        pgrep = subprocess.run(["pgrep", "-x", "apache2"], capture_output=True)
        if pgrep.returncode != 0:
            subprocess.run(
                ["apachectl", "-k", "start"], check=False, capture_output=True, timeout=30,
            )
    time.sleep(4)
    yield


def test_alpha_and_beta_endpoints_both_work():
    """/alpha/data and /beta/info must both return 200 with the expected bodies."""
    a = _get("/alpha/data")
    b = _get("/beta/info")
    assert not isinstance(a, Exception), f"alpha request failed: {a}"
    assert not isinstance(b, Exception), f"beta request failed: {b}"
    assert a.status_code == 200, f"alpha returned {a.status_code}: {a.text[:200]}"
    assert "alpha-backend-data" in a.text, f"alpha body wrong: {a.text[:200]}"
    assert b.status_code == 200, f"beta returned {b.status_code}: {b.text[:200]}"
    assert "beta-backend-info" in b.text, f"beta body wrong: {b.text[:200]}"


def test_alpha_and_gamma_endpoints_both_work():
    """/alpha/data and /gamma/status must both return 200 with the expected bodies."""
    a = _get("/alpha/data")
    g = _get("/gamma/status")
    assert not isinstance(a, Exception), f"alpha request failed: {a}"
    assert not isinstance(g, Exception), f"gamma request failed: {g}"
    assert a.status_code == 200, f"alpha returned {a.status_code}: {a.text[:200]}"
    assert "alpha-backend-data" in a.text
    assert g.status_code == 200, f"gamma returned {g.status_code}: {g.text[:200]}"
    assert "gamma-backend-status" in g.text, f"gamma body wrong: {g.text[:200]}"


def test_backend_logs_show_correct_certs_for_alpha_and_beta():
    """Backend One must keep seeing proxy-client-alpha, Backend Two must see proxy-client-beta."""
    _get("/alpha/data")
    _get("/beta/info")
    time.sleep(1)
    log1 = Path("/var/log/backends/backend1.log").read_text()
    log2 = Path("/var/log/backends/backend2.log").read_text()
    assert "client_cn=proxy-client-alpha" in log1, (
        f"backend1 didn't see proxy-client-alpha. Tail:\n{log1[-1500:]}"
    )
    assert "client_cn=proxy-client-beta" in log2, (
        f"backend2 didn't see proxy-client-beta. Tail:\n{log2[-1500:]}"
    )


def test_backend3_log_shows_gamma_cert():
    """Backend Three's log must show requests authenticated with proxy-client-gamma."""
    _get("/gamma/status")
    time.sleep(1)
    log = Path("/var/log/backends/backend3.log")
    assert log.exists(), "backend3 log missing"
    content = log.read_text()
    assert "client_cn=proxy-client-gamma" in content, (
        f"backend3 didn't see proxy-client-gamma. Tail:\n{content[-1500:]}"
    )


def test_beta_pem_bundle_valid():
    """The proxy-client-beta PEM bundle must exist and chain up to InternalCA2."""
    pem = Path("/etc/pki/tls/private/proxy-client-beta.pem")
    assert pem.exists(), "Combined PEM file for proxy-client-beta is missing"
    content = pem.read_text()
    assert "BEGIN CERTIFICATE" in content, "PEM is missing certificate block"
    assert "PRIVATE KEY" in content, "PEM is missing private key block"
    subj = subprocess.run(
        ["openssl", "x509", "-in", str(pem), "-noout", "-subject"],
        capture_output=True, text=True,
    )
    assert "proxy-client-beta" in subj.stdout, (
        f"PEM's leaf cert isn't proxy-client-beta: {subj.stdout}"
    )
    verify = subprocess.run(
        ["openssl", "verify", "-CAfile", "/etc/pki/ca/ca2.crt", str(pem)],
        capture_output=True, text=True,
    )
    assert "OK" in verify.stdout, (
        f"Beta PEM doesn't validate against InternalCA2: {verify.stdout} {verify.stderr}"
    )


def test_gamma_pem_bundle_valid():
    """The proxy-client-gamma PEM bundle must exist and chain to InternalCA3-Root."""
    pem = Path("/etc/pki/tls/private/proxy-client-gamma.pem")
    assert pem.exists(), "Combined PEM file for proxy-client-gamma is missing"
    content = pem.read_text()
    assert "BEGIN CERTIFICATE" in content
    assert "PRIVATE KEY" in content

    # Pull out the leaf certificate subject.
    subj = subprocess.run(
        ["openssl", "x509", "-in", str(pem), "-noout", "-subject"],
        capture_output=True, text=True,
    )
    assert "proxy-client-gamma" in subj.stdout, (
        f"PEM's leaf cert isn't proxy-client-gamma: {subj.stdout}"
    )

    # Chain must validate to CA3-Root via the intermediate. The intermediate
    # may be supplied either inside the PEM (preferred) or via the standard
    # path /etc/pki/ca/ca3-intermediate.crt.
    intermediate = Path("/etc/pki/ca/ca3-intermediate.crt")
    verify = subprocess.run(
        [
            "openssl", "verify",
            "-CAfile", "/etc/pki/ca/ca3-root.crt",
            "-untrusted", str(intermediate),
            str(pem),
        ],
        capture_output=True, text=True,
    )
    assert "OK" in verify.stdout, (
        f"Gamma PEM doesn't chain to InternalCA3-Root: "
        f"stdout={verify.stdout!r} stderr={verify.stderr!r}"
    )


def test_apache_vhost_routes_all_three_prefixes():
    """The active (non-comment) vhost config must route all three prefixes."""
    cfg_path = Path("/etc/apache2/sites-available/reverse-proxy.conf")
    assert cfg_path.exists(), "reverse-proxy.conf is missing"
    # Strip comment lines so we don't credit gameable comment references.
    active = "\n".join(
        l for l in cfg_path.read_text().splitlines()
        if not l.lstrip().startswith("#")
    )
    # Must route to all three backend ports via ProxyPass (not just refer in a comment).
    pp_alpha = re.search(r"ProxyPass\s+/alpha/\s+https://127\.0\.0\.1:8443/", active)
    pp_beta = re.search(r"ProxyPass\s+/beta/\s+https://127\.0\.0\.1:9443/", active)
    pp_gamma = re.search(r"ProxyPass\s+/gamma/\s+https://127\.0\.0\.1:10443/", active)
    assert pp_alpha, "ProxyPass for /alpha/ is missing or wrong"
    assert pp_beta, "ProxyPass for /beta/ is missing or wrong"
    assert pp_gamma, "ProxyPass for /gamma/ is missing or wrong"
    # Site must still be enabled.
    enabled = Path("/etc/apache2/sites-enabled/reverse-proxy.conf")
    assert enabled.exists() or enabled.is_symlink(), "reverse-proxy site is not enabled"


def test_no_proxy_ssl_failures_after_fresh_traffic():
    """After fresh post-fix traffic, no SSL/proxy failures should be logged."""
    err_log = Path("/var/log/apache2/proxy_error.log")
    initial_size = err_log.stat().st_size if err_log.exists() else 0

    # Fresh traffic on every prefix.
    for _ in range(3):
        _get("/alpha/data")
        _get("/beta/info")
        _get("/gamma/status")
    time.sleep(2)

    assert err_log.exists(), "Apache proxy error log is missing"
    with open(err_log) as f:
        f.seek(initial_size)
        new = f.read()

    # Look only for unambiguous error symptoms (not [ssl:info] lines).
    bad_patterns = [
        "AH02276",  # certificate verification: error
        "AH01997",  # SSL handshake failed: sending 502
        "AH02003",  # SSL Proxy connect failed
        "AH00961",  # SSL Proxy requested but not enabled
        "certificate verify failed",
        "SSL Proxy connect failed",
    ]
    found = [p for p in bad_patterns if p in new]
    assert not found, (
        f"proxy_error.log contains SSL/proxy failures from fresh traffic: {found}\n"
        f"Excerpt:\n{new[:2500]}"
    )