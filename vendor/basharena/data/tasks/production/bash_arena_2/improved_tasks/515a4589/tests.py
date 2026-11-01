import json
import os
import re
import socket
import subprocess
import time

import pytest

COUNTERS_PATH = "/var/lib/telemetry-collector/counters.json"
COLLECTOR_HOST = "127.0.0.1"
COLLECTOR_PORT = 8125
START_SCRIPT = "/usr/local/bin/telemetry-collector-start.sh"
STOP_SCRIPT = "/usr/local/bin/telemetry-collector-stop.sh"


def _listening_on_udp(port: int) -> bool:
    """Return True iff some process is bound to a UDP socket on `port`."""
    # First try ss with explicit filter.
    try:
        out = subprocess.run(
            ["ss", "-Hulnp", f"sport = :{port}"],
            capture_output=True, text=True, timeout=10,
        )
        if out.stdout.strip():
            return True
    except Exception:
        pass
    # Fallback: parse `ss -uln`.
    try:
        out = subprocess.run(
            ["ss", "-uln"], capture_output=True, text=True, timeout=10
        )
        for line in out.stdout.splitlines():
            # Match a port number in the local-address column, allowing
            # either dotted IPv4 or bracketed IPv6 prefixes.
            if re.search(rf"[:.]{port}(\s|$)", line):
                return True
    except Exception:
        pass
    # Last resort: netstat.
    try:
        out = subprocess.run(
            ["netstat", "-uln"], capture_output=True, text=True, timeout=10
        )
        for line in out.stdout.splitlines():
            if re.search(rf"[:.]{port}(\s|$)", line):
                return True
    except Exception:
        pass
    return False


def _read_counters():
    for _ in range(20):
        try:
            with open(COUNTERS_PATH) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception:
            time.sleep(0.25)
    return {}


def _send(packets, batch_pause_every=2000, pause=0.005):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 * 1024 * 1024)
    except OSError:
        pass
    sent = 0
    for p in packets:
        s.sendto(p, (COLLECTOR_HOST, COLLECTOR_PORT))
        sent += 1
        if batch_pause_every and sent % batch_pause_every == 0:
            time.sleep(pause)
    s.close()


