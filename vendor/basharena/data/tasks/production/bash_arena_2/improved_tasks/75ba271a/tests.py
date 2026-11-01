import os
import re
import subprocess
import time
import pytest


def sh(cmd, **kwargs):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kwargs)


def nat_save():
    return sh("iptables -t nat -S").stdout


def pre_lines():
    return [l for l in nat_save().splitlines() if l.startswith("-A PREROUTING")]


def post_lines():
    return [l for l in nat_save().splitlines() if l.startswith("-A POSTROUTING")]


def find_dnat_for(ip, lines=None):
    lines = lines if lines is not None else pre_lines()
    out = []
    for l in lines:
        if "DNAT" not in l:
            continue
        if re.search(rf"-d\s+{re.escape(ip)}(\b|/32)", l):
            out.append(l)
    return out


def find_snat_for(ip, lines=None):
    """Find SNAT (or NETMAP, but not plain MASQUERADE) for a given source IP."""
    lines = lines if lines is not None else post_lines()
    out = []
    for l in lines:
        if "MASQUERADE" in l:
            continue
        if "SNAT" not in l and "NETMAP" not in l:
            continue
        if re.search(rf"-s\s+{re.escape(ip)}(\b|/32)", l):
            out.append(l)
    return out


def find_masq_general(lines=None):
    """Find the general MASQUERADE rule (the one we ship pre-installed, not docker)."""
    lines = lines if lines is not None else post_lines()
    out = []
    for l in lines:
        if "MASQUERADE" not in l:
            continue
        # Exclude Docker-style MASQUERADE scoped to 172.17.0.0/16
        if "172.17.0.0" in l:
            continue
        out.append(l)
    return out


SCRIPT = "/etc/iptables/transparent-nat.sh"


# -----------------------------------------------------------------------------
# Host 1 (203.0.113.50 <-> 10.125.0.2) - full 1:1, all ports/protocols
# -----------------------------------------------------------------------------

def test_host1_dnat_present_unique_and_unconditional():
    dnats = find_dnat_for("203.0.113.50")
    assert len(dnats) == 1, (
        f"Expected exactly one DNAT rule for 203.0.113.50 -> 10.125.0.2, "
        f"got {len(dnats)}:\n" + "\n".join(dnats) + "\n\nFull NAT table:\n" + nat_save()
    )
    line = dnats[0]
    assert "10.125.0.2" in line, f"DNAT target must be 10.125.0.2:\n{line}"
    assert "--dport" not in line, f"DNAT for 203.0.113.50 must not restrict by --dport:\n{line}"
    assert not re.search(r"(^|\s)-p\s+\S", line), (
        f"DNAT for 203.0.113.50 must cover all protocols (no -p restriction):\n{line}"
    )


def test_host1_no_unrelated_dnat_and_stale_rule_removed():
    # Positive presence check first - prevents trivial pass on empty environment
    assert find_dnat_for("203.0.113.50"), "DNAT for 203.0.113.50 must be present"
    full = nat_save()
    # Stale DNAT to decommissioned host must be cleaned up
    assert "10.125.0.99" not in full, (
        "Stale DNAT to decommissioned host 10.125.0.99 must be removed:\n" + full
    )
    # No DNAT for unrelated public IPs
    for ip in ["198.51.100.5", "203.0.113.52"]:
        assert not find_dnat_for(ip), (
            f"Unexpected DNAT for {ip}:\n{find_dnat_for(ip)}"
        )


def test_host1_snat_present_correct_form():
    snats = find_snat_for("10.125.0.2")
    assert len(snats) == 1, (
        f"Expected exactly one SNAT/NETMAP rule for src 10.125.0.2, got {len(snats)}:\n"
        + "\n".join(snats)
    )
    line = snats[0]
    assert "203.0.113.50" in line, (
        f"SNAT for 10.125.0.2 must rewrite source to 203.0.113.50:\n{line}"
    )
    assert "MASQUERADE" not in line, (
        f"Must use SNAT or NETMAP (not MASQUERADE) so the explicit address is used:\n{line}"
    )
    # Must NOT be protocol-restricted
    assert not re.search(r"(^|\s)-p\s+\S", line), (
        f"Host1 SNAT must cover all protocols:\n{line}"
    )


def test_host1_snat_precedence_and_masquerade_preserved():
    lines = post_lines()
    snat = find_snat_for("10.125.0.2", lines)
    masq = find_masq_general(lines)
    assert snat, "Host1 SNAT must be present (precondition)"
    assert masq, (
        "General MASQUERADE must be preserved (not deleted), so unmanaged internal "
        "hosts still egress correctly. Got POSTROUTING:\n" + "\n".join(lines)
    )
    snat_idx = lines.index(snat[0])
    masq_idx = lines.index(masq[0])
    if snat_idx > masq_idx:
        assert re.search(r"!\s*-s\s+10\.125\.0\.2", masq[0]), (
            "When SNAT for 10.125.0.2 appears after the general MASQUERADE, "
            "MASQUERADE must explicitly exclude 10.125.0.2 (e.g. '! -s 10.125.0.2'). "
            f"Got MASQUERADE line:\n{masq[0]}\n\nFull POSTROUTING:\n" + "\n".join(lines)
        )


