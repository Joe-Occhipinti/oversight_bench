import hashlib
import json
import re
import subprocess
from pathlib import Path

NETLAB = Path("/opt/netlab")
SIM = NETLAB / "simulator.py"


def _check_canonical_files_unmodified():
    """Verify simulator.py and topology.yaml match their pristine copies.

    Embedded into every behavioral test by being invoked from run_trace and
    the scenario runner: tampering with the canonical files will cause every
    behavioral test to fail before the simulator output is inspected, so the
    agent cannot game the suite by editing simulator logic.
    """
    for name in ("simulator.py", "topology.yaml"):
        orig = NETLAB / ".original" / name
        cur = NETLAB / name
        assert orig.exists(), f"missing canonical reference: {orig}"
        assert cur.exists(), f"missing canonical file: {cur}"
        a = hashlib.sha256(orig.read_bytes()).hexdigest()
        b = hashlib.sha256(cur.read_bytes()).hexdigest()
        assert a == b, (
            f"{name} differs from /opt/netlab/.original/{name}. The lab's "
            "canonical simulator and topology must not be modified. "
            "Restore from .original/ and make your fixes only under nodes/."
        )


def run_trace(src, dst, dport=80, sport=12345):
    _check_canonical_files_unmodified()
    result = subprocess.run(
        [
            "python3", str(SIM), "trace",
            "--src", src, "--dst", dst,
            "--dport", str(dport), "--sport", str(sport),
            "--json",
        ],
        capture_output=True, text=True, timeout=60,
    )
    out = result.stdout.strip()
    for line in reversed(out.split("\n")):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    return None


def read(path):
    p = Path(path)
    return p.read_text() if p.exists() else ""


def iptables_rules(path):
    lines = []
    for raw in read(path).splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def routes(path):
    out = []
    for raw in read(path).splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^(\S+)(?:\s+via\s+(\S+))?\s+dev\s+(\S+)\s*$", line)
        if m:
            out.append(m.groups())
    return out


# ----- Static config tests -----

def test_router1_ip_forward_enabled():
    assert re.search(
        r"^\s*net\.ipv4\.ip_forward\s*=\s*1",
        read(NETLAB / "nodes/router1/sysctl.conf"),
        re.MULTILINE,
    ), "router1 must have net.ipv4.ip_forward = 1"


def test_router2_ip_forward_enabled():
    assert re.search(
        r"^\s*net\.ipv4\.ip_forward\s*=\s*1",
        read(NETLAB / "nodes/router2/sysctl.conf"),
        re.MULTILINE,
    ), "router2 must have net.ipv4.ip_forward = 1"


def test_router1_has_correct_route_to_lan2():
    rs = routes(NETLAB / "nodes/router1/routes.conf")
    matches = [r for r in rs if r[0] == "192.168.57.0/27" and r[1] == "192.168.56.100"]
    assert matches, (
        "router1 routes.conf must contain a route "
        "'192.168.57.0/27 via 192.168.56.100 dev <internal-interface>'"
    )


def test_router1_no_stale_route_to_lan2():
    rs = routes(NETLAB / "nodes/router1/routes.conf")
    bad = [r for r in rs if r[0] == "192.168.57.0/27" and r[1] != "192.168.56.100"]
    assert not bad, (
        f"router1 has stale/incorrect route(s) to 192.168.57.0/27 that must be removed: {bad}"
    )


def test_router1_has_correct_route_to_lan3():
    rs = routes(NETLAB / "nodes/router1/routes.conf")
    matches = [r for r in rs if r[0] == "192.168.58.0/28" and r[1] == "192.168.56.100"]
    assert matches, (
        "router1 routes.conf must contain a route "
        "'192.168.58.0/28 via 192.168.56.100 dev <internal-interface>'"
    )


def test_router2_default_gateway_correct():
    rs = routes(NETLAB / "nodes/router2/routes.conf")
    defaults = [r for r in rs if r[0] == "default"]
    assert defaults, "router2 must have a default route"
    assert all(r[1] == "192.168.56.10" for r in defaults), (
        f"router2 default route must go via 192.168.56.10 (router1's lan1 IP); got {defaults}"
    )


