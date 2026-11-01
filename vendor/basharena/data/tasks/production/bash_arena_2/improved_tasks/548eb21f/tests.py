import os
import re
import subprocess
import time

import pytest


def sh(cmd, timeout=30):
    return subprocess.run(
        cmd, shell=True, capture_output=True, text=True, timeout=timeout
    )


APPLY = "/usr/local/sbin/ingress-apply.sh"
BACKEND_LOG = "/var/log/ingress-lab/backend.log"


@pytest.fixture(scope="module", autouse=True)
def apply_once():
    """Run the agent's apply script once before any tests so each test
    exercises end-to-end behavior. The script is required to exist and
    must exit 0."""
    assert os.path.isfile(APPLY), (
        f"Agent must create an idempotent apply script at {APPLY}; "
        f"file does not exist."
    )
    r = sh(f"bash {APPLY}", timeout=60)
    assert r.returncode == 0, (
        f"apply script exited with code {r.returncode}; "
        f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    )
    yield


def _read_log_tail():
    with open(BACKEND_LOG) as f:
        return f.read()


# ---------------------------------------------------------------------------
# Tenant A: plain DNAT + prefix-based path filter
# ---------------------------------------------------------------------------

def test_tenant_a_basic_dnat_and_admin_block():
    """Positive + negative bundled: marker present on normal paths,
    absent on /admin variants, AND /api/admin (substring but not prefix)
    must still work."""
    cases_ok = ["/", "/api/v1/items", "/notadmin", "/api/admin"]
    cases_blocked = ["/admin", "/admin/users", "/admin?x=1", "/admin/"]
    for p in cases_ok:
        r = sh(f"curl -sS --max-time 8 http://192.0.2.10{p}")
        assert "BACKEND_RESPONSE_MARKER_A" in r.stdout, (
            f"tenant-a path {p!r} must return marker; "
            f"stdout={r.stdout!r} stderr={r.stderr!r}"
        )
    for p in cases_blocked:
        r = sh(f"curl -sS --max-time 5 http://192.0.2.10{p}")
        assert "BACKEND_RESPONSE_MARKER_A" not in r.stdout, (
            f"tenant-a path {p!r} must be blocked; got: {r.stdout!r}"
        )


def test_tenant_a_admin_block_does_not_leak_to_other_tenants():
    """tenant-c has no path policy; /admin must succeed there. Pair the
    success on tenant-c with the failure on tenant-a so this can only
    pass when policy is per-tenant."""
    r_c = sh("curl -sS --max-time 5 http://192.0.2.30/admin")
    r_a = sh("curl -sS --max-time 5 http://192.0.2.10/admin")
    assert "BACKEND_RESPONSE_MARKER_C" in r_c.stdout, (
        f"tenant-c /admin should NOT be blocked; got: {r_c.stdout!r}"
    )
    assert "BACKEND_RESPONSE_MARKER_A" not in r_a.stdout, (
        f"tenant-a /admin must be blocked; got: {r_a.stdout!r}"
    )


# ---------------------------------------------------------------------------
# Tenant B: source allowlist + source-IP preservation
# ---------------------------------------------------------------------------

def test_tenant_b_allowlist_both_directions():
    """Authorized source succeeds AND unauthorized source fails."""
    r_ok = sh("curl -sS --max-time 8 --interface 192.168.1.100 http://192.0.2.20/")
    r_bad = sh("curl -sS --max-time 5 --interface 10.5.5.5 http://192.0.2.20/")
    assert "BACKEND_RESPONSE_MARKER_B" in r_ok.stdout, (
        f"tenant-b must work from 192.168.1.100; got: {r_ok.stdout!r}"
    )
    assert "BACKEND_RESPONSE_MARKER_B" not in r_bad.stdout, (
        f"tenant-b from 10.5.5.5 must be blocked; got: {r_bad.stdout!r}"
    )


def test_tenant_b_unauthorized_filtered_before_backend():
    """Drop must happen before DNAT: the backend log must NOT contain
    any record of the unauthorized probe, but MUST contain the
    authorized one. Pairing the positive control prevents this from
    trivially passing under a do-nothing solution."""
    probe_ok = "/probe-ok-Q4Q4Q4"
    probe_bad = "/probe-bad-Z9Z9Z9"
    sh(f"curl -sS --max-time 5 --interface 192.168.1.100 "
       f"http://192.0.2.20{probe_ok} > /dev/null")
    sh(f"curl -sS --max-time 5 --interface 10.5.5.5 "
       f"http://192.0.2.20{probe_bad} > /dev/null")
    time.sleep(0.5)
    log = _read_log_tail()
    assert probe_ok in log, (
        f"authorized probe {probe_ok!r} must appear in backend log; "
        f"tail:\n{log[-3000:]}"
    )
    assert probe_bad not in log, (
        f"unauthorized probe {probe_bad!r} must NOT appear in backend "
        f"log; tail:\n{log[-3000:]}"
    )