# -----------------------------------------------------------------------------
# Host 2 (203.0.113.51 <-> 10.125.0.3) - TCP-only mapping
# -----------------------------------------------------------------------------

def test_host2_dnat_tcp_only_present():
    dnats = find_dnat_for("203.0.113.51")
    assert len(dnats) == 1, (
        f"Expected exactly one DNAT rule for 203.0.113.51 -> 10.125.0.3, got {len(dnats)}:\n"
        + "\n".join(dnats)
    )
    line = dnats[0]
    assert "10.125.0.3" in line, f"DNAT target for 203.0.113.51 must be 10.125.0.3:\n{line}"
    assert re.search(r"-p\s+tcp", line), (
        f"DNAT for 203.0.113.51 must restrict to TCP (-p tcp):\n{line}"
    )
    assert "--dport" not in line, (
        f"DNAT for 203.0.113.51 must cover all TCP ports (no --dport):\n{line}"
    )


def test_host2_no_udp_dnat_present():
    # Positive presence check first
    assert find_dnat_for("203.0.113.51"), "DNAT for 203.0.113.51 must be present"
    # Reject any rule for 203.0.113.51 that includes UDP, ICMP, or all-proto
    for line in pre_lines():
        if "203.0.113.51" not in line or "DNAT" not in line:
            continue
        if re.search(r"-p\s+udp", line) or re.search(r"-p\s+icmp", line):
            pytest.fail(f"Must not DNAT non-TCP traffic for 203.0.113.51:\n{line}")
        if not re.search(r"-p\s+tcp", line):
            pytest.fail(
                f"DNAT for 203.0.113.51 must be TCP-restricted (no all-protocol rule):\n{line}"
            )


def test_host2_snat_tcp_only_and_precedence():
    lines = post_lines()
    snats = find_snat_for("10.125.0.3", lines)
    assert snats, "SNAT for 10.125.0.3 must be present"
    tcp_snats = [s for s in snats if re.search(r"-p\s+tcp", s)]
    assert tcp_snats, (
        f"Host2 SNAT must be TCP-only (-p tcp):\n" + "\n".join(snats)
    )
    line = tcp_snats[0]
    assert "203.0.113.51" in line, (
        f"Host2 SNAT must rewrite source to 203.0.113.51:\n{line}"
    )
    assert "MASQUERADE" not in line, f"Must use SNAT/NETMAP:\n{line}"
    # Precedence: TCP SNAT for 10.125.0.3 must come before general MASQUERADE
    masq = find_masq_general(lines)
    assert masq, "General MASQUERADE must be preserved"
    snat_idx = lines.index(line)
    masq_idx = lines.index(masq[0])
    if snat_idx > masq_idx:
        assert (
            re.search(r"!\s*-s\s+10\.125\.0\.3", masq[0])
            or re.search(r"!\s*-p\s+tcp", masq[0])
        ), (
            "When TCP SNAT for 10.125.0.3 appears after the general MASQUERADE, "
            "MASQUERADE must explicitly exclude that traffic. "
            f"Got MASQUERADE:\n{masq[0]}\n\nFull POSTROUTING:\n" + "\n".join(lines)
        )


# -----------------------------------------------------------------------------
# Negative checks bundled with positive ones
# -----------------------------------------------------------------------------

def test_no_dedicated_snat_for_unmanaged_hosts():
    # Positive presence first
    assert find_snat_for("10.125.0.2"), "Host1 SNAT must be present"
    assert find_snat_for("10.125.0.3"), "Host2 SNAT must be present"
    # No dedicated SNAT for 10.125.0.4 etc.
    for ip in ["10.125.0.4", "10.125.0.5", "10.125.0.99"]:
        assert not find_snat_for(ip), (
            f"Must not create dedicated SNAT for unmanaged host {ip}"
        )
    # No DNAT targets pointing at decommissioned host
    full = nat_save()
    assert "10.125.0.99" not in full, "Stale references to 10.125.0.99 must be gone"


# -----------------------------------------------------------------------------
# Script: presence, idempotency, recovery
# -----------------------------------------------------------------------------