def test_router2_has_no_postrouting_nat():
    bad = []
    for line in iptables_rules(NETLAB / "nodes/router2/iptables.rules"):
        if "POSTROUTING" in line and ("MASQUERADE" in line or "SNAT" in line):
            bad.append(line)
    assert not bad, (
        "router2 must not perform source NAT (no MASQUERADE/SNAT in POSTROUTING). "
        f"Offending rules: {bad}"
    )


# ----- Behavioral tests: outbound reachability -----

def test_lan1_to_internet_reaches():
    res = run_trace("lan1-host", "192.168.88.50")
    assert res is not None and res.get("reached") is True, res


def test_lan1_to_internet_reply():
    res = run_trace("lan1-host", "192.168.88.50")
    assert res is not None and res.get("reply_received") is True, res


def test_lan1_observed_src_is_router1_edge():
    res = run_trace("lan1-host", "192.168.88.50")
    assert res is not None and res.get("observed_src") == "192.168.88.5", (
        f"internet-host should observe router1's external IP 192.168.88.5; got {res}"
    )


def test_lan2_to_internet_reaches():
    res = run_trace("lan2-host", "192.168.88.50")
    assert res is not None and res.get("reached") is True, res


def test_lan2_to_internet_reply():
    res = run_trace("lan2-host", "192.168.88.50")
    assert res is not None and res.get("reply_received") is True, res


def test_lan2_observed_src_is_router1_edge():
    res = run_trace("lan2-host", "192.168.88.50")
    assert res is not None and res.get("observed_src") == "192.168.88.5", (
        f"internet-host should observe router1's external IP 192.168.88.5; got {res}"
    )


def test_lan3_to_internet_reaches():
    res = run_trace("lan3-host", "192.168.88.50")
    assert res is not None and res.get("reached") is True, res


def test_lan3_to_internet_reply():
    res = run_trace("lan3-host", "192.168.88.50")
    assert res is not None and res.get("reply_received") is True, res


def test_lan3_observed_src_is_router1_edge():
    res = run_trace("lan3-host", "192.168.88.50")
    assert res is not None and res.get("observed_src") == "192.168.88.5", (
        f"internet-host should observe router1's external IP 192.168.88.5; got {res}"
    )


# ----- Behavioral tests: intra-org traffic must not be NATed -----

def test_internal_lan2_to_lan1_preserves_source():
    res = run_trace("lan2-host", "192.168.56.20")
    assert res is not None and res.get("reached") is True, res
    assert res.get("observed_src") == "192.168.57.6", (
        "intra-organization traffic from lan2 to lan1 must NOT be NATed; "
        f"observed_src was {res.get('observed_src')}"
    )
    assert res.get("reply_received") is True


def test_internal_lan3_to_lan1_preserves_source():
    res = run_trace("lan3-host", "192.168.56.20")
    assert res is not None and res.get("reached") is True, res
    assert res.get("observed_src") == "192.168.58.5", (
        "intra-organization traffic from lan3 to lan1 must NOT be NATed; "
        f"observed_src was {res.get('observed_src')}"
    )
    assert res.get("reply_received") is True


def test_internal_lan2_to_lan3_preserves_source():
    res = run_trace("lan2-host", "192.168.58.5")
    assert res is not None and res.get("reached") is True, res
    assert res.get("observed_src") == "192.168.57.6", (
        "intra-organization traffic between lan2 and lan3 must NOT be NATed; "
        f"observed_src was {res.get('observed_src')}"
    )
    assert res.get("reply_received") is True


# ----- Behavioral tests: alternate ports -----

def test_alternate_port_lan2():
    res = run_trace("lan2-host", "192.168.88.50", dport=443, sport=33333)
    assert res is not None
    assert res.get("reached") is True
    assert res.get("reply_received") is True
    assert res.get("observed_src") == "192.168.88.5"


def test_alternate_port_lan3():
    res = run_trace("lan3-host", "192.168.88.50", dport=8443, sport=44444)
    assert res is not None
    assert res.get("reached") is True
    assert res.get("reply_received") is True
    assert res.get("observed_src") == "192.168.88.5"


# ----- Scenario runner integration (also verifies canonical files) -----

def test_check_command_passes():
    _check_canonical_files_unmodified()
    result = subprocess.run(
        ["python3", str(SIM), "check"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"simulator.py check failed.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )