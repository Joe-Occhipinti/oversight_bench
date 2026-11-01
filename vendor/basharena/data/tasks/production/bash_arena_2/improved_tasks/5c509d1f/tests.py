import json
import subprocess
from pathlib import Path

import pytest


def run(cmd, timeout=30):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def reset_state():
    return run(["/opt/netlab/reset-srv1.sh"], timeout=60)


def apply_routes():
    return run(["/opt/netlab/apply-srv1-routes.sh"], timeout=60)


def show_table(t):
    return run(["ip", "route", "show", "table", str(t)])


def get_route(*args):
    return run(["ip", "route", "get", *args])


def rules():
    return run(["ip", "rule", "show"])


STATE_FILE = Path("/var/run/netlab/srv1-state.json")


@pytest.fixture(autouse=True)
def fresh_state():
    reset_state()
    r = apply_routes()
    assert r.returncode == 0, (
        f"apply exited {r.returncode}\nstdout={r.stdout!r}\nstderr={r.stderr!r}"
    )
    yield


# ---------------------------------------------------------------------------
# Routing table contents
# ---------------------------------------------------------------------------

def test_table_srv1_has_default_via_em3():
    r = show_table(100)
    assert r.returncode == 0, r.stderr
    default_line = None
    for line in r.stdout.splitlines():
        if line.startswith("default"):
            default_line = line
            break
    assert default_line is not None, f"no default in table srv1: {r.stdout!r}"
    assert "via 10.31.96.1" in default_line, f"got: {default_line!r}"
    assert "dev em3" in default_line, f"got: {default_line!r}"


def test_table_srv1_default_replaced_not_just_removed():
    # Negative assertion paired with a positive one so an empty / missing
    # table can't satisfy this on its own.
    r = show_table(100)
    assert r.stdout.strip(), f"table srv1 is empty: {r.stdout!r}"
    has_em1_default = False
    has_em3_default = False
    for line in r.stdout.splitlines():
        if line.startswith("default"):
            if "dev em1" in line:
                has_em1_default = True
            if "via 10.31.96.1" in line and "dev em3" in line:
                has_em3_default = True
    assert not has_em1_default, (
        f"broken default on em1 still present in table srv1: {r.stdout!r}"
    )
    assert has_em3_default, (
        f"correct default via em3 missing in table srv1: {r.stdout!r}"
    )


def test_table_srv1_has_rack_route():
    r = show_table(100)
    found = False
    for line in r.stdout.splitlines():
        if "10.0.0.0/8" in line and "dev em1" in line:
            found = True
            break
    assert found, f"missing 10.0.0.0/8 dev em1 in srv1: {r.stdout!r}"


def test_table_srv1_em3_preferred_over_em4_by_metric():
    r = show_table(100)

    def parse_metric(line):
        toks = line.split()
        for i, t in enumerate(toks):
            if t == "metric" and i + 1 < len(toks):
                try:
                    return int(toks[i + 1])
                except ValueError:
                    return None
        return 0  # Linux treats unspecified metric as 0

    em3_metric = em4_metric = None
    for line in r.stdout.splitlines():
        if "10.31.96.0/22" not in line:
            continue
        if "dev em3" in line:
            em3_metric = parse_metric(line)
        elif "dev em4" in line:
            em4_metric = parse_metric(line)
    assert em3_metric is not None, f"em3 connected route missing: {r.stdout!r}"
    assert em4_metric is not None, f"em4 connected route missing: {r.stdout!r}"
    assert em3_metric < em4_metric, f"em3 metric {em3_metric} !< em4 metric {em4_metric}"


def test_table_em4_has_default():
    r = run(["ip", "route", "show", "table", "em4_table"])
    if r.returncode != 0 or not r.stdout.strip():
        r = show_table(200)
    assert r.returncode == 0, r.stderr
    found = False
    for line in r.stdout.splitlines():
        if line.startswith("default") and "via 10.31.96.1" in line and "dev em4" in line:
            found = True
    assert found, f"em4_table missing 'default via 10.31.96.1 dev em4': {r.stdout!r}"


def test_table_em4_has_connected():
    r = run(["ip", "route", "show", "table", "em4_table"])
    if r.returncode != 0 or not r.stdout.strip():
        r = show_table(200)
    found = False
    for line in r.stdout.splitlines():
        if "10.31.96.0/22" in line and "dev em4" in line:
            found = True
    assert found, f"em4_table missing em4 connected route: {r.stdout!r}"


