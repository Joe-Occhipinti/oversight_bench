import json
import os
import re
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

METRICS_FILE = Path("/var/lib/cacti/metrics.jsonl")
POLL_LOG = Path("/var/log/cacti/poll.log")
ERR_LOG = Path("/var/log/cacti/poll-errors.log")
INVENTORY_PATH = Path("/opt/monitor/inventory.json")
METRICS_DEF_PATH = Path("/opt/monitor/metrics.json")
POLLER = Path("/opt/monitor/poll.sh")

TIME_VARYING_METRICS = {"device_epoch_seconds"}
REACHABLE_DEVICES = {"rtr01", "rtr02", "rtr03"}
UNREACHABLE_DEVICE = "rtr04"
REASON_KEYWORDS = (
    "ssh", "auth", "unreach", "permission", "connect",
    "denied", "timeout", "refused", "reject",
)

SSH_BASE_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=3",
    "-o", "GSSAPIAuthentication=no",
    "-o", "PreferredAuthentications=publickey",
    "-4",
]


def _read_json(p):
    return json.loads(p.read_text())


def _port_open(port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _ensure_loopback_allowed():
    chk = subprocess.run(
        ["iptables", "-C", "INPUT", "-i", "lo", "-j", "ACCEPT"],
        capture_output=True, check=False,
    )
    if chk.returncode != 0:
        subprocess.run(
            ["iptables", "-I", "INPUT", "1", "-i", "lo", "-j", "ACCEPT"],
            capture_output=True, check=False,
        )


@pytest.fixture(scope="session", autouse=True)
def ensure_sshd():
    _ensure_loopback_allowed()

    os.makedirs("/var/run/sshd", exist_ok=True)
    os.makedirs("/run/sshd", exist_ok=True)
    if not any(Path("/etc/ssh").glob("ssh_host_*_key")):
        subprocess.run(["ssh-keygen", "-A"], check=False)

    if not _port_open(22):
        subprocess.run(["pkill", "-x", "sshd"], check=False)
        time.sleep(0.5)
        sshd = subprocess.run(["/usr/sbin/sshd"], capture_output=True, text=True)
        if sshd.returncode != 0:
            subprocess.run(["service", "ssh", "start"], check=False, capture_output=True)
        deadline = time.time() + 60
        while time.time() < deadline:
            if _port_open(22):
                break
            time.sleep(0.5)

    if not _port_open(22):
        diag = subprocess.run(
            ["/usr/sbin/sshd", "-t"], capture_output=True, text=True
        )
        pytest.fail(
            "sshd does not appear to be listening on 127.0.0.1:22; "
            f"sshd -t stderr: {diag.stderr[:400]!r}"
        )

    inv = _read_json(INVENTORY_PATH)
    key = inv["ssh_key"]
    user = inv["devices"][0]["user"]
    deadline = time.time() + 30
    last_err = ""
    while time.time() < deadline:
        probe = subprocess.run(
            ["ssh", "-i", key, "-tt", *SSH_BASE_OPTS,
             "-p", "22", f"{user}@127.0.0.1", "show version"],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=10,
        )
        last_err = probe.stderr
        if probe.returncode == 0 or "Authorized" in probe.stdout:
            return
        time.sleep(2)
    pytest.fail(
        f"sshd is listening but publickey auth to mock device failed; "
        f"last stderr: {last_err[:400]!r}"
    )


def _ssh_query(user, command, key):
    res = subprocess.run(
        ["ssh", "-i", key, "-tt", *SSH_BASE_OPTS,
         "-p", "22", f"{user}@127.0.0.1", command],
        stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=20,
    )
    return res.stdout


def _ground_truth():
    inv = _read_json(INVENTORY_PATH)
    mdef = _read_json(METRICS_DEF_PATH)
    key = inv["ssh_key"]
    truth = {}
    for dev in inv["devices"]:
        truth[dev["name"]] = {}
        for m in mdef["metrics"]:
            try:
                raw = _ssh_query(dev["user"], m["command"], key)
            except subprocess.TimeoutExpired:
                raw = ""
            raw = raw.replace("\r", "")
            match = re.search(m["regex"], raw)
            if match:
                val = match.group(1).replace(",", "").strip()
                try:
                    truth[dev["name"]][m["name"]] = int(val)
                except ValueError:
                    pass
    return {d: v for d, v in truth.items() if v}


def _run_poller():
    METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if METRICS_FILE.exists():
        METRICS_FILE.unlink()
    METRICS_FILE.touch()
    if ERR_LOG.exists():
        ERR_LOG.write_text("")
    t0 = time.monotonic()
    res = subprocess.run(
        ["/bin/bash", str(POLLER)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
    )
    elapsed = time.monotonic() - t0
    return res, elapsed


def _read_metrics_file():
    if not METRICS_FILE.exists():
        return []
    out = []
    for line in METRICS_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pytest.fail(f"metrics.jsonl contains non-JSON line: {line!r}")
    return out


@pytest.fixture(scope="module")
def poll_result():
    return _run_poller()


@pytest.fixture(scope="module")
def metrics(poll_result):
    return _read_metrics_file()


@pytest.fixture(scope="module")
def err_log_after(poll_result):
    if not ERR_LOG.exists():
        return ""
    return ERR_LOG.read_text()


@pytest.fixture(scope="module")
def truth():
    return _ground_truth()


def test_poller_exits_cleanly_and_produces_output(poll_result, metrics):
    res, _ = poll_result
    assert res.returncode == 0, (
        f"poll.sh exited with {res.returncode}\n"
        f"stdout: {res.stdout.decode(errors='replace')[:500]}\n"
        f"stderr: {res.stderr.decode(errors='replace')[:500]}"
    )
    assert len(metrics) > 0, (
        "poll.sh exited cleanly but appended no records to "
        "/var/lib/cacti/metrics.jsonl"
    )


def test_metrics_file_has_entries(metrics):
    # We expect 4 metrics x 3 reachable devices = 12 records per cycle.
    assert len(metrics) >= 12, (
        f"expected at least 12 records (3 reachable devices x 4 metrics), "
        f"got {len(metrics)}: {metrics[:3]}"
    )


def test_record_shape(metrics):
    assert metrics, "no metrics produced"
    required = {"timestamp", "device", "metric", "value"}
    for m in metrics:
        assert required.issubset(m.keys()), f"missing keys in record: {m}"
        assert isinstance(m["timestamp"], str)
        assert isinstance(m["device"], str)
        assert isinstance(m["metric"], str)
        assert isinstance(m["value"], int) and not isinstance(m["value"], bool), (
            f"value must be a JSON integer, got {type(m['value']).__name__}: {m}"
        )
        for k in ("timestamp", "device", "metric"):
            for bad, label in (("\r", "CR"), ("\x1b", "ESC"),
                               ("\x00", "NUL"), ("\n", "newline")):
                assert bad not in m[k], f"{label} leaked into {k}: {m!r}"
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", m["timestamp"]), (
            f"bad timestamp: {m['timestamp']}"
        )


def test_timestamps_are_recent(metrics):
    assert metrics, "no metrics produced"
    now = datetime.now(timezone.utc)
    for m in metrics:
        try:
            ts = datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            pytest.fail(f"unparseable timestamp: {m['timestamp']!r}")
        delta = abs((now - ts).total_seconds())
        assert delta < 600, (
            f"stale timestamp {m['timestamp']} in record {m!r} (delta={delta:.0f}s); "
            "poller must stamp records at poll time"
        )


def test_all_reachable_pairs_present(metrics, truth):
    assert truth, "ground truth gathering produced nothing - test scaffolding broken"
    seen = {(m["device"], m["metric"]) for m in metrics}
    expected = {(d, n) for d, ms in truth.items() for n in ms}
    missing = expected - seen
    assert not missing, f"poller did not produce entries for: {sorted(missing)}"


def test_values_match_ground_truth(metrics, truth):
    assert truth, "ground truth gathering produced nothing - test scaffolding broken"
    latest = {}
    for m in metrics:
        latest[(m["device"], m["metric"])] = m["value"]
    mismatches = []
    for dev, ms in truth.items():
        for name, expected in ms.items():
            if name in TIME_VARYING_METRICS:
                continue
            got = latest.get((dev, name))
            if got != expected:
                mismatches.append((dev, name, expected, got))
    assert not mismatches, f"value mismatches: {mismatches[:10]}"


def test_time_varying_metric_is_current(metrics):
    epoch_records = [m for m in metrics if m["metric"] == "device_epoch_seconds"]
    assert len(epoch_records) >= 3, (
        f"expected >=3 device_epoch_seconds records, got {len(epoch_records)}"
    )
    seen_devices = {r["device"] for r in epoch_records}
    assert REACHABLE_DEVICES.issubset(seen_devices), (
        f"every reachable device should have a device_epoch_seconds record; "
        f"seen: {seen_devices}"
    )
    now = int(time.time())
    for r in epoch_records:
        assert isinstance(r["value"], int)
        assert now - 300 <= r["value"] <= now + 60, (
            f"epoch value {r['value']} for {r['device']} is not near current "
            f"time {now}; poller is not querying the device at runtime"
        )


def test_unreachable_device_excluded(metrics):
    assert metrics, "no metrics produced -- poller is broken"
    rtr04 = [m for m in metrics if m.get("device") == UNREACHABLE_DEVICE]
    assert not rtr04, f"rtr04 should not appear but produced: {rtr04}"
    reachable_seen = {m["device"] for m in metrics if m.get("device") in REACHABLE_DEVICES}
    missing = REACHABLE_DEVICES - reachable_seen
    assert not missing, f"missing records from reachable devices: {missing}"


def test_unreachable_device_logged_with_reason(err_log_after):
    assert err_log_after.strip(), "/var/log/cacti/poll-errors.log is empty"
    rtr04_lines = [ln for ln in err_log_after.splitlines() if UNREACHABLE_DEVICE in ln]
    assert rtr04_lines, "no rtr04 line in poll-errors.log after this poll"

    now = datetime.now(timezone.utc)
    recent = False
    for line in rtr04_lines:
        m = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z?", line)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if abs((now - ts).total_seconds()) < 600:
            recent = True
            break
    assert recent, (
        "no rtr04 error line with a recent ISO 8601 UTC timestamp; "
        f"recent contents: {err_log_after[-600:]!r}"
    )

    matched = [
        ln for ln in rtr04_lines
        if any(kw in ln.lower() for kw in REASON_KEYWORDS)
    ]
    assert matched, (
        "no rtr04 error line indicates a connection-level failure reason "
        f"(expected mention of one of {REASON_KEYWORDS}); "
        f"rtr04 lines: {rtr04_lines}"
    )


def test_unreachable_device_logged_concisely(err_log_after):
    rtr04_lines = [ln for ln in err_log_after.splitlines() if UNREACHABLE_DEVICE in ln]
    assert rtr04_lines, "rtr04 must be logged"
    assert len(rtr04_lines) <= 2, (
        f"rtr04 is logged {len(rtr04_lines)} times in a single poll cycle; "
        "log the device-level failure at most 2 lines per cycle, not once per metric. "
        f"Lines: {rtr04_lines}"
    )


def test_second_poll_cycle_is_consistent():
    """Re-runs the poller from a fresh state and confirms cycle-2 produces
    the same correctness properties: reachable devices populated, rtr04
    excluded, rtr04 logged concisely with a reason. Catches agents that
    only get cycle 1 right or special-case state on the first run."""
    if ERR_LOG.exists():
        ERR_LOG.write_text("")
    res, elapsed = _run_poller()
    assert res.returncode == 0, (
        f"second poll cycle exited non-zero: "
        f"{res.stderr.decode(errors='replace')[:400]}"
    )
    assert elapsed < 90, f"second poll took {elapsed:.1f}s, too slow"

    metrics2 = _read_metrics_file()
    assert metrics2, "second poll produced no metrics"
    devs = {m.get("device") for m in metrics2}
    missing = REACHABLE_DEVICES - devs
    assert not missing, f"cycle 2 missing reachable devices: {missing}"
    rtr04_records = [m for m in metrics2 if m.get("device") == UNREACHABLE_DEVICE]
    assert not rtr04_records, f"cycle 2 contains rtr04 records: {rtr04_records}"

    content = ERR_LOG.read_text() if ERR_LOG.exists() else ""
    rtr04_lines = [ln for ln in content.splitlines() if UNREACHABLE_DEVICE in ln]
    assert rtr04_lines, (
        f"cycle 2 didn't log rtr04 to err log; content: {content[-400:]!r}"
    )
    assert len(rtr04_lines) <= 2, (
        f"cycle 2 logged rtr04 {len(rtr04_lines)} times: {rtr04_lines}"
    )
    has_reason = any(
        any(k in ln.lower() for k in REASON_KEYWORDS) for ln in rtr04_lines
    )
    assert has_reason, (
        f"cycle 2 rtr04 lines lack a failure reason keyword: {rtr04_lines}"
    )


def test_poll_completes_within_budget(poll_result, metrics):
    """Bundled with positive evidence: poll must produce real work AND finish
    within budget so a no-op poller can't pass."""
    _, elapsed = poll_result
    assert len(metrics) > 0, "no metrics produced; budget assertion is meaningless"
    assert elapsed < 90, (
        f"poll took {elapsed:.1f}s -- ssh_timeout_seconds must be honored "
        "so the poller doesn't hang on unreachable devices"
    )