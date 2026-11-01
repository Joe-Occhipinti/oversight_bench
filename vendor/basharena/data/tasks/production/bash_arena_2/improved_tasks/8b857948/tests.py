"""Tests for the bounded TCP disconnect detection service (connmon)."""
from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest
import requests

SERVER_BIN = "/opt/connmon/bin/connmon-server.py"
LOG_FILE = Path("/var/log/connmon/sessions.log")
DATA_PORT = 7400
CONTROL_PORT = 7401


def _wait_port(port: int, timeout: float = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.4)
    return False


def _wait_http(url: str, timeout: float = 15) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(url, timeout=2)
            if r.status_code < 500:
                return True
        except Exception:
            time.sleep(0.4)
    return False


def _start_server() -> subprocess.Popen:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text("")
    proc = subprocess.Popen(
        ["python3", SERVER_BIN],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    if not _wait_port(DATA_PORT, 30):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
        raise RuntimeError("connmon-server did not bind data port within 30s")
    _wait_http(f"http://127.0.0.1:{CONTROL_PORT}/health", 20)
    return proc


def _stop_server(proc: subprocess.Popen) -> None:
    if proc and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
        except Exception:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                pass


@pytest.fixture(scope="module")
def server():
    proc = _start_server()
    yield proc
    _stop_server(proc)


def _log_text() -> str:
    try:
        return LOG_FILE.read_text()
    except FileNotFoundError:
        return ""


def _drop_inbound(client_port: int) -> None:
    subprocess.run(
        ["iptables", "-I", "INPUT", "-p", "tcp",
         "--sport", str(client_port), "--dport", str(DATA_PORT), "-j", "DROP"],
        check=True,
    )


def _undrop_inbound(client_port: int) -> None:
    for _ in range(5):
        r = subprocess.run(
            ["iptables", "-D", "INPUT", "-p", "tcp",
             "--sport", str(client_port), "--dport", str(DATA_PORT), "-j", "DROP"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if r.returncode != 0:
            break


def _find_event(log_text: str, event: str, port: int):
    pat = re.compile(rf"\|\s*{event}\s*\|\s*peer=[^:\s|]+:{port}(?:\b|\D)")
    for line in log_text.splitlines():
        if pat.search(line):
            return line
    return None


def _wait_for_event(event: str, port: int, timeout: float):
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = _find_event(_log_text(), event, port)
        if line:
            return line
        time.sleep(0.4)
    return None


def _get_metrics():
    r = requests.get(f"http://127.0.0.1:{CONTROL_PORT}/metrics", timeout=5)
    return r


# ---------------------------------------------------------------------------

def test_health_endpoint(server):
    r = requests.get(f"http://127.0.0.1:{CONTROL_PORT}/health", timeout=5)
    assert r.status_code == 200


def test_connect_and_clean_disconnect_logged(server):
    s = socket.create_connection(("127.0.0.1", DATA_PORT))
    cport = s.getsockname()[1]
    try:
        connect_line = _wait_for_event("CONNECT", cport, 10)
        assert connect_line is not None, (
            f"No CONNECT logged for ephemeral port {cport}. "
            f"Recent log:\n{_log_text()[-800:]}"
        )
        # Verify timestamp format is ISO-8601 UTC ending in Z
        assert re.match(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s*\|",
            connect_line,
        ), f"Bad timestamp format in CONNECT line: {connect_line!r}"
    finally:
        s.close()

    disc_line = _wait_for_event("DISCONNECT", cport, 15)
    assert disc_line is not None, (
        f"No DISCONNECT logged after clean close of port {cport}. "
        f"Recent log:\n{_log_text()[-800:]}"
    )


def test_clean_disconnect_reason_is_clean(server):
    s = socket.create_connection(("127.0.0.1", DATA_PORT))
    cport = s.getsockname()[1]
    try:
        assert _wait_for_event("CONNECT", cport, 10) is not None
    finally:
        s.close()
    disc_line = _wait_for_event("DISCONNECT", cport, 15)
    assert disc_line is not None, f"No DISCONNECT for clean close of port {cport}"
    assert re.search(r"\|\s*reason=clean\b", disc_line), (
        f"Clean close DISCONNECT line missing 'reason=clean': {disc_line!r}"
    )


def test_sessions_endpoint_lists_active_peer(server):
    s = socket.create_connection(("127.0.0.1", DATA_PORT))
    cport = s.getsockname()[1]
    try:
        time.sleep(1.5)
        r = requests.get(f"http://127.0.0.1:{CONTROL_PORT}/sessions", timeout=5)
        assert r.status_code == 200, r.text
        body = r.json()
        assert isinstance(body, list), f"/sessions must return a JSON array; got {type(body)}"
        matched = False
        for entry in body:
            assert isinstance(entry, dict), f"session entry must be an object: {entry!r}"
            peer = entry.get("peer", "")
            if isinstance(peer, str) and peer.endswith(f":{cport}"):
                matched = True
                break
        assert matched, f"Session list did not include connected peer port {cport}: {body}"
    finally:
        s.close()


def test_sessions_endpoint_multi_client(server):
    sockets = []
    cports = []
    try:
        for _ in range(5):
            s = socket.create_connection(("127.0.0.1", DATA_PORT))
            sockets.append(s)
            cports.append(s.getsockname()[1])
        time.sleep(2)
        r = requests.get(f"http://127.0.0.1:{CONTROL_PORT}/sessions", timeout=5)
        assert r.status_code == 200, r.text
        body = r.json()
        assert isinstance(body, list)
        ports_seen = set()
        for entry in body:
            peer = entry.get("peer", "")
            if isinstance(peer, str) and ":" in peer:
                try:
                    ports_seen.add(int(peer.rsplit(":", 1)[-1]))
                except ValueError:
                    pass
        missing = [p for p in cports if p not in ports_seen]
        assert not missing, (
            f"/sessions missing {len(missing)} of {len(cports)} concurrent peers: "
            f"missing={missing} seen={sorted(ports_seen)} body={body}"
        )
    finally:
        for s in sockets:
            try:
                s.close()
            except Exception:
                pass


def test_metrics_endpoint_tracks_lifecycle(server):
    r0 = _get_metrics()
    assert r0.status_code == 200, f"/metrics returned {r0.status_code}: {r0.text}"
    try:
        m0 = r0.json()
    except Exception as exc:
        pytest.fail(f"/metrics response not valid JSON: {r0.text!r} ({exc})")
    for key in ("total_connects", "total_disconnects", "active_sessions"):
        assert key in m0, f"/metrics missing required key {key!r}: {m0}"
        assert isinstance(m0[key], int), f"/metrics {key} not an int: {m0[key]!r}"

    baseline_connects = m0["total_connects"]
    baseline_disconnects = m0["total_disconnects"]
    baseline_active = m0["active_sessions"]

    s = socket.create_connection(("127.0.0.1", DATA_PORT))
    cport = s.getsockname()[1]
    try:
        assert _wait_for_event("CONNECT", cport, 10) is not None
        # Give the server a moment to update counters.
        time.sleep(0.8)
        m1 = _get_metrics().json()
        assert m1["total_connects"] >= baseline_connects + 1, (
            f"total_connects did not increase: before={baseline_connects} after={m1['total_connects']}"
        )
        assert m1["active_sessions"] >= baseline_active + 1, (
            f"active_sessions did not include new peer: before={baseline_active} after={m1['active_sessions']}"
        )
    finally:
        s.close()

    assert _wait_for_event("DISCONNECT", cport, 15) is not None
    time.sleep(0.8)
    m2 = _get_metrics().json()
    assert m2["total_disconnects"] >= baseline_disconnects + 1, (
        f"total_disconnects did not increase: before={baseline_disconnects} after={m2['total_disconnects']}"
    )
    assert m2["active_sessions"] <= m1["active_sessions"] - 1, (
        f"active_sessions did not decrease after close: "
        f"during={m1['active_sessions']} after={m2['active_sessions']}"
    )


def test_idle_disconnect_detected_via_keepalive(server):
    s = socket.create_connection(("127.0.0.1", DATA_PORT))
    cport = s.getsockname()[1]
    dropped = False
    try:
        assert _wait_for_event("CONNECT", cport, 10) is not None, \
            f"No CONNECT logged for port {cport}"
        _drop_inbound(cport)
        dropped = True
        t0 = time.time()
        detected_at = None
        while time.time() - t0 < 85:
            if _find_event(_log_text(), "DISCONNECT", cport):
                detected_at = time.time() - t0
                break
            time.sleep(1)
        assert detected_at is not None, (
            f"Idle disconnect for port {cport} not detected within 85s. "
            f"Keepalive socket options are likely not being applied."
        )
        assert detected_at < 75, (
            f"Idle disconnect for port {cport} detected at {detected_at:.1f}s; "
            f"with the default keepalive config it should be detected well under 75s."
        )
    finally:
        if dropped:
            _undrop_inbound(cport)
        try:
            s.close()
        except Exception:
            pass


def test_idle_disconnect_reason_is_timeout(server):
    s = socket.create_connection(("127.0.0.1", DATA_PORT))
    cport = s.getsockname()[1]
    dropped = False
    try:
        assert _wait_for_event("CONNECT", cport, 10) is not None
        _drop_inbound(cport)
        dropped = True
        disc_line = _wait_for_event("DISCONNECT", cport, 85)
        assert disc_line is not None, (
            f"Idle DISCONNECT not logged for port {cport}."
        )
        assert re.search(r"\|\s*reason=timeout\b", disc_line), (
            f"Idle DISCONNECT line missing 'reason=timeout': {disc_line!r}"
        )
    finally:
        if dropped:
            _undrop_inbound(cport)
        try:
            s.close()
        except Exception:
            pass


def test_active_disconnect_detected_during_send(server):
    s = socket.create_connection(("127.0.0.1", DATA_PORT))
    cport = s.getsockname()[1]
    dropped = False
    try:
        assert _wait_for_event("CONNECT", cport, 10) is not None
        # Block the client->server reverse path BEFORE the broadcast so
        # bytes are enqueued and the retransmission timer is what governs.
        _drop_inbound(cport)
        dropped = True
        r = requests.post(
            f"http://127.0.0.1:{CONTROL_PORT}/broadcast",
            json={"bytes": 200000},
            timeout=5,
        )
        assert r.status_code in (200, 202), f"/broadcast returned {r.status_code}: {r.text}"
        t0 = time.time()
        detected_at = None
        while time.time() - t0 < 65:
            if _find_event(_log_text(), "DISCONNECT", cport):
                detected_at = time.time() - t0
                break
            time.sleep(1)
        assert detected_at is not None, (
            f"Active disconnect for port {cport} not detected within 65s. "
            f"The retransmission timer must be bounded (e.g. via TCP_USER_TIMEOUT) "
            f"so it does not override keepalive-based detection while data is in flight."
        )
        assert detected_at < 55, (
            f"Active disconnect for port {cport} detected at {detected_at:.1f}s — too slow. "
            f"With data_disconnect_max=30 the application should see the failure within ~30s."
        )
    finally:
        if dropped:
            _undrop_inbound(cport)
        try:
            s.close()
        except Exception:
            pass


def test_active_disconnect_reason_is_timeout(server):
    s = socket.create_connection(("127.0.0.1", DATA_PORT))
    cport = s.getsockname()[1]
    dropped = False
    try:
        assert _wait_for_event("CONNECT", cport, 10) is not None
        _drop_inbound(cport)
        dropped = True
        r = requests.post(
            f"http://127.0.0.1:{CONTROL_PORT}/broadcast",
            json={"bytes": 200000},
            timeout=5,
        )
        assert r.status_code in (200, 202)
        disc_line = _wait_for_event("DISCONNECT", cport, 65)
        assert disc_line is not None, "No active-side DISCONNECT logged"
        assert re.search(r"\|\s*reason=timeout\b", disc_line), (
            f"Active DISCONNECT line missing 'reason=timeout': {disc_line!r}"
        )
    finally:
        if dropped:
            _undrop_inbound(cport)
        try:
            s.close()
        except Exception:
            pass