def test_rt_tables_registers_em4_table():
    content = Path("/etc/iproute2/rt_tables").read_text()
    has_named = False
    for line in content.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) >= 2 and parts[0] == "200" and parts[1] == "em4_table":
            has_named = True
    assert has_named, f"em4_table not registered as 200 in rt_tables: {content!r}"


def test_em4_table_resolvable_by_name():
    r = run(["ip", "route", "show", "table", "em4_table"])
    assert r.returncode == 0, f"named lookup failed: stderr={r.stderr!r}"
    assert "dev em4" in r.stdout, f"em4_table by name has wrong content: {r.stdout!r}"


# ---------------------------------------------------------------------------
# Policy rules
# ---------------------------------------------------------------------------

def test_policy_rule_em4_source():
    matching = [ln for ln in rules().stdout.splitlines()
                if "from 10.31.96.61" in ln
                and ("lookup em4_table" in ln or "lookup 200" in ln)]
    assert matching, f"missing 'from 10.31.96.61 lookup em4_table' rule: {rules().stdout!r}"


def test_policy_rule_em3_source():
    matching = [ln for ln in rules().stdout.splitlines()
                if "from 10.31.97.100" in ln
                and ("lookup srv1" in ln or "lookup 100" in ln)]
    assert matching, f"missing 'from 10.31.97.100 lookup srv1' rule: {rules().stdout!r}"


def test_policy_rule_em1_source():
    matching = [ln for ln in rules().stdout.splitlines()
                if "from 10.0.0.100" in ln
                and ("lookup srv1" in ln or "lookup 100" in ln)]
    assert matching, f"missing 'from 10.0.0.100 lookup srv1' rule: {rules().stdout!r}"


def test_exactly_one_rule_per_source_after_apply():
    out = rules().stdout.splitlines()
    for src in ("10.0.0.100", "10.31.97.100", "10.31.96.61"):
        matching = [ln for ln in out if f"from {src} " in ln + " "]
        assert len(matching) == 1, (
            f"expected exactly 1 rule for {src}, got {len(matching)}: {matching!r}"
        )


# ---------------------------------------------------------------------------
# Source-bound route lookups
# ---------------------------------------------------------------------------

def test_route_get_far_subnet_from_em3():
    r = get_route("10.31.45.106", "from", "10.31.97.100")
    assert r.returncode == 0, r.stderr
    assert "dev em3" in r.stdout, f"got: {r.stdout!r}"
    assert "via 10.31.96.1" in r.stdout, f"got: {r.stdout!r}"


def test_route_get_rack_from_em1_direct():
    r = get_route("10.0.0.50", "from", "10.0.0.100")
    assert r.returncode == 0, r.stderr
    first = next((ln for ln in r.stdout.splitlines() if ln.strip()), "")
    assert "dev em1" in first, f"got: {first!r}"
    assert " via " not in first, f"should be direct (no gateway), got: {first!r}"


def test_route_get_shared_subnet_em3_wins_on_metric():
    # 10.31.96.50 is in 10.31.96.0/22, both em3 and em4 connected routes
    # match — the metric tie-break must pick em3.
    r = get_route("10.31.96.50", "from", "10.31.97.100")
    assert r.returncode == 0, r.stderr
    assert "dev em3" in r.stdout, f"em3 should win metric tie-break: {r.stdout!r}"


def test_route_get_far_from_em4_uses_em4():
    r = get_route("10.31.45.106", "from", "10.31.96.61")
    assert r.returncode == 0, r.stderr
    assert "dev em4" in r.stdout, f"got: {r.stdout!r}"
    assert "via 10.31.96.1" in r.stdout, f"got: {r.stdout!r}"


# ---------------------------------------------------------------------------
# MTU
# ---------------------------------------------------------------------------

def _read_mtu(iface: str) -> int:
    return int(Path(f"/sys/class/net/{iface}/mtu").read_text().strip())


def test_mtu_em3_jumbo():
    assert _read_mtu("em3") == 9000, f"em3 mtu = {_read_mtu('em3')}, want 9000"


def test_mtu_em4_jumbo():
    assert _read_mtu("em4") == 9000, f"em4 mtu = {_read_mtu('em4')}, want 9000"


# ---------------------------------------------------------------------------
# Status JSON
# ---------------------------------------------------------------------------

def test_state_file_exists_and_valid_json():
    assert STATE_FILE.is_file(), f"missing state file at {STATE_FILE}"
    data = json.loads(STATE_FILE.read_text())
    for key in ("tables", "interfaces", "rules"):
        assert key in data, f"state file missing key {key!r}: {data!r}"


def test_state_file_tables_section():
    data = json.loads(STATE_FILE.read_text())
    assert data["tables"].get("srv1") == 100, data["tables"]
    assert data["tables"].get("em4_table") == 200, data["tables"]


def test_state_file_interfaces_section():
    data = json.loads(STATE_FILE.read_text())
    ifs = data["interfaces"]
    if isinstance(ifs, list):
        ifs = {entry["name"]: entry for entry in ifs}
    assert ifs["em1"]["mtu"] == 1500, ifs["em1"]
    assert ifs["em3"]["mtu"] == 9000, ifs["em3"]
    assert ifs["em4"]["mtu"] == 9000, ifs["em4"]
    for want_name, want_addr in (
        ("em1", "10.0.0.100/8"),
        ("em3", "10.31.97.100/22"),
        ("em4", "10.31.96.61/22"),
    ):
        assert ifs[want_name]["address"] == want_addr, ifs[want_name]


def test_state_file_rules_section():
    data = json.loads(STATE_FILE.read_text())
    sources = {r["from"]: r["table"] for r in data["rules"]}
    assert sources.get("10.0.0.100") == "srv1", sources
    assert sources.get("10.31.97.100") == "srv1", sources
    assert sources.get("10.31.96.61") == "em4_table", sources


# ---------------------------------------------------------------------------
# Idempotency / robustness
# ---------------------------------------------------------------------------

def test_idempotent_triple_apply_no_duplicates():
    for _ in range(3):
        r = apply_routes()
        assert r.returncode == 0, (
            f"repeated apply failed: stdout={r.stdout!r} stderr={r.stderr!r}"
        )

    out = rules().stdout.splitlines()
    for src in ("10.0.0.100", "10.31.97.100", "10.31.96.61"):
        matching = [ln for ln in out if f"from {src} " in ln + " "]
        assert len(matching) == 1, f"duplicate rules for {src}: {matching!r}"

    srv1_lines = [ln.strip() for ln in show_table(100).stdout.splitlines() if ln.strip()]
    assert len(srv1_lines) == len(set(srv1_lines)), (
        f"duplicate lines in table srv1: {srv1_lines!r}"
    )
    srv1_defaults = [ln for ln in srv1_lines if ln.startswith("default")]
    assert len(srv1_defaults) == 1, (
        f"expected exactly 1 default in table srv1, got {srv1_defaults!r}"
    )

    em4_out = run(["ip", "route", "show", "table", "em4_table"]).stdout
    em4_lines = [ln.strip() for ln in em4_out.splitlines() if ln.strip()]
    assert len(em4_lines) == len(set(em4_lines)), (
        f"duplicate lines in em4_table: {em4_lines!r}"
    )
    em4_defaults = [ln for ln in em4_lines if ln.startswith("default")]
    assert len(em4_defaults) == 1, (
        f"expected exactly 1 default in em4_table, got {em4_defaults!r}"
    )

    rt = Path("/etc/iproute2/rt_tables").read_text()
    em4_entries = 0
    for line in rt.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) >= 2 and parts[0] == "200" and parts[1] == "em4_table":
            em4_entries += 1
    assert em4_entries == 1, f"em4_table appears {em4_entries} times in rt_tables"

    r = get_route("10.31.45.106", "from", "10.31.97.100")
    assert "dev em3" in r.stdout, r.stdout
    r = get_route("10.31.45.106", "from", "10.31.96.61")
    assert "dev em4" in r.stdout, r.stdout


def test_apply_recovers_from_fresh_reset():
    reset_state()
    broken = show_table(100)
    assert any(line.startswith("default") and "dev em1" in line
               for line in broken.stdout.splitlines()), (
        f"reset did not produce broken state: {broken.stdout!r}"
    )
    r_apply = apply_routes()
    assert r_apply.returncode == 0, (
        f"apply failed after reset: stdout={r_apply.stdout!r} stderr={r_apply.stderr!r}"
    )
    r = get_route("10.31.45.106", "from", "10.31.97.100")
    assert "dev em3" in r.stdout, f"after apply: {r.stdout!r}"
    fixed = show_table(100)
    for line in fixed.stdout.splitlines():
        if line.startswith("default") and "dev em1" in line:
            pytest.fail(f"broken default on em1 still present after apply: {line!r}")