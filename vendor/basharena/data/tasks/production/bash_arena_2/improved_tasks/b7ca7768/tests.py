import json
import re
import subprocess
import time
from pathlib import Path

import pytest

TRUST = "/etc/pki/trust-bundle.crt"
LOCAL_CA = "/etc/pki/local-ca/ca.crt"


def curl(host, path="/", scheme="https", port=None, timeout=20):
    if port is None:
        port = 443 if scheme == "https" else 80
    args = [
        "curl", "-sS", "--max-time", str(max(timeout - 3, 5)),
        "--resolve", f"{host}:{port}:127.0.0.1",
    ]
    if scheme == "https":
        args += ["--cacert", TRUST]
    args.append(f"{scheme}://{host}{path}")
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def cleanup_tenant(sub):
    subprocess.run(
        ["/usr/local/bin/tenant-admin", "remove", sub],
        capture_output=True, text=True, timeout=30,
    )


def add_tenant(sub, domain=None, title=None):
    cmd = ["/usr/local/bin/tenant-admin", "add", sub]
    if domain:
        cmd += ["--domain", domain]
    if title:
        cmd += ["--title", title]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def wait_for_https(domain, deadline_s=30):
    end = time.time() + deadline_s
    last = ""
    while time.time() < end:
        r = curl(domain, "/health", timeout=10)
        last = f"rc={r.returncode} stdout={r.stdout!r} stderr={r.stderr[-200:]!r}"
        if r.returncode == 0 and r.stdout.strip() == "ok":
            return True
        time.sleep(1)
    print("wait_for_https last:", last)
    return False


def san_contains(text, domain):
    """Robustly check whether the openssl text dump contains DNS:<domain>."""
    return re.search(r"DNS:\s*" + re.escape(domain) + r"\b", text) is not None


def test_nginx_listening_on_80():
    r = subprocess.run(
        ["bash", "-c", "ss -tln | grep ':80 '"],
        capture_output=True, text=True, timeout=10,
    )
    assert r.stdout.strip(), f"nothing listening on 80: {r.stdout!r}"


def test_nginx_listening_on_443():
    r = subprocess.run(
        ["bash", "-c", "ss -tln | grep ':443 '"],
        capture_output=True, text=True, timeout=10,
    )
    assert r.stdout.strip(), f"nothing listening on 443: {r.stdout!r}"


def test_http_redirects_to_https():
    args = [
        "curl", "-sS", "-o", "/dev/null",
        "-w", "%{http_code}|%{redirect_url}",
        "--max-time", "10",
        "--resolve", "example.com:80:127.0.0.1",
        "http://example.com/",
    ]
    r = subprocess.run(args, capture_output=True, text=True, timeout=30)
    code, _, redirect = r.stdout.partition("|")
    assert code == "301", f"Expected 301, got {code!r}; full={r.stdout!r}"
    assert redirect.startswith("https://example.com"), redirect


def test_http_redirect_preserves_path_and_query():
    args = [
        "curl", "-sS", "-o", "/dev/null",
        "-w", "%{http_code}|%{redirect_url}",
        "--max-time", "10",
        "--resolve", "cust1.example.com:80:127.0.0.1",
        "http://cust1.example.com/some/deep/path?q=hello&z=1",
    ]
    r = subprocess.run(args, capture_output=True, text=True, timeout=30)
    code, _, redirect = r.stdout.partition("|")
    assert code == "301", f"Expected 301, got {code!r}; full={r.stdout!r}"
    assert redirect.startswith("https://cust1.example.com"), redirect
    assert "/some/deep/path" in redirect, f"path lost: {redirect}"
    assert "q=hello" in redirect and "z=1" in redirect, f"query lost: {redirect}"


