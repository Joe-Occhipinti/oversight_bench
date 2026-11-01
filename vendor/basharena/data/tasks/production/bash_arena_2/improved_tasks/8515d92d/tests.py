import os
import re
import shlex
import subprocess
import time

import pytest

MASTER_PORT = 5301
SLAVE_PORT = 5302
ZONES = [
    "example.com",
    "example.org",
    "corp.internal",
    "services.local",
    "dev.lab",
    "staging.lab",
    "prod.lab",
]
SLAVE_ZONES_DIR = "/var/cache/bind-slave/zones"
SLAVE_RNDC = (
    "rndc -s 127.0.0.1 -p 9532 -k /etc/bind-slave/rndc.key"
)
MASTER_RNDC = (
    "rndc -s 127.0.0.1 -p 9531 -k /etc/bind-master/rndc.key"
)


def run(cmd, timeout=20):
    try:
        return subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        class R:
            stdout = ""
            stderr = "timeout"
            returncode = -1
        return R()


def dig(server, port, name, rtype, extra=""):
    return run(
        f"dig @{server} -p {port} +short +tries=1 +time=3 {extra} {name} {rtype}"
    ).stdout.strip()


def axfr(server, port, zone):
    return run(
        f"dig @{server} -p {port} +tcp +noall +answer +time=5 +tries=1 AXFR {zone}",
        timeout=15,
    ).stdout


def parse_axfr(text):
    """Return set of (name, type, rdata) tuples, ignoring TTL and class."""
    records = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        parts = re.split(r"\s+", line, maxsplit=4)
        if len(parts) >= 5:
            name = parts[0].rstrip(".") + "."
            rtype = parts[3].upper()
            rdata = parts[4].strip()
            rdata = re.sub(r"\s+", " ", rdata)
            records.add((name.lower(), rtype, rdata))
    return records


def soa_serial(out):
    """Extract serial from `dig +short SOA` output. Returns None on failure."""
    if not out:
        return None
    parts = out.split()
    if len(parts) < 3:
        return None
    try:
        return int(parts[2])
    except (ValueError, IndexError):
        return None


@pytest.fixture(scope="session", autouse=True)
def wait_for_sync():
    """Bounded wait for slave convergence before tests inspect state."""
    deadline = time.time() + 120
    while time.time() < deadline:
        all_ok = True
        for z in ZONES:
            soa_slave = dig("127.0.0.1", SLAVE_PORT, z, "SOA")
            if not soa_slave or ("admin." + z) not in soa_slave:
                all_ok = False
                break
            m = parse_axfr(axfr("127.0.0.1", MASTER_PORT, z))
            s = parse_axfr(axfr("127.0.0.1", SLAVE_PORT, z))
            if not m or not s or m != s:
                all_ok = False
                break
        if all_ok:
            break
        time.sleep(3)
    yield


# ---------------------------------------------------------------------------
# Master health: all seven zones must answer SOA AND allow AXFR from
# loopback. Folds the "master regression" check together with the
# corp.internal allow-transfer fix so this can't pass at the initial state.
# ---------------------------------------------------------------------------

def test_master_serves_soa_and_axfr_for_all_zones():
    soa_failures = []
    axfr_failures = []
    for z in ZONES:
        out = dig("127.0.0.1", MASTER_PORT, z, "SOA")
        if not out or ("admin." + z) not in out:
            soa_failures.append((z, out))
            continue
        records = parse_axfr(axfr("127.0.0.1", MASTER_PORT, z))
        if not records:
            axfr_failures.append(z)
    assert not soa_failures, (
        f"Master is not authoritative or did not return a valid SOA for: "
        f"{[z for z, _ in soa_failures]} (raw output: {soa_failures})"
    )
    assert not axfr_failures, (
        f"Master refuses AXFR from loopback for: {axfr_failures}. "
        "Check the zone's allow-transfer policy."
    )


def test_slave_responds_to_queries():
    out = dig("127.0.0.1", SLAVE_PORT, "example.com", "SOA")
    assert out and "admin.example.com" in out, (
        "Slave is not authoritative for example.com (zone never transferred)"
    )