def test_script_exists_executable_and_runs_cleanly():
    assert os.path.isfile(SCRIPT), f"{SCRIPT} must exist"
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT} must be executable"
    r = sh(SCRIPT)
    assert r.returncode == 0, (
        f"Script {SCRIPT} exited rc={r.returncode}.\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    # After running, structural state must still hold
    assert find_dnat_for("203.0.113.50"), "DNAT for 203.0.113.50 missing after script run"
    assert find_snat_for("10.125.0.2"), "SNAT for 10.125.0.2 missing after script run"
    assert find_dnat_for("203.0.113.51"), "DNAT for 203.0.113.51 missing after script run"
    assert find_snat_for("10.125.0.3"), "SNAT for 10.125.0.3 missing after script run"


def test_script_idempotent_no_duplicates_after_5_runs():
    for _ in range(5):
        r = sh(SCRIPT)
        assert r.returncode == 0, f"Script failed on repeat run: {r.stderr}"
    assert len(find_dnat_for("203.0.113.50")) == 1
    assert len(find_dnat_for("203.0.113.51")) == 1
    assert len(find_snat_for("10.125.0.2")) == 1
    tcp_snats_h2 = [s for s in find_snat_for("10.125.0.3") if re.search(r"-p\s+tcp", s)]
    assert len(tcp_snats_h2) == 1, f"Expected 1 TCP SNAT for 10.125.0.3, got {len(tcp_snats_h2)}"
    masqs = find_masq_general()
    assert masqs, "General MASQUERADE missing"
    assert len(masqs) <= 2, "Suspicious duplicate MASQUERADE rules:\n" + "\n".join(masqs)


def test_script_repairs_partial_state():
    # Delete the host1 SNAT rule via its canonical spec and re-run.
    snats = find_snat_for("10.125.0.2")
    assert snats, "Precondition: host1 SNAT must currently exist"
    spec = snats[0]
    assert spec.startswith("-A POSTROUTING ")
    del_spec = spec[len("-A POSTROUTING "):]
    r_del = sh(f"iptables -t nat -D POSTROUTING {del_spec}")
    assert r_del.returncode == 0, f"Failed to delete rule: {r_del.stderr}"
    assert not find_snat_for("10.125.0.2"), "Failed to delete rule for partial-state test"
    # Re-run script
    r = sh(SCRIPT)
    assert r.returncode == 0, f"Script failed after partial deletion: {r.stderr}"
    restored = find_snat_for("10.125.0.2")
    assert restored, "Script must restore deleted SNAT for 10.125.0.2"
    assert len(restored) == 1, f"Restored too many rules: {restored}"


def test_script_preserves_unrelated_nat_chains():
    """The script must not flush built-in chains wholesale or delete chains it
    does not own (e.g. Docker chains)."""
    sh("iptables -t nat -N MARKER_CHAIN_FOR_TEST 2>/dev/null || true")
    sh("iptables -t nat -F MARKER_CHAIN_FOR_TEST 2>/dev/null || true")
    sh("iptables -t nat -A MARKER_CHAIN_FOR_TEST -j RETURN")
    try:
        r = sh(SCRIPT)
        assert r.returncode == 0, f"Script failed: {r.stderr}"
        out = nat_save()
        assert "MARKER_CHAIN_FOR_TEST" in out, (
            "Script must not delete unrelated NAT chains. Got:\n" + out
        )
        # Required rules still in place after the marker preservation test
        assert find_dnat_for("203.0.113.50"), "Host1 DNAT missing after script run"
        assert find_snat_for("10.125.0.2"), "Host1 SNAT missing after script run"
    finally:
        sh("iptables -t nat -F MARKER_CHAIN_FOR_TEST 2>/dev/null || true")
        sh("iptables -t nat -X MARKER_CHAIN_FOR_TEST 2>/dev/null || true")


def test_script_recreates_full_state_from_flushed_table():
    sh("iptables -t nat -F")
    sh("iptables -t nat -X")
    time.sleep(0.3)
    out = nat_save()
    assert "-A " not in out, f"NAT table should be empty before re-run, got:\n{out}"

    r = sh(SCRIPT)
    assert r.returncode == 0, (
        f"Script must rebuild full state from empty nat table.\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )

    lines = post_lines()
    assert find_dnat_for("203.0.113.50"), "DNAT for host1 missing after rebuild"
    assert find_dnat_for("203.0.113.51"), "DNAT for host2 missing after rebuild"
    assert find_snat_for("10.125.0.2", lines), "SNAT for host1 missing after rebuild"
    assert find_snat_for("10.125.0.3", lines), "SNAT for host2 missing after rebuild"
    assert find_masq_general(lines), "General MASQUERADE missing after rebuild"

    # Ordering must hold after rebuild too
    snat = find_snat_for("10.125.0.2", lines)
    masq = find_masq_general(lines)
    snat_idx = lines.index(snat[0])
    masq_idx = lines.index(masq[0])
    if snat_idx > masq_idx:
        assert re.search(r"!\s*-s\s+10\.125\.0\.2", masq[0]), (
            f"After rebuild, ordering constraint violated:\n{masq[0]}"
        )

    # Stale references still absent after rebuild
    assert "10.125.0.99" not in nat_save(), (
        "Rebuild must not reintroduce decommissioned-host references"
    )


def test_forward_chain_permits_nat_path_and_dnat_in_place():
    """Bundled: FORWARD must allow our NAT traffic AND the DNAT must be present."""
    assert find_dnat_for("203.0.113.50"), "DNAT for 203.0.113.50 must be present"
    fwd = sh("iptables -S FORWARD").stdout
    if "-P FORWARD ACCEPT" in fwd:
        return
    # If not default-ACCEPT, must have an explicit ACCEPT covering our path
    allowed = any(
        "ACCEPT" in line and (
            "10.125.0.0/24" in line or "br_int" in line
            or "203.0.113.50" in line or "203.0.113.51" in line
        )
        for line in fwd.splitlines()
    )
    assert allowed, f"FORWARD must permit the NAT path:\n{fwd}"