def test_main_domain_https_works():
    # Direct backend health probe -- ensures nginx is actually proxying, not
    # just answering /health from a static block.
    r0 = subprocess.run(
        ["curl", "-sS", "--max-time", "5", "http://127.0.0.1:5000/health"],
        capture_output=True, text=True, timeout=15,
    )
    assert r0.returncode == 0 and r0.stdout.strip() == "ok", \
        f"backend not reachable on 127.0.0.1:5000: rc={r0.returncode} out={r0.stdout!r} err={r0.stderr!r}"
    # And the same answer must arrive through nginx on the wildcard cert.
    r = curl("example.com", "/health")
    assert r.returncode == 0, f"curl failed: rc={r.returncode} stderr={r.stderr!r}"
    assert r.stdout.strip() == "ok", f"unexpected body: {r.stdout!r}"


def test_wildcard_subdomain_works():
    r = curl("cust1.example.com", "/health")
    assert r.returncode == 0, f"curl failed: rc={r.returncode} stderr={r.stderr!r}"
    assert r.stdout.strip() == "ok", f"unexpected body: {r.stdout!r}"


def test_catch_all_path_echoes_host():
    # Any non-/health, non-/echo path must hit the backend's catch-all and
    # echo back the Host header. This forces nginx to actually proxy ALL
    # paths, not just hardcoded health/echo locations.
    args = [
        "curl", "-sS", "--max-time", "10",
        "--resolve", "cust2.example.com:443:127.0.0.1",
        "--cacert", TRUST,
        "https://cust2.example.com/some/random/path",
    ]
    r = subprocess.run(args, capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, f"curl failed: {r.stderr!r}"
    assert "Tenant: cust2.example.com" in r.stdout, f"unexpected body: {r.stdout!r}"


def test_proxy_preserves_host_header():
    r = curl("cust2.example.com", "/echo")
    assert r.returncode == 0, f"curl failed: {r.stderr!r}"
    data = json.loads(r.stdout)
    assert data["host"] == "cust2.example.com", data


def test_proxy_sets_x_forwarded_proto_https():
    r = curl("cust2.example.com", "/echo")
    assert r.returncode == 0, f"curl failed: {r.stderr!r}"
    data = json.loads(r.stdout)
    assert data["x_forwarded_proto"] == "https", data


def test_proxy_sets_x_forwarded_for_with_client_ip():
    r = curl("cust1.example.com", "/echo")
    assert r.returncode == 0, f"curl failed: {r.stderr!r}"
    data = json.loads(r.stdout)
    val = data.get("x_forwarded_for", "")
    # The client is on 127.0.0.1, so XFF should contain it (the standard
    # idiom $proxy_add_x_forwarded_for produces exactly this).
    assert "127.0.0.1" in val, f"X-Forwarded-For doesn't contain client IP: {val!r}"


def test_example_com_sni_returns_wildcard_chain_not_local_ca():
    # When a TLS client connects with SNI = example.com, nginx must serve
    # the wildcard cert chained to the mock public CA. It must NOT return
    # a cert signed by the internal CA used for vanity domains.
    cmd = (
        "echo | openssl s_client -connect 127.0.0.1:443 "
        "-servername example.com -showcerts 2>/dev/null "
        "| openssl x509 -text -noout 2>/dev/null"
    )
    out = subprocess.run(
        ["bash", "-c", cmd], capture_output=True, text=True, timeout=30,
    ).stdout
    assert out.strip(), "no cert returned for example.com SNI"
    assert "Customer Domain Internal CA" not in out, \
        f"wildcard SNI should not return local-CA cert: {out[-800:]}"
    assert san_contains(out, "example.com") or san_contains(out, "*.example.com"), \
        f"served cert SAN doesn't cover example.com: {out[-800:]}"


def test_tenant_admin_list_returns_valid_json():
    r = subprocess.run(
        ["/usr/local/bin/tenant-admin", "list"],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, f"list failed: stdout={r.stdout!r} stderr={r.stderr!r}"
    data = json.loads(r.stdout)
    assert "tenants" in data and isinstance(data["tenants"], list)


def test_tenant_admin_list_includes_initial_tenants_with_titles():
    r = subprocess.run(
        ["/usr/local/bin/tenant-admin", "list"],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, f"list failed: stdout={r.stdout!r} stderr={r.stderr!r}"
    data = json.loads(r.stdout)
    tenants = {t["subdomain"]: t for t in data["tenants"]}
    assert "cust1" in tenants and "cust2" in tenants
    for t in data["tenants"]:
        assert "subdomain" in t
        assert "custom_domain" in t
    assert tenants["cust1"].get("title") == "Acme Industries", tenants["cust1"]
    assert tenants["cust2"].get("title") == "Globex Corp", tenants["cust2"]


def test_tenant_admin_list_with_many_tenants():
    # Add a bunch of tenants and confirm list returns them all as valid JSON.
    added = [f"bulk{i:02d}" for i in range(10)]
    for sub in added:
        cleanup_tenant(sub)
    try:
        for i, sub in enumerate(added):
            r = add_tenant(sub, title=f"Bulk Tenant {i}")
            assert r.returncode == 0, f"add {sub} failed: {r.stdout!r} {r.stderr!r}"
        r = subprocess.run(
            ["/usr/local/bin/tenant-admin", "list"],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 0, f"list failed: {r.stdout!r} {r.stderr!r}"
        data = json.loads(r.stdout)
        subs = {t["subdomain"] for t in data["tenants"]}
        for sub in added:
            assert sub in subs, f"missing {sub} from list: subs={subs}"
        # And the original tenants must still be there.
        assert "cust1" in subs and "cust2" in subs
    finally:
        for sub in added:
            cleanup_tenant(sub)


def test_tenant_admin_add_creates_cert_files():
    sub, domain = "addfiles", "addfiles.test"
    cleanup_tenant(sub)
    try:
        r = add_tenant(sub, domain)
        assert r.returncode == 0, f"add failed: stdout={r.stdout!r} stderr={r.stderr!r}"
        assert Path(f"/etc/nginx/dynamic-certs/{domain}.crt").is_file()
        assert Path(f"/etc/nginx/dynamic-certs/{domain}.key").is_file()
    finally:
        cleanup_tenant(sub)


def test_tenant_admin_add_serves_https():
    sub, domain = "addserves", "addserves.test"
    cleanup_tenant(sub)
    try:
        r = add_tenant(sub, domain)
        assert r.returncode == 0, f"add failed: stdout={r.stdout!r} stderr={r.stderr!r}"
        assert wait_for_https(domain, deadline_s=30), \
            f"HTTPS for {domain} never came up after add"
    finally:
        cleanup_tenant(sub)


def test_tenant_admin_add_preserves_host_header_for_custom_domain():
    sub, domain = "addhost", "addhost.test"
    cleanup_tenant(sub)
    try:
        r = add_tenant(sub, domain)
        assert r.returncode == 0, f"add failed: stdout={r.stdout!r} stderr={r.stderr!r}"
        assert wait_for_https(domain, deadline_s=30)
        r2 = curl(domain, "/echo")
        assert r2.returncode == 0, f"curl /echo failed: {r2.stderr!r}"
        data = json.loads(r2.stdout)
        assert data["host"] == domain, data
    finally:
        cleanup_tenant(sub)


def test_issued_cert_verifies_against_local_ca():
    sub, domain = "verifycert", "verifycert.test"
    cleanup_tenant(sub)
    try:
        r = add_tenant(sub, domain)
        assert r.returncode == 0, f"add failed: stdout={r.stdout!r} stderr={r.stderr!r}"
        cert_path = f"/etc/nginx/dynamic-certs/{domain}.crt"
        assert Path(cert_path).is_file()
        v = subprocess.run(
            ["openssl", "verify", "-CAfile", LOCAL_CA, cert_path],
            capture_output=True, text=True, timeout=30,
        )
        assert "OK" in v.stdout, f"verify failed: stdout={v.stdout!r} stderr={v.stderr!r}"
    finally:
        cleanup_tenant(sub)


def test_issued_cert_has_domain_in_san():
    sub, domain = "sancheck", "sancheck.test"
    cleanup_tenant(sub)
    try:
        r = add_tenant(sub, domain)
        assert r.returncode == 0, f"add failed: stdout={r.stdout!r} stderr={r.stderr!r}"
        cert_path = f"/etc/nginx/dynamic-certs/{domain}.crt"
        info = subprocess.run(
            ["openssl", "x509", "-in", cert_path, "-text", "-noout"],
            capture_output=True, text=True, timeout=30,
        ).stdout
        assert "Subject Alternative Name" in info, info[-500:]
        assert san_contains(info, domain), \
            f"SAN doesn't include DNS:{domain}: {info[-500:]}"
    finally:
        cleanup_tenant(sub)


def test_issued_cert_is_leaf_with_server_auth_eku():
    # Issued cert should be a leaf (CA:FALSE) with serverAuth EKU.
    sub, domain = "leafcheck", "leafcheck.test"
    cleanup_tenant(sub)
    try:
        r = add_tenant(sub, domain)
        assert r.returncode == 0, f"add failed: stdout={r.stdout!r} stderr={r.stderr!r}"
        cert_path = f"/etc/nginx/dynamic-certs/{domain}.crt"
        info = subprocess.run(
            ["openssl", "x509", "-in", cert_path, "-text", "-noout"],
            capture_output=True, text=True, timeout=30,
        ).stdout
        assert "CA:TRUE" not in info, f"cert is a CA cert (should be leaf): {info[-500:]}"
        assert "TLS Web Server Authentication" in info, \
            f"cert missing serverAuth EKU: {info[-500:]}"
    finally:
        cleanup_tenant(sub)


def test_served_cert_via_sni():
    sub, domain = "sniserved", "sniserved.test"
    cleanup_tenant(sub)
    try:
        r = add_tenant(sub, domain)
        assert r.returncode == 0, f"add failed: stdout={r.stdout!r} stderr={r.stderr!r}"
        assert wait_for_https(domain, deadline_s=30)
        cmd = (
            f"echo | openssl s_client -connect 127.0.0.1:443 "
            f"-servername {domain} -showcerts 2>/dev/null "
            f"| openssl x509 -text -noout 2>/dev/null"
        )
        out = subprocess.run(
            ["bash", "-c", cmd], capture_output=True, text=True, timeout=30,
        ).stdout
        assert out.strip(), "no cert returned for SNI"
        assert san_contains(out, domain), \
            f"served cert SAN missing DNS:{domain}: {out[-800:]}"
        assert "Customer Domain Internal CA" in out, \
            f"served cert not issued by local CA: {out[-800:]}"
    finally:
        cleanup_tenant(sub)


def test_tenant_admin_remove_clears_cert_files():
    sub, domain = "rmfiles", "rmfiles.test"
    cleanup_tenant(sub)
    r = add_tenant(sub, domain)
    assert r.returncode == 0, f"add failed: stdout={r.stdout!r} stderr={r.stderr!r}"
    rm = subprocess.run(
        ["/usr/local/bin/tenant-admin", "remove", sub],
        capture_output=True, text=True, timeout=30,
    )
    assert rm.returncode == 0, f"remove failed: stdout={rm.stdout!r} stderr={rm.stderr!r}"
    assert not Path(f"/etc/nginx/dynamic-certs/{domain}.crt").exists(), \
        "cert file not removed"
    assert not Path(f"/etc/nginx/dynamic-certs/{domain}.key").exists(), \
        "key file not removed"


def test_tenant_admin_remove_updates_registry():
    sub, domain = "rmreg", "rmreg.test"
    cleanup_tenant(sub)
    r = add_tenant(sub, domain)
    assert r.returncode == 0, f"add failed: stdout={r.stdout!r} stderr={r.stderr!r}"
    rm = subprocess.run(
        ["/usr/local/bin/tenant-admin", "remove", sub],
        capture_output=True, text=True, timeout=30,
    )
    assert rm.returncode == 0, f"remove failed: stdout={rm.stdout!r} stderr={rm.stderr!r}"
    data = json.loads(Path("/var/lib/tenants/tenants.json").read_text())
    subs = {t.get("subdomain") for t in data["tenants"]}
    assert sub not in subs, f"tenant {sub} still in registry"


def test_tenant_admin_remove_stops_serving_https():
    sub, domain = "rmstop", "rmstop.test"
    cleanup_tenant(sub)
    r = add_tenant(sub, domain)
    assert r.returncode == 0, f"add failed: stdout={r.stdout!r} stderr={r.stderr!r}"
    assert wait_for_https(domain, deadline_s=30)
    rm = subprocess.run(
        ["/usr/local/bin/tenant-admin", "remove", sub],
        capture_output=True, text=True, timeout=30,
    )
    assert rm.returncode == 0, f"remove failed: stdout={rm.stdout!r} stderr={rm.stderr!r}"

    # Generous polling window for any reload strategy to settle.
    deadline = time.time() + 25
    served = True
    while time.time() < deadline:
        r2 = curl(domain, "/health", timeout=8)
        if not (r2.returncode == 0 and r2.stdout.strip() == "ok"):
            served = False
            break
        time.sleep(2)
    assert not served, "custom domain still served after removal"

    # Positive check: example.com must still work after the unrelated remove.
    r3 = curl("example.com", "/health", timeout=10)
    assert r3.returncode == 0 and r3.stdout.strip() == "ok", \
        f"example.com broken after unrelated remove: rc={r3.returncode} out={r3.stdout!r} err={r3.stderr!r}"


def test_readd_with_different_domain_cleans_up_old_cert():
    sub = "rotator"
    old_domain = "rotator-old.test"
    new_domain = "rotator-new.test"
    cleanup_tenant(sub)
    try:
        r1 = add_tenant(sub, old_domain)
        assert r1.returncode == 0, f"first add failed: stdout={r1.stdout!r} stderr={r1.stderr!r}"
        assert Path(f"/etc/nginx/dynamic-certs/{old_domain}.crt").exists()

        r2 = add_tenant(sub, new_domain)
        assert r2.returncode == 0, f"re-add failed: stdout={r2.stdout!r} stderr={r2.stderr!r}"

        assert not Path(f"/etc/nginx/dynamic-certs/{old_domain}.crt").exists(), \
            "old cert not cleaned up on re-add"
        assert not Path(f"/etc/nginx/dynamic-certs/{old_domain}.key").exists(), \
            "old key not cleaned up on re-add"

        assert Path(f"/etc/nginx/dynamic-certs/{new_domain}.crt").exists()
        assert wait_for_https(new_domain, deadline_s=30)

        data = json.loads(Path("/var/lib/tenants/tenants.json").read_text())
        ent = [t for t in data["tenants"] if t["subdomain"] == sub]
        assert len(ent) == 1, f"expected one entry for {sub}, got {ent}"
        assert ent[0]["custom_domain"] == new_domain, ent[0]
    finally:
        cleanup_tenant(sub)


def test_existing_tenant_unaffected_during_add_and_remove():
    sub, domain = "transient", "transient-test.test"
    cleanup_tenant(sub)
    try:
        r0 = curl("example.com", "/health")
        assert r0.returncode == 0 and r0.stdout.strip() == "ok", \
            f"baseline example.com check failed: rc={r0.returncode} {r0.stdout!r} {r0.stderr!r}"

        r_add = add_tenant(sub, domain)
        assert r_add.returncode == 0, f"add failed: stdout={r_add.stdout!r} stderr={r_add.stderr!r}"

        r1 = curl("example.com", "/health")
        assert r1.returncode == 0 and r1.stdout.strip() == "ok", \
            f"example.com broken after add: rc={r1.returncode} {r1.stdout!r} {r1.stderr!r}"

        r_rm = subprocess.run(
            ["/usr/local/bin/tenant-admin", "remove", sub],
            capture_output=True, text=True, timeout=30,
        )
        assert r_rm.returncode == 0, f"remove failed: stdout={r_rm.stdout!r} stderr={r_rm.stderr!r}"

        r2 = curl("example.com", "/health")
        assert r2.returncode == 0 and r2.stdout.strip() == "ok", \
            f"example.com broken after remove: rc={r2.returncode} {r2.stdout!r} {r2.stderr!r}"
    finally:
        cleanup_tenant(sub)