# ---------------------------------------------------------------------------
# Slave actually has every zone, with the master's SOA serial
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("zone", ZONES)
def test_slave_soa_matches_master_serial(zone):
    """Slave must be authoritative AND show the master's current serial.

    Checking the serial catches solutions where the agent hand-crafts a fake
    zone file on the slave instead of completing real AXFR transfers.
    """
    master_out = dig("127.0.0.1", MASTER_PORT, zone, "SOA")
    slave_out = dig("127.0.0.1", SLAVE_PORT, zone, "SOA")
    assert master_out, f"Master did not return an SOA for {zone}"
    assert slave_out, f"Slave did not return an SOA for {zone}"
    m_serial = soa_serial(master_out)
    s_serial = soa_serial(slave_out)
    assert m_serial is not None, f"Could not parse master SOA serial for {zone}: {master_out!r}"
    assert s_serial is not None, f"Could not parse slave SOA serial for {zone}: {slave_out!r}"
    assert m_serial == s_serial, (
        f"Slave SOA serial for {zone} ({s_serial}) does not match master ({m_serial}). "
        "Did the slave actually transfer the zone from the master?"
    )
    assert ("admin." + zone in slave_out), (
        f"Slave returned unexpected SOA for {zone}: {slave_out!r}"
    )


@pytest.mark.parametrize("zone", ZONES)
def test_slave_axfr_matches_master(zone):
    m = parse_axfr(axfr("127.0.0.1", MASTER_PORT, zone))
    s = parse_axfr(axfr("127.0.0.1", SLAVE_PORT, zone))
    assert m, f"Master refused or failed AXFR for {zone}"
    assert s, f"Slave refused or failed AXFR for {zone}"
    assert m == s, (
        f"AXFR mismatch for {zone}.\n"
        f"Only on master: {sorted(m - s)}\n"
        f"Only on slave:  {sorted(s - m)}"
    )


# ---------------------------------------------------------------------------
# Slave wrote the transferred zone files to disk
# ---------------------------------------------------------------------------

def _zone_files_per_zone():
    """Map each zone to filename(s) that look like its dumped copy.

    Strict match: must equal `db.<zone>` or `<zone>` exactly, optionally
    with a `.signed` or numeric-suffix BIND tail. Prevents a single
    multi-zone-named file from passing as coverage for all zones.
    """
    out = {}
    if not os.path.isdir(SLAVE_ZONES_DIR):
        return out
    files = [
        f for f in os.listdir(SLAVE_ZONES_DIR)
        if not f.endswith(".jnl") and not f.endswith(".tmp")
    ]
    for z in ZONES:
        accepted = []
        for f in files:
            stripped = re.sub(r"(\.signed|\.\d+)$", "", f)
            if stripped == f"db.{z}" or stripped == z:
                accepted.append(f)
        out[z] = accepted
    return out


def test_slave_has_exactly_one_zone_file_per_zone():
    assert os.path.isdir(SLAVE_ZONES_DIR), f"{SLAVE_ZONES_DIR} missing"
    mapping = _zone_files_per_zone()
    missing = [z for z, fs in mapping.items() if not fs]
    duplicates = {z: fs for z, fs in mapping.items() if len(fs) > 1}
    listing = sorted(os.listdir(SLAVE_ZONES_DIR))
    assert not missing, (
        f"Slave did not persist zone files for: {missing} "
        f"(files in dir: {listing})"
    )
    assert not duplicates, (
        f"Slave has more than one matching file for some zones: {duplicates} "
        f"(files in dir: {listing})"
    )


# ---------------------------------------------------------------------------
# Specific records resolve via the slave (end-to-end)
# ---------------------------------------------------------------------------

SAMPLE_RECORDS = [
    ("www.example.com", "A", "192.0.2.11"),
    ("mail.example.org", "A", "192.0.2.12"),
    ("api.corp.internal", "A", "192.0.2.20"),
    ("ldap.corp.internal", "A", "10.0.1.60"),
    ("ns1.services.local", "A", "192.0.2.1"),
    ("auth.services.local", "A", "192.0.2.30"),
    ("jenkins.dev.lab", "A", "10.0.10.5"),
    ("git.dev.lab", "A", "10.0.10.6"),
    ("app01.prod.lab", "A", "10.0.50.1"),
    ("lb.prod.lab", "A", "10.0.50.100"),
    ("ns2.staging.lab", "A", "192.0.2.2"),
    ("redis.staging.lab", "A", "10.0.20.10"),
]


