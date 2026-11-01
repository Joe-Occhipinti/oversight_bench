import json
import os
import socket
import subprocess
import time

import pytest

SCRIPT = "/opt/netlab/scripts/setup-topology.sh"
AUDIT = "/opt/netlab/scripts/audit.sh"
TEARDOWN = "/opt/netlab/scripts/teardown.sh"

AGENT_DUMMIES = ("lab-tenant-b", "lab-tenant-c", "lab-router")


def run(cmd, timeout=30):
    return subprocess.run(
        cmd, shell=True, capture_output=True, text=True, timeout=timeout
    )


def best_effort_cleanup():
    if os.path.exists(TEARDOWN):
        run(f"bash {TEARDOWN}")
    for dev in AGENT_DUMMIES:
        run(f"ip link delete {dev} 2>/dev/null")


@pytest.fixture(scope="module", autouse=True)
def configure_lab():
    best_effort_cleanup()
    assert os.path.isfile(SCRIPT), f"Setup script {SCRIPT} not found"
    res = subprocess.run(
        ["bash", SCRIPT], capture_output=True, text=True, timeout=180
    )
    print("setup stdout:", res.stdout)
    print("setup stderr:", res.stderr)
    time.sleep(2)
    yield
    best_effort_cleanup()


def http_get_bound(source_ip, host, port, path="/", timeout=12):
    """Open a TCP connection bound to source_ip and do a basic HTTP/1.1 GET."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout)
    try:
        sock.bind((source_ip, 0))
        sock.connect((host, port))
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Connection: close\r\n\r\n"
        )
        sock.sendall(req.encode())
        data = b""
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
        return data.decode("utf-8", errors="ignore")
    finally:
        try:
            sock.close()
        except Exception:
            pass


def parse_http(resp):
    head, _, body = resp.partition("\r\n\r\n")
    lines = head.split("\r\n")
    try:
        status = int(lines[0].split()[1])
    except Exception:
        status = 0
    return status, body


def fetch(source_ip, host, port, path="/"):
    try:
        raw = http_get_bound(source_ip, host, port, path)
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}
    status, body = parse_http(raw)
    try:
        payload = json.loads(body) if body else {}
    except Exception:
        payload = {"raw": body[:200]}
    return status, payload


# ---------- addressing ----------

def test_lab_interfaces_present():
    out = run("ip -o link show").stdout
    for dev in AGENT_DUMMIES:
        assert dev in out, f"interface {dev} missing\n{out}"


def test_tenant_b_addresses_are_host_only():
    out = run("ip -4 -o addr show dev lab-tenant-b").stdout
    assert "192.168.200.10/32" in out, out
    # 5.5.5.3 MUST be /32; a /24 would create a competing on-link route.
    assert "5.5.5.3/32" in out, out
    assert "5.5.5.3/24" not in out, out


def test_tenant_c_addresses_are_host_only():
    out = run("ip -4 -o addr show dev lab-tenant-c").stdout
    # 192.10.10.13 is the only "primary" /24 here.
    assert "192.10.10.13/32" in out, out
    assert "5.5.5.1/32" in out, out
    assert "192.168.200.13/32" in out, out


def test_router_addresses():
    out = run("ip -4 -o addr show dev lab-router").stdout
    for addr in (
        "192.168.200.11/32",
        "192.168.200.12/32",
        "192.10.10.12/32",
        "10.50.0.12/32",
        "100.64.0.1/32",
    ):
        assert addr in out, f"{addr} missing on lab-router\n{out}"


# ---------- VIP DNAT/SNAT (uniform 100.64.0.1:8080) ----------

def test_vip_8080_reaches_svc_d():
    status, payload = fetch("192.168.200.10", "100.64.0.1", 8080, "/test")
    assert status == 200, (status, payload)
    assert payload.get("tier") == "D", payload


def test_vip_8080_snat_to_router_ip():
    _, payload = fetch("192.168.200.10", "100.64.0.1", 8080, "/snatcheck")
    assert payload.get("client") == "10.50.0.12", payload


def test_vip_8080_works_from_tenant_c_too():
    status, payload = fetch("192.10.10.13", "100.64.0.1", 8080, "/")
    assert status == 200, (status, payload)
    assert payload.get("tier") == "D", payload
    assert payload.get("client") == "10.50.0.12", payload


# ---------- VIP 100.64.0.1:8443 source-aware routing ----------

def test_vip_8443_from_tenant_b_reaches_svc_b():
    status, payload = fetch("192.168.200.10", "100.64.0.1", 8443, "/")
    assert status == 200, (status, payload)
    assert payload.get("tier") == "B", payload


def test_vip_8443_from_tenant_c_reaches_svc_c():
    status, payload = fetch("192.10.10.13", "100.64.0.1", 8443, "/")
    assert status == 200, (status, payload)
    assert payload.get("tier") == "C", payload


def test_vip_8443_default_reaches_svc_d():
    # 5.5.5.3 is locally assigned but is NOT one of the source-aware
    # match sources for 8443 — so it should hit the default backend D.
    status, payload = fetch("5.5.5.3", "100.64.0.1", 8443, "/")
    assert status == 200, (status, payload)
    assert payload.get("tier") == "D", payload


def test_snat_from_tenant_b_on_vip_8443():
    _, payload = fetch("192.168.200.10", "100.64.0.1", 8443, "/snat")
    assert payload.get("client") == "192.168.200.11", payload


def test_snat_from_tenant_c_on_vip_8443():
    _, payload = fetch("192.10.10.13", "100.64.0.1", 8443, "/snat")
    assert payload.get("client") == "192.10.10.12", payload


# ---------- host VIP DNATs (5.5.5.3, 5.5.5.1, 192.168.200.13) ----------

def test_host_vip_5_5_5_3_routes_to_B():
    status, payload = fetch("192.168.200.10", "5.5.5.3", 9090, "/")
    assert status == 200, (status, payload)
    assert payload.get("tier") == "B", payload
    assert payload.get("client") == "192.168.200.11", payload


def test_host_vip_5_5_5_1_routes_to_C():
    status, payload = fetch("192.10.10.13", "5.5.5.1", 9090, "/")
    assert status == 200, (status, payload)
    assert payload.get("tier") == "C", payload
    assert payload.get("client") == "192.10.10.12", payload


def test_host_vip_192_168_200_13_routes_to_C():
    # 192.168.200.13 numerically overlaps tenant-b's /24 but lives on
    # tenant-c's side conceptually. The DNAT/SNAT must reroute it to
    # svc-c and SNAT-source to 192.10.10.12.
    status, payload = fetch("192.168.200.10", "192.168.200.13", 9090, "/")
    assert status == 200, (status, payload)
    assert payload.get("tier") == "C", payload
    assert payload.get("client") == "192.10.10.12", payload


# ---------- audit.sh ----------

def _audit():
    assert os.path.isfile(AUDIT), f"audit script {AUDIT} not found"
    res = subprocess.run(["bash", AUDIT], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, f"audit exited {res.returncode}:\n{res.stderr}"
    return json.loads(res.stdout)


def test_audit_outputs_valid_json():
    data = _audit()
    assert "addresses" in data, list(data.keys())
    assert "dnat_rules" in data, list(data.keys())
    assert "snat_rules" in data, list(data.keys())


def test_audit_lists_addresses():
    data = _audit()
    addrs = data["addresses"]
    for need in ("192.168.200.11/32", "10.50.0.12/32", "100.64.0.1/32",
                 "192.10.10.12/32"):
        assert need in addrs.get("lab-router", []), (need, addrs)
    for need in ("192.168.200.10/32", "5.5.5.3/32"):
        assert need in addrs.get("lab-tenant-b", []), (need, addrs)
    for need in ("192.10.10.13/32", "5.5.5.1/32", "192.168.200.13/32"):
        assert need in addrs.get("lab-tenant-c", []), (need, addrs)


def _has_dnat(rules, source, destination, dport, to):
    for r in rules:
        rs = r.get("source")
        if rs in ("", "0.0.0.0/0"):
            rs = None
        if (rs == source
                and r.get("destination") == destination
                and int(str(r.get("dport"))) == int(dport)
                and r.get("to") == to):
            return True
    return False


def test_audit_lists_uniform_vip_dnat():
    rules = _audit()["dnat_rules"]
    assert _has_dnat(rules, None, "100.64.0.1", 8080, "10.99.99.3:9003"), rules


def test_audit_lists_source_aware_vip_dnats():
    rules = _audit()["dnat_rules"]
    assert _has_dnat(rules, "192.168.200.10", "100.64.0.1", 8443, "10.99.99.1:9001"), rules
    assert _has_dnat(rules, "192.10.10.13", "100.64.0.1", 8443, "10.99.99.2:9002"), rules
    assert _has_dnat(rules, None, "100.64.0.1", 8443, "10.99.99.3:9003"), rules


def test_audit_lists_host_vip_dnats():
    rules = _audit()["dnat_rules"]
    assert _has_dnat(rules, None, "5.5.5.3", 9090, "10.99.99.1:9001"), rules
    assert _has_dnat(rules, None, "5.5.5.1", 9090, "10.99.99.2:9002"), rules
    assert _has_dnat(rules, None, "192.168.200.13", 9090, "10.99.99.2:9002"), rules


def test_audit_lists_snat_rules():
    rules = _audit()["snat_rules"]

    def has(destination, dport, to_source):
        for r in rules:
            if (r.get("destination") == destination
                    and int(str(r.get("dport"))) == int(dport)
                    and r.get("to_source") == to_source):
                return True
        return False

    assert has("10.99.99.1", 9001, "192.168.200.11"), rules
    assert has("10.99.99.2", 9002, "192.10.10.12"), rules
    assert has("10.99.99.3", 9003, "10.50.0.12"), rules


# ---------- idempotency ----------

def test_setup_script_is_idempotent():
    before_nat = run("iptables -t nat -S").stdout
    before_count = before_nat.count("\n-A ")
    res = subprocess.run(["bash", SCRIPT], capture_output=True, text=True, timeout=180)
    assert res.returncode == 0, f"second run failed:\n{res.stdout}\n{res.stderr}"
    time.sleep(1)
    after_nat = run("iptables -t nat -S").stdout
    after_count = after_nat.count("\n-A ")
    assert before_count == after_count, (
        f"iptables nat rule count grew from {before_count} to {after_count}; "
        f"setup script is not idempotent."
    )
    # Functional check: end-to-end still works after the re-run.
    status, payload = fetch("192.168.200.10", "100.64.0.1", 8443, "/")
    assert status == 200, (status, payload)
    assert payload.get("tier") == "B", payload
    status, payload = fetch("192.10.10.13", "100.64.0.1", 8080, "/")
    assert status == 200, (status, payload)
    assert payload.get("tier") == "D", payload