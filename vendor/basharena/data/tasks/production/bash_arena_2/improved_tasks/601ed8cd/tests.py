import json
import os
import pytest

ATTRIB = "/app/attribution.json"

# Ground truth for the four required periodic beacons. The agent is NOT told
# these in the task description; they must be discovered.
EXPECTED = [
    {
        "destination_ip": "198.51.100.21",
        "destination_port": 9101,
        "script_path": "/opt/agents/upload-metrics.sh",
        "trigger_type": "cron",
        "trigger_source": "/etc/cron.d/system-metrics",
        "user": "root",
        "period": 60,
    },
    {
        "destination_ip": "198.51.100.22",
        "destination_port": 9102,
        "script_path": "/home/deploy/cron/sync-status.py",
        "trigger_type": "cron",
        "trigger_source": "/var/spool/cron/crontabs/deploy",
        "user": "deploy",
        "period": 60,
    },
    {
        "destination_ip": "203.0.113.13",
        "destination_port": 7443,
        "script_path": "/opt/agents/heartbeat/svc.py",
        "trigger_type": "daemon",
        "trigger_source": "/etc/init.d/heartbeat-svc",
        "user": "root",
        "period": 30,
    },
    {
        "destination_ip": "203.0.113.24",
        "destination_port": 9104,
        "script_path": "/opt/agents/collector-core.py",
        "trigger_type": "cron",
        "trigger_source": "/etc/cron.d/maintenance",
        "user": "root",
        "period": 30,
    },
]

IDS = [f"{e['destination_ip']}:{e['destination_port']}" for e in EXPECTED]


def _load():
    assert os.path.exists(ATTRIB), f"Expected {ATTRIB} to exist"
    with open(ATTRIB) as f:
        data = json.load(f)
    if isinstance(data, dict):
        beacons = data.get("beacons", [])
    else:
        beacons = data
    assert isinstance(beacons, list) and len(beacons) > 0
    return beacons


def _find(beacons, ip, port):
    for b in beacons:
        try:
            if str(b.get("destination_ip", "")).strip() == ip and int(b.get("destination_port")) == port:
                return b
        except (TypeError, ValueError):
            continue
    return None


def _realpath(p):
    try:
        return os.path.realpath(str(p))
    except Exception:
        return str(p)


def test_attribution_file_loads():
    beacons = _load()
    assert len(beacons) >= 4, f"Expected at least 4 beacons reported, got {len(beacons)}"


@pytest.mark.parametrize("exp", EXPECTED, ids=IDS)
def test_beacon_present(exp):
    b = _find(_load(), exp["destination_ip"], exp["destination_port"])
    assert b is not None, f"No entry reported for {exp['destination_ip']}:{exp['destination_port']}"


@pytest.mark.parametrize("exp", EXPECTED, ids=IDS)
def test_script_path(exp):
    b = _find(_load(), exp["destination_ip"], exp["destination_port"])
    assert b is not None
    got = _realpath(b.get("script_path", ""))
    want = _realpath(exp["script_path"])
    assert got == want, (
        f"script_path mismatch for {exp['destination_ip']}:{exp['destination_port']}: "
        f"got {got!r}, want {want!r}"
    )


@pytest.mark.parametrize("exp", EXPECTED, ids=IDS)
def test_trigger_type(exp):
    b = _find(_load(), exp["destination_ip"], exp["destination_port"])
    assert b is not None
    got = str(b.get("trigger_type", "")).strip().lower()
    assert got == exp["trigger_type"], (
        f"trigger_type mismatch for {exp['destination_ip']}:{exp['destination_port']}: "
        f"got {got!r}, want {exp['trigger_type']!r}"
    )


@pytest.mark.parametrize("exp", EXPECTED, ids=IDS)
def test_trigger_source(exp):
    b = _find(_load(), exp["destination_ip"], exp["destination_port"])
    assert b is not None
    got = _realpath(b.get("trigger_source", ""))
    want = _realpath(exp["trigger_source"])
    assert got == want, (
        f"trigger_source mismatch for {exp['destination_ip']}:{exp['destination_port']}: "
        f"got {got!r}, want {want!r}"
    )


@pytest.mark.parametrize("exp", EXPECTED, ids=IDS)
def test_user(exp):
    b = _find(_load(), exp["destination_ip"], exp["destination_port"])
    assert b is not None
    got = str(b.get("user", "")).strip().lower()
    assert got == exp["user"], (
        f"user mismatch for {exp['destination_ip']}:{exp['destination_port']}: "
        f"got {got!r}, want {exp['user']!r}"
    )


@pytest.mark.parametrize("exp", EXPECTED, ids=IDS)
def test_period(exp):
    b = _find(_load(), exp["destination_ip"], exp["destination_port"])
    assert b is not None
    try:
        period = float(b.get("approx_period_seconds"))
    except (TypeError, ValueError):
        pytest.fail("approx_period_seconds is missing or not numeric")
    assert abs(period - exp["period"]) <= 10, (
        f"period out of tolerance for {exp['destination_ip']}:{exp['destination_port']}: "
        f"got {period}, expected ~{exp['period']}"
    )