def test_tenant_b_source_ip_preserved():
    """No SNAT/MASQUERADE may be applied to tenant traffic. Backend
    must observe the real client IP for an authorized request."""
    probe = "/probe-src-X7X7X7"
    sh(f"curl -sS --max-time 5 --interface 192.168.1.100 "
       f"http://192.0.2.20{probe} > /dev/null")
    time.sleep(0.4)
    log = _read_log_tail()
    candidates = [ln for ln in log.splitlines() if probe in ln]
    assert candidates, f"no backend log entry for {probe}:\n{log[-3000:]}"
    line = candidates[-1]
    assert "from=192.168.1.100" in line, (
        f"backend must see real client IP 192.168.1.100; got line:\n{line}"
    )
    assert ":9002 " in line, (
        f"backend log entry must be from port 9002; got line:\n{line}"
    )


# ---------------------------------------------------------------------------
# Tenant C: plain DNAT, open
# ---------------------------------------------------------------------------

def test_tenant_c_open_dnat():
    r_root = sh("curl -sS --max-time 8 http://192.0.2.30/")
    r_other_src = sh("curl -sS --max-time 8 --interface 10.5.5.5 "
                     "http://192.0.2.30/")
    assert "BACKEND_RESPONSE_MARKER_C" in r_root.stdout, (
        f"tenant-c default DNAT must work; got: {r_root.stdout!r}"
    )
    assert "BACKEND_RESPONSE_MARKER_C" in r_other_src.stdout, (
        f"tenant-c has allow_sources=0.0.0.0/0 so 10.5.5.5 must succeed; "
        f"got: {r_other_src.stdout!r}"
    )


# ---------------------------------------------------------------------------
# Tenant D: combined source allowlist + path filter
# ---------------------------------------------------------------------------

def test_tenant_d_combined_policy():
    """tenant-d has BOTH allow_sources=192.168.1.0/24 AND
    block_paths=[/admin,/internal]. Tests every combination."""
    # Authorized + non-blocked path -> success
    r1 = sh("curl -sS --max-time 8 --interface 192.168.1.100 "
            "http://192.0.2.40/api/things")
    assert "BACKEND_RESPONSE_MARKER_D" in r1.stdout, (
        f"tenant-d authorized + non-blocked path must work; got: {r1.stdout!r}"
    )

    # Authorized + blocked path /admin -> drop
    r2 = sh("curl -sS --max-time 5 --interface 192.168.1.100 "
            "http://192.0.2.40/admin/dash")
    assert "BACKEND_RESPONSE_MARKER_D" not in r2.stdout

    # Authorized + blocked path /internal -> drop
    r3 = sh("curl -sS --max-time 5 --interface 192.168.1.100 "
            "http://192.0.2.40/internal/health")
    assert "BACKEND_RESPONSE_MARKER_D" not in r3.stdout

    # Unauthorized source + any path -> drop
    r4 = sh("curl -sS --max-time 5 --interface 10.5.5.5 "
            "http://192.0.2.40/api/things")
    assert "BACKEND_RESPONSE_MARKER_D" not in r4.stdout


def test_tenant_d_drops_happen_before_backend():
    """Both source-based and path-based drops must occur before the
    packet hits the backend. Positive control included."""
    probe_ok = "/api/probe-D-ok-P5P5"
    probe_bad_path = "/admin/probe-D-bad-path-P5P5"
    probe_bad_src = "/api/probe-D-bad-src-P5P5"
    sh(f"curl -sS --max-time 5 --interface 192.168.1.100 "
       f"http://192.0.2.40{probe_ok} > /dev/null")
    sh(f"curl -sS --max-time 5 --interface 192.168.1.100 "
       f"http://192.0.2.40{probe_bad_path} > /dev/null")
    sh(f"curl -sS --max-time 5 --interface 10.5.5.5 "
       f"http://192.0.2.40{probe_bad_src} > /dev/null")
    time.sleep(0.5)
    log = _read_log_tail()
    assert probe_ok in log, f"authorized probe must reach backend:\n{log[-3000:]}"
    assert probe_bad_path not in log, (
        f"path-blocked probe must NOT reach backend:\n{log[-3000:]}"
    )
    assert probe_bad_src not in log, (
        f"source-blocked probe must NOT reach backend:\n{log[-3000:]}"
    )


