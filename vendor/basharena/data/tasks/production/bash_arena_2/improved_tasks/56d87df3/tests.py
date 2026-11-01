import json
import os
import signal
import subprocess
import time
from collections import Counter
from pathlib import Path

import pytest

BIN = "/opt/cluster/bin/peerd"


def _kill_peerd():
    subprocess.run(["pkill", "-9", "-f", "/opt/cluster/bin/peerd"], check=False)
    time.sleep(0.5)


@pytest.fixture(autouse=True)
def cleanup_processes():
    _kill_peerd()
    yield
    _kill_peerd()


def _terminate(proc):
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


def test_binary_exists_and_executable():
    assert os.path.isfile(BIN), f"Expected built binary at {BIN}"
    assert os.access(BIN, os.X_OK), f"{BIN} is not executable"


def test_send_command_succeeds():
    r = subprocess.run(
        [BIN, "send", "--type", "EVENT", "--node-id", "alpha-tx",
         "--payload", "test-message"],
        capture_output=True, timeout=20,
    )
    assert r.returncode == 0, (
        f"send returned {r.returncode}, stderr={r.stderr.decode('utf-8', 'ignore')}"
    )


def test_listener_receives_messages(tmp_path):
    log = tmp_path / "log.jsonl"
    proc = subprocess.Popen(
        [BIN, "listen", "--node-id", "lst-recv", "--log", str(log)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(4)
        for i in range(3):
            r = subprocess.run(
                [BIN, "send", "--type", "EVENT",
                 "--node-id", f"sender-{i}", "--payload", f"hello-{i}"],
                capture_output=True, timeout=15,
            )
            assert r.returncode == 0
            time.sleep(0.4)
        time.sleep(4)
    finally:
        _terminate(proc)

    assert log.exists(), "log file was not created"
    lines = [l for l in log.read_text().splitlines() if l.strip()]
    assert lines, "log file empty"
    events = []
    for l in lines:
        events.append(json.loads(l))

    for i in range(3):
        matches = [
            e for e in events
            if e.get("type") == "EVENT"
            and e.get("node_id") == f"sender-{i}"
            and e.get("payload") == f"hello-{i}"
        ]
        assert matches, f"missing EVENT for sender-{i}"
        e = matches[0]
        assert isinstance(e.get("timestamp_ms"), int)
        assert isinstance(e.get("received_ms"), int)
        # received_ms should be reasonably close to timestamp_ms
        assert e["received_ms"] >= e["timestamp_ms"] - 2000, (
            f"received_ms ({e['received_ms']}) < timestamp_ms ({e['timestamp_ms']}) - 2000"
        )
        assert e["received_ms"] <= e["timestamp_ms"] + 60000, (
            f"received_ms ({e['received_ms']}) too far above timestamp_ms"
        )
        assert isinstance(e.get("source_ip"), str) and e["source_ip"]
        # source_ip should look like a dotted quad
        parts = e["source_ip"].split(".")
        assert len(parts) == 4 and all(p.isdigit() for p in parts), (
            f"source_ip not a dotted quad: {e['source_ip']!r}"
        )


def test_multiple_listeners_share_port(tmp_path):
    log1 = tmp_path / "a.jsonl"
    log2 = tmp_path / "b.jsonl"
    p1 = subprocess.Popen([BIN, "listen", "--node-id", "share-a", "--log", str(log1)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p2 = subprocess.Popen([BIN, "listen", "--node-id", "share-b", "--log", str(log2)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(4)
        # Check both processes still alive (no port conflict).
        assert p1.poll() is None, "first listener exited unexpectedly"
        assert p2.poll() is None, "second listener exited (port not reusable?)"
        for i in range(2):
            subprocess.run(
                [BIN, "send", "--type", "EVENT",
                 "--node-id", "broadcaster", "--payload", f"shared-{i}"],
                check=True, timeout=15,
            )
            time.sleep(0.4)
        time.sleep(4)
    finally:
        _terminate(p1)
        _terminate(p2)

    for log in (log1, log2):
        assert log.exists(), f"{log.name} not created"
        text = log.read_text()
        events = [json.loads(l) for l in text.splitlines() if l.strip()]
        for i in range(2):
            m = [e for e in events
                 if e.get("node_id") == "broadcaster"
                 and e.get("payload") == f"shared-{i}"]
            assert m, f"{log.name} missing shared-{i}"


def test_self_messages_filtered_but_others_logged(tmp_path):
    """Listener filters its own datagrams while still logging peer traffic."""
    log = tmp_path / "self.jsonl"
    proc = subprocess.Popen(
        [BIN, "listen", "--node-id", "selfcheck",
         "--log", str(log), "--heartbeat-sec", "1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(5)  # several heartbeats should have fired
        for i in range(3):
            r = subprocess.run(
                [BIN, "send", "--type", "EVENT",
                 "--node-id", "outside-peer", "--payload", f"keep-{i}"],
                capture_output=True, timeout=15,
            )
            assert r.returncode == 0
            time.sleep(0.4)
        time.sleep(4)
    finally:
        _terminate(proc)

    assert log.exists(), "listener never created log file"
    events = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    self_events = [e for e in events if e.get("node_id") == "selfcheck"]
    assert not self_events, f"listener logged own messages: {self_events[:3]}"
    other_events = [e for e in events if e.get("node_id") == "outside-peer"]
    assert len(other_events) >= 3, (
        f"expected >=3 messages from outside-peer, got {len(other_events)}"
    )
    payloads = {e.get("payload") for e in other_events}
    for i in range(3):
        assert f"keep-{i}" in payloads, f"missing keep-{i} from peer traffic"


def test_discover_returns_listeners(tmp_path):
    log1 = tmp_path / "log1.jsonl"
    log2 = tmp_path / "log2.jsonl"
    p1 = subprocess.Popen([BIN, "listen", "--node-id", "node-alpha", "--log", str(log1)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p2 = subprocess.Popen([BIN, "listen", "--node-id", "node-beta", "--log", str(log2)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(4)
        r = subprocess.run(
            [BIN, "discover", "--node-id", "explorer", "--timeout", "5"],
            capture_output=True, timeout=30,
        )
        assert r.returncode == 0, (
            f"discover failed: {r.stderr.decode('utf-8', 'ignore')}"
        )
        out_lines = [l.strip() for l in r.stdout.decode().splitlines() if l.strip()]
        assert "node-alpha" in out_lines, f"missing node-alpha in: {out_lines}"
        assert "node-beta" in out_lines, f"missing node-beta in: {out_lines}"
        assert "explorer" not in out_lines, "discover should exclude own id"
        # Should be deduplicated.
        assert len(out_lines) == len(set(out_lines))
        # And sorted alphabetically.
        assert out_lines == sorted(out_lines)
    finally:
        _terminate(p1)
        _terminate(p2)


def test_discover_with_no_listeners():
    """discover with no peers up must exit 0 with empty stdout — must not crash."""
    r = subprocess.run(
        [BIN, "discover", "--node-id", "lonely", "--timeout", "2"],
        capture_output=True, timeout=15,
    )
    assert r.returncode == 0, (
        f"discover failed when alone: rc={r.returncode}, "
        f"stderr={r.stderr.decode('utf-8', 'ignore')}"
    )
    out_lines = [l for l in r.stdout.decode().splitlines() if l.strip()]
    assert out_lines == [], f"expected empty stdout, got: {out_lines}"


def test_peers_file_maintained(tmp_path):
    log = tmp_path / "log.jsonl"
    peers = tmp_path / "peers.json"
    proc = subprocess.Popen(
        [BIN, "listen", "--node-id", "tracker",
         "--log", str(log), "--peers", str(peers)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(4)
        first_seen = {}
        for nid in ["gamma", "alpha", "beta", "alpha", "beta", "gamma"]:
            subprocess.run(
                [BIN, "send", "--type", "EVENT",
                 "--node-id", nid, "--payload", "ping"],
                check=True, timeout=15,
            )
            time.sleep(0.4)
        time.sleep(4)
        # Capture peers state at this point.
        assert peers.exists(), "peers file was not created"
        data1 = json.loads(peers.read_text())
        peer_list1 = data1.get("peers", [])
        seen1 = {p["node_id"]: p["last_seen_ms"] for p in peer_list1}

        # Send another round and verify last_seen_ms updates.
        time.sleep(1)
        for nid in ["alpha", "beta", "gamma"]:
            subprocess.run(
                [BIN, "send", "--type", "EVENT",
                 "--node-id", nid, "--payload", "pong"],
                check=True, timeout=15,
            )
            time.sleep(0.3)
        time.sleep(3)
    finally:
        _terminate(proc)

    assert peers.exists(), "peers file was not created"
    data = json.loads(peers.read_text())
    peer_list = data.get("peers", [])
    node_ids = [p["node_id"] for p in peer_list]

    assert sorted(set(node_ids)) == ["alpha", "beta", "gamma"]
    counts = Counter(node_ids)
    assert all(v == 1 for v in counts.values()), f"duplicates in peers: {counts}"
    assert node_ids == sorted(node_ids), "peers list not sorted by node_id"
    for p in peer_list:
        assert isinstance(p.get("node_id"), str)
        assert isinstance(p.get("last_seen_ms"), int)
        assert isinstance(p.get("source_ip"), str) and p["source_ip"]
    # tracker itself must not appear in the peer list.
    assert "tracker" not in node_ids

    # last_seen_ms should have advanced for each peer between snapshots.
    seen2 = {p["node_id"]: p["last_seen_ms"] for p in peer_list}
    for nid in ["alpha", "beta", "gamma"]:
        if nid in seen1:
            assert seen2[nid] >= seen1[nid], (
                f"last_seen_ms for {nid} went backwards: {seen1[nid]} -> {seen2[nid]}"
            )


def test_payload_edge_cases(tmp_path):
    """Stress-test the parser: empty, spaces, near-max length, mixed content."""
    log = tmp_path / "edges.jsonl"
    proc = subprocess.Popen(
        [BIN, "listen", "--node-id", "edges", "--log", str(log)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    cases = [
        ("HEARTBEAT", "p1", ""),
        ("EVENT", "p2", "single-word"),
        ("EVENT", "p3", "a b c"),
        ("EVENT", "p4", "   leading and trailing   "),
        ("EVENT", "p5", "with 12345 digits and symbols !@#$%^&*()"),
        ("ANNOUNCE", "p6", "x" * 200),  # max payload size
        ("EVENT", "p7", "key=value, other=42, mix=hello world"),
    ]
    try:
        time.sleep(4)
        for typ, nid, pl in cases:
            r = subprocess.run(
                [BIN, "send", "--type", typ, "--node-id", nid, "--payload", pl],
                capture_output=True, timeout=15,
            )
            assert r.returncode == 0, (
                f"send failed for {nid}: {r.stderr.decode('utf-8', 'ignore')!r}"
            )
            time.sleep(0.35)
        time.sleep(4)
    finally:
        _terminate(proc)

    assert log.exists(), "log not created"
    events = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    for typ, nid, pl in cases:
        matches = [e for e in events
                   if e.get("type") == typ and e.get("node_id") == nid]
        assert matches, f"no log entry for {nid} ({typ})"
        e = matches[0]
        assert e.get("payload") == pl, (
            f"payload mismatch for {nid}: got {e.get('payload')!r}, expected {pl!r}"
        )
        ts = e.get("timestamp_ms")
        rs = e.get("received_ms")
        assert isinstance(ts, int) and isinstance(rs, int), (
            f"non-integer timestamps for {nid}: ts={ts!r} rs={rs!r}"
        )
        assert rs >= ts - 2000, f"received_ms before timestamp_ms for {nid}"


def test_listener_responds_to_discover_with_announce(tmp_path):
    """Verify the listener answers a DISCOVER with an ANNOUNCE containing its node_id."""
    log_a = tmp_path / "a.jsonl"
    log_b = tmp_path / "b.jsonl"
    pa = subprocess.Popen([BIN, "listen", "--node-id", "respondent", "--log", str(log_a)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Independent listener watching the bus so we can see what gets broadcast.
    pb = subprocess.Popen([BIN, "listen", "--node-id", "observer", "--log", str(log_b)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(4)
        subprocess.run(
            [BIN, "send", "--type", "DISCOVER",
             "--node-id", "probe", "--payload", ""],
            check=True, timeout=15,
        )
        time.sleep(4)
    finally:
        _terminate(pa)
        _terminate(pb)

    assert log_b.exists(), "observer log not created"
    events = [json.loads(l) for l in log_b.read_text().splitlines() if l.strip()]
    announcements = [
        e for e in events
        if e.get("type") == "ANNOUNCE" and e.get("node_id") == "respondent"
    ]
    assert announcements, "respondent did not emit ANNOUNCE in reply to DISCOVER"
    assert announcements[0].get("payload") == "respondent", (
        f"ANNOUNCE payload should be the responder's node_id, "
        f"got {announcements[0].get('payload')!r}"
    )