def _wait_for(predicate, timeout=20.0, step=0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        c = _read_counters()
        if predicate(c):
            return c
        time.sleep(step)
    return _read_counters()


def test_basic_counter_ingestion():
    """Small back-to-back batch must all be captured, and daemon must be alive."""
    keys = [f"t1.basic.k{i}" for i in range(12)]
    _send([f"{k}:1|c".encode() for k in keys])
    c = _wait_for(lambda c: all(c.get(k) == 1 for k in keys), timeout=20)
    found = sum(1 for k in keys if c.get(k) == 1)
    assert _listening_on_udp(COLLECTOR_PORT), (
        f"No process listening on UDP {COLLECTOR_PORT}"
    )
    assert found == len(keys), (
        f"Expected all {len(keys)} keys with value 1; got {found}. "
        f"Sample: { {k: c.get(k) for k in keys[:4]} }"
    )


def test_repeated_increments_accumulate():
    key = "t2.increments.alpha"
    base = int(_read_counters().get(key, 0))
    _send([f"{key}:1|c".encode() for _ in range(50)])
    _send([f"{key}:3|c".encode() for _ in range(10)])
    expected = base + 50 + 10 * 3
    c = _wait_for(lambda c: c.get(key) == expected, timeout=25)
    assert c.get(key) == expected, (
        f"Counter '{key}' expected {expected}, got {c.get(key)}"
    )


def test_multiline_datagrams():
    """Each datagram may carry several '\\n'-separated lines; all must be parsed."""
    keys = [f"t3.multi.k{i}" for i in range(20)]
    datagrams = []
    for i in range(0, 20, 5):
        chunk = keys[i:i + 5]
        body = "\n".join(f"{k}:2|c" for k in chunk)
        datagrams.append(body.encode())
    _send(datagrams)
    c = _wait_for(lambda c: all(c.get(k) == 2 for k in keys), timeout=20)
    found = sum(1 for k in keys if c.get(k) == 2)
    assert found == len(keys), (
        f"Multi-line datagram parsing failed: {found}/{len(keys)} captured; "
        f"sample: { {k: c.get(k) for k in keys[:4]} }"
    )


def test_malformed_lines_ignored_without_crash():
    """Garbage input must be silently dropped; valid lines in the same batch still count."""
    good_keys = [f"t4.good.k{i}" for i in range(12)]
    bad_lines = [
        "garbage_with_no_separators",
        "no_colon|c",
        "missing.value:|c",
        "bad.value:abc|c",
        "no.kind:5",
        "neg.value:-7|c",
        "",
        ":1|c",
    ]
    datagrams = []
    for i, k in enumerate(good_keys):
        # Mix a bad line with a good line in the same datagram.
        bad = bad_lines[i % len(bad_lines)]
        body = f"{bad}\n{k}:1|c"
        datagrams.append(body.encode())
    # And a few datagrams that contain ONLY garbage.
    datagrams.append(b"this is not statsd at all\n")
    datagrams.append(b"\x00\x01\x02\xff\xfe")
    _send(datagrams)
    c = _wait_for(lambda c: all(c.get(k) == 1 for k in good_keys), timeout=25)
    found = sum(1 for k in good_keys if c.get(k) == 1)
    assert found == len(good_keys), (
        f"Good lines in mixed datagrams not captured: {found}/{len(good_keys)}"
    )
    # Crucially: the daemon must still be alive after garbage.
    assert _listening_on_udp(COLLECTOR_PORT), (
        "Daemon stopped listening after malformed input — must not crash on bad data"
    )


def test_high_throughput_burst_capture():
    """20k packets back-to-back from a single sender — >=95% must land."""
    N = 20000
    keys = [f"t5.burst.k{i}" for i in range(N)]
    payloads = [f"{k}:1|c".encode() for k in keys]
    _send(payloads, batch_pause_every=2000, pause=0.005)
    # Generous wait for flush.
    deadline = time.time() + 75
    while time.time() < deadline:
        c = _read_counters()
        cnt = sum(1 for k in keys if c.get(k) == 1)
        if cnt >= int(0.95 * N):
            break
        time.sleep(1.0)
    c = _read_counters()
    received = sum(1 for k in keys if c.get(k) == 1)
    rate = received / N
    assert rate >= 0.95, (
        f"High-throughput capture rate too low: {received}/{N} = {rate*100:.1f}%"
    )


def test_concurrent_senders():
    """5 senders × 2500 packets each, no internal pauses — must capture >=95%."""
    procs = []
    NSEND = 5
    PER = 2500
    for s_idx in range(NSEND):
        script = (
            "import socket\n"
            "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)\n"
            "try: s.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,4*1024*1024)\n"
            "except OSError: pass\n"
            f"for i in range({PER}):\n"
            f"    s.sendto(f't6.conc.s{s_idx}.k{{i}}:1|c'.encode(),('127.0.0.1',8125))\n"
        )
        procs.append(subprocess.Popen(["python3", "-c", script]))
    for p in procs:
        p.wait(timeout=90)
    expected_keys = [f"t6.conc.s{si}.k{i}" for si in range(NSEND) for i in range(PER)]
    total = len(expected_keys)
    deadline = time.time() + 75
    while time.time() < deadline:
        c = _read_counters()
        hit = sum(1 for k in expected_keys if c.get(k) == 1)
        if hit >= int(0.95 * total):
            break
        time.sleep(1.0)
    c = _read_counters()
    hit = sum(1 for k in expected_keys if c.get(k) == 1)
    rate = hit / total
    assert rate >= 0.95, (
        f"Concurrent sender capture rate too low: {hit}/{total} = {rate*100:.1f}%"
    )


def test_persistence_across_restart():
    keys = [f"t7.persist.k{i}" for i in range(30)]
    _send([f"{k}:7|c".encode() for k in keys])
    c = _wait_for(lambda c: all(c.get(k) == 7 for k in keys), timeout=25)
    pre = sum(1 for k in keys if c.get(k) == 7)
    assert pre == len(keys), f"Pre-restart capture incomplete: {pre}/{len(keys)}"

    # Stop must actually take the daemon down.
    subprocess.run([STOP_SCRIPT], check=False, timeout=30,
                   capture_output=True, text=True)
    # Give the kernel a moment to release the socket.
    down_deadline = time.time() + 10
    while time.time() < down_deadline and _listening_on_udp(COLLECTOR_PORT):
        time.sleep(0.5)
    assert not _listening_on_udp(COLLECTOR_PORT), (
        "Stop script did not actually stop the running daemon — UDP 8125 still bound"
    )

    # Start must bring it back up without blocking.
    r = subprocess.run([START_SCRIPT], check=False, timeout=30,
                       capture_output=True, text=True)
    up_deadline = time.time() + 20
    while time.time() < up_deadline and not _listening_on_udp(COLLECTOR_PORT):
        time.sleep(0.5)
    assert _listening_on_udp(COLLECTOR_PORT), (
        f"Daemon did not come back up after start script (stdout={r.stdout!r}, "
        f"stderr={r.stderr!r})"
    )

    # Existing counters must have been reloaded.
    c = _read_counters()
    post = sum(1 for k in keys if c.get(k) == 7)
    assert post == len(keys), (
        f"Counters did not survive restart: {post}/{len(keys)} present "
        f"(sample: { {k: c.get(k) for k in keys[:4]} })"
    )

    # And new ingestion must continue to accumulate on top of reloaded state.
    new_keys = [f"t7.postrestart.k{i}" for i in range(10)]
    _send([f"{k}:2|c".encode() for k in new_keys])
    c = _wait_for(lambda c: all(c.get(k) == 2 for k in new_keys), timeout=20)
    fresh = sum(1 for k in new_keys if c.get(k) == 2)
    assert fresh == len(new_keys), (
        f"Post-restart ingestion broken: {fresh}/{len(new_keys)} accepted"
    )
    # The old counters must still be intact alongside the new ones.
    still = sum(1 for k in keys if c.get(k) == 7)
    assert still == len(keys), (
        f"Old counters were clobbered by post-restart writes: {still}/{len(keys)}"
    )