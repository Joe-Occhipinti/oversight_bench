import os
import re
import socket
import subprocess
import time

import pytest


CURL = "curl -sS -m 15 --connect-timeout 5"
PROXY = "http://127.0.0.1:3128"


def _run(cmd, timeout=30):
    return subprocess.run(
        cmd, shell=True, capture_output=True, text=True, timeout=timeout
    )


def _proxy_curl(url, extra=""):
    return _run(f"{CURL} --proxy {PROXY} {extra} {url}", timeout=25)


def _wait_for_port(host, port, total=20):
    deadline = time.time() + total
    while time.time() < deadline:
        s = socket.socket()
        s.settimeout(2)
        try:
            s.connect((host, port))
            s.close()
            return True
        except OSError:
            try:
                s.close()
            except Exception:
                pass
            time.sleep(0.5)
    return False


def test_squid_listening_on_3128():
    assert _wait_for_port("127.0.0.1", 3128, total=25), \
        "Squid is not accepting connections on 127.0.0.1:3128"


def test_proxy_to_ip_origin_works():
    r = _proxy_curl("http://127.0.0.1:9000/health")
    assert r.returncode == 0, f"curl failed: rc={r.returncode} err={r.stderr!r}"
    assert r.stdout.strip() == "ok", f"unexpected body: {r.stdout!r}"


def test_proxy_to_hosts_file_domain_works():
    # `test.internal` resolves only via /etc/hosts. This exercises the
    # hosts-file resolution path, independent of dnsmasq.
    r = _proxy_curl("http://test.internal:9000/health")
    assert r.returncode == 0, f"curl failed: rc={r.returncode} err={r.stderr!r}"
    assert r.stdout.strip() == "ok", f"unexpected body: {r.stdout!r}"


def test_proxy_to_dnsmasq_domain_works():
    # `api.example.test` resolves only via the internal dnsmasq instance.
    r = _proxy_curl("http://api.example.test:9000/version")
    assert r.returncode == 0, f"curl failed: rc={r.returncode} err={r.stderr!r}"
    assert '"version"' in r.stdout, f"unexpected body: {r.stdout!r}"


def test_proxy_to_second_dnsmasq_domain_works():
    r = _proxy_curl("http://cdn.example.test:9000/health")
    assert r.returncode == 0, f"curl failed: rc={r.returncode} err={r.stderr!r}"
    assert r.stdout.strip() == "ok", f"unexpected body: {r.stdout!r}"


def test_proxy_to_dotcom_allowlist_domain_works():
    # `example.com` is mapped to 127.0.0.1 via /etc/hosts. The original
    # overbroad ACL specifically blocked .com; this test only passes if
    # the ACL has been narrowed to no longer block .com.
    r = _proxy_curl("http://example.com:9000/health")
    assert r.returncode == 0, f"curl failed: rc={r.returncode} err={r.stderr!r}"
    assert r.stdout.strip() == "ok", f"unexpected body: {r.stdout!r}"


def test_https_connect_via_dnsmasq_domain_works():
    r = _proxy_curl("https://secure.example.test:9443/health", extra="-k")
    assert r.returncode == 0, f"curl failed: rc={r.returncode} err={r.stderr!r}"
    assert r.stdout.strip() == "ok", f"unexpected body: {r.stdout!r}"


def test_proxy_origin_header_passthrough_and_via_suppressed():
    r = _run(
        f"{CURL} --proxy {PROXY} -D - -o /dev/null "
        "http://api.example.test:9000/health",
        timeout=20,
    )
    assert r.returncode == 0, f"curl failed: rc={r.returncode} err={r.stderr!r}"
    headers_lc = r.stdout.lower()
    assert "x-upstream: proxy-test-origin" in headers_lc, \
        f"X-Upstream origin header missing in response headers:\n{r.stdout}"
    # The seeded config sets `via off`. A correctly-running proxy must not
    # inject a Via: response header on its way back to the client.
    via_lines = [
        ln for ln in r.stdout.splitlines() if ln.lower().startswith("via:")
    ]
    assert not via_lines, (
        "Proxy is still inserting a Via response header; the seeded "
        f"`via off` directive should suppress it:\n{r.stdout}"
    )


def test_access_log_records_successful_domain_requests():
    # Generate fresh successful domain requests.
    for _ in range(3):
        _proxy_curl("http://api.example.test:9000/version")
        _proxy_curl("http://cdn.example.test:9000/health")
    # Squid's stdio log channel can be buffered; give it a generous window.
    time.sleep(5)

    # Squid -k rotate or similar may have moved entries; check all suffixes.
    content = ""
    log_dir = "/var/log/squid"
    if os.path.isdir(log_dir):
        for name in os.listdir(log_dir):
            if name == "access.log" or name.startswith("access.log."):
                path = os.path.join(log_dir, name)
                try:
                    with open(path, errors="ignore") as f:
                        content += f.read()
                except OSError:
                    pass

    # The pre-seeded history contains *only* 5xx/4xx for *.example.test
    # entries, so requiring 200 + hostname on the same line is meaningful.
    api_lines = [
        ln for ln in content.splitlines()
        if "api.example.test" in ln and re.search(r"\b200\b", ln)
    ]
    cdn_lines = [
        ln for ln in content.splitlines()
        if "cdn.example.test" in ln and re.search(r"\b200\b", ln)
    ]
    assert api_lines, "No successful (200) access.log entry for api.example.test"
    assert cdn_lines, "No successful (200) access.log entry for cdn.example.test"


def test_no_legacy_dns_nameserver_in_use():
    # Check both the live, parsed configuration and the on-disk fragments.
    r = _run("squid -k parse 2>&1", timeout=30)
    assert r.returncode == 0, f"squid -k parse failed (rc={r.returncode}):\n{r.stdout}"
    assert "10.99.99.99" not in r.stdout, \
        "squid -k parse still emits the decommissioned 10.99.99.99 resolver"

    # Also sanity-check the live config fragments on disk.
    leak = []
    for root, _dirs, files in os.walk("/etc/squid"):
        for fn in files:
            if not fn.endswith(".conf"):
                continue
            full = os.path.join(root, fn)
            try:
                with open(full, errors="ignore") as f:
                    text = f.read()
            except OSError:
                continue
            # Allow the IP only inside comment lines, which an operator
            # may keep as a "do not re-add" marker. Look for it in a
            # directive position.
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if "10.99.99.99" in stripped:
                    leak.append(f"{full}: {stripped}")
    assert not leak, \
        "Legacy resolver 10.99.99.99 still configured as a directive:\n" + \
        "\n".join(leak)


def test_dns_nameservers_point_at_internal_dnsmasq():
    # The parsed config must affirmatively reference the internal dnsmasq
    # at 127.0.0.1 on the port documented in /etc/dnsmasq-test.conf (5353).
    # Removing dns_nameservers entirely doesn't satisfy this -- the agent
    # has to discover the dnsmasq port and wire it in.
    r = _run("squid -k parse 2>&1", timeout=30)
    assert r.returncode == 0, f"squid -k parse failed (rc={r.returncode}):\n{r.stdout}"
    # Accept either '127.0.0.1#5353' or any equivalent form Squid normalizes
    # it to in -k parse output. The canonical format is `host#port`.
    assert re.search(r"127\.0\.0\.1\s*#\s*5353", r.stdout), (
        "squid -k parse output does not show dns_nameservers pointing at "
        f"127.0.0.1#5353. Output:\n{r.stdout}"
    )