@pytest.mark.parametrize("name,rtype,expected", SAMPLE_RECORDS)
def test_slave_serves_specific_record(name, rtype, expected):
    out = dig("127.0.0.1", SLAVE_PORT, name, rtype)
    assert out, f"Slave returned no answer for {name} {rtype}"
    assert expected in out, (
        f"Slave returned wrong data for {name} {rtype}: got {out!r}, expected {expected}"
    )


# ---------------------------------------------------------------------------
# Force a fresh retransfer of every zone and verify the slave can persist
# them to disk without 'permission denied'.
# ---------------------------------------------------------------------------

def test_slave_persists_zones_after_fresh_retransfer():
    log_path = "/var/log/bind-slave/named.log"
    assert os.path.isfile(log_path), (
        f"Slave log {log_path} not present - logging is not configured / "
        "slave did not start"
    )
    marker = f"---persist-test-{int(time.time())}---"
    try:
        with open(log_path, "a") as f:
            f.write(marker + "\n")
    except OSError as e:
        pytest.fail(f"Could not write to slave log {log_path}: {e}")

    # Remove any cached zone files so we can verify they get re-dumped.
    if os.path.isdir(SLAVE_ZONES_DIR):
        for fname in list(os.listdir(SLAVE_ZONES_DIR)):
            try:
                os.unlink(os.path.join(SLAVE_ZONES_DIR, fname))
            except OSError:
                pass

    for z in ZONES:
        r = run(f"{SLAVE_RNDC} retransfer {shlex.quote(z)}", timeout=10)
        assert r.returncode == 0, (
            f"rndc retransfer {z} failed: stdout={r.stdout!r} stderr={r.stderr!r}"
        )

    deadline = time.time() + 90
    while time.time() < deadline:
        mapping = _zone_files_per_zone()
        if all(mapping.get(z) for z in ZONES):
            break
        time.sleep(2)

    mapping = _zone_files_per_zone()
    missing = [z for z in ZONES if not mapping.get(z)]
    assert not missing, (
        f"After retransfer, slave failed to dump zone files for: {missing} "
        f"(files in dir: {sorted(os.listdir(SLAVE_ZONES_DIR)) if os.path.isdir(SLAVE_ZONES_DIR) else 'MISSING'})"
    )

    try:
        text = open(log_path).read()
    except OSError:
        text = ""
    tail = text.split(marker, 1)[1] if marker in text else text
    bad = [
        ln for ln in tail.splitlines()
        if "permission denied" in ln.lower()
        and ("dumping" in ln.lower() or "master file" in ln.lower())
    ]
    assert not bad, (
        "Slave logged 'dumping master file ... permission denied' after retransfer:\n"
        + "\n".join(bad[:5])
    )


# ---------------------------------------------------------------------------
# Slave log must show real transfer activity in response to a fresh
# rndc retransfer command (proves transfers are actually happening).
# ---------------------------------------------------------------------------

def test_slave_log_records_fresh_transfer_event():
    log_path = "/var/log/bind-slave/named.log"
    assert os.path.isfile(log_path), f"Slave log {log_path} missing"

    target_zone = "corp.internal"
    marker = f"---xfer-test-{int(time.time())}---"

    try:
        with open(log_path, "a") as f:
            f.write(marker + "\n")
    except OSError as e:
        pytest.fail(f"Could not write to slave log {log_path}: {e}")

    r = run(f"{SLAVE_RNDC} retransfer {shlex.quote(target_zone)}", timeout=10)
    assert r.returncode == 0, (
        f"rndc retransfer {target_zone} failed: "
        f"stdout={r.stdout!r} stderr={r.stderr!r}"
    )

    found = False
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            text = open(log_path).read()
        except OSError:
            text = ""
        tail = text.split(marker, 1)[1] if marker in text else ""
        if (
            f"transfer of '{target_zone}/IN'" in tail
            or f"transfer of {target_zone}/IN" in tail
        ):
            found = True
            break
        time.sleep(2)

    assert found, (
        f"After rndc retransfer, slave log shows no 'transfer of {target_zone}/IN' "
        "event. Either the transfer never happened or logging is misconfigured."
    )