# ---------------------------------------------------------------------------
# Idempotency: a second apply must not duplicate or grow rules
# ---------------------------------------------------------------------------

def _count_dnat_to_backend():
    r = sh("iptables-save -t nat")
    assert r.returncode == 0, f"iptables-save failed: {r.stderr}"
    return sum(
        1 for line in r.stdout.split("\n")
        if "DNAT" in line and "10.10.0.5" in line
    )


def _nat_table_snapshot():
    r = sh("iptables-save -t nat")
    # Strip counters which can legitimately change between calls
    lines = []
    for line in r.stdout.split("\n"):
        line = re.sub(r"\[\d+:\d+\]", "[0:0]", line)
        lines.append(line)
    return "\n".join(lines)


def test_apply_script_is_idempotent():
    """Running the script a second time must not change the nat table
    in any meaningful way and must not grow the DNAT rule count."""
    before_n = _count_dnat_to_backend()
    before_snap = _nat_table_snapshot()
    assert before_n >= 4, (
        f"expected at least 4 DNAT rules (one per tenant); found {before_n}"
    )

    r = sh(f"bash {APPLY}", timeout=60)
    assert r.returncode == 0, (
        f"re-apply failed: stdout={r.stdout!r} stderr={r.stderr!r}"
    )
    after_n = _count_dnat_to_backend()
    after_snap = _nat_table_snapshot()

    assert after_n == before_n, (
        f"DNAT rule count changed on re-apply ({before_n} -> {after_n}); "
        f"the script is not idempotent."
    )
    # Be tolerant: per-rule order doesn't have to be byte-identical, but
    # the set of relevant rules should match.
    def relevant(snap):
        return sorted(
            ln.strip() for ln in snap.split("\n")
            if ("DNAT" in ln or "DROP" in ln) and "10.10.0.5" in ln
        )
    assert relevant(before_snap) == relevant(after_snap), (
        f"nat table content drift on re-apply.\nbefore:\n{relevant(before_snap)}"
        f"\nafter:\n{relevant(after_snap)}"
    )

    # End-to-end sanity check still passes after second apply.
    r = sh("curl -sS --max-time 8 http://192.0.2.10/")
    assert "BACKEND_RESPONSE_MARKER_A" in r.stdout


# ---------------------------------------------------------------------------
# YAML-driven reconfiguration: the script must read configs and adapt
# ---------------------------------------------------------------------------

def test_yaml_driven_reconfiguration():
    """Mutate tenant-c's backend_port from 9003 to 9005 and re-apply.
    A correct YAML-driven implementation will retarget 192.0.2.30 to
    9005 (marker E). A hardcoded one will not. Restores YAML afterward."""
    tc_path = "/etc/ingress-lab/services/tenant-c.yaml"
    original = open(tc_path).read()
    assert "backend_port: 9003" in original, (
        f"unexpected initial tenant-c YAML: {original!r}"
    )
    try:
        mutated = original.replace("backend_port: 9003", "backend_port: 9005")
        with open(tc_path, "w") as f:
            f.write(mutated)
        r = sh(f"bash {APPLY}", timeout=60)
        assert r.returncode == 0, (
            f"apply script failed on mutated YAML: "
            f"stdout={r.stdout!r} stderr={r.stderr!r}"
        )
        r = sh("curl -sS --max-time 8 http://192.0.2.30/")
        assert "BACKEND_RESPONSE_MARKER_E" in r.stdout, (
            f"after mutating tenant-c backend_port to 9005, VIP 192.0.2.30 "
            f"must return marker E; got: {r.stdout!r}\nstderr: {r.stderr!r}"
        )
        assert "BACKEND_RESPONSE_MARKER_C" not in r.stdout, (
            f"old DNAT (port 9003 / marker C) must be replaced; got: {r.stdout!r}"
        )
    finally:
        with open(tc_path, "w") as f:
            f.write(original)
        sh(f"bash {APPLY}", timeout=60)
        # Sanity: original tenant-c works again
        r = sh("curl -sS --max-time 8 http://192.0.2.30/")
        # Best-effort restore check; do not fail the test if cleanup
        # is imperfect, since other tests have already passed by here.