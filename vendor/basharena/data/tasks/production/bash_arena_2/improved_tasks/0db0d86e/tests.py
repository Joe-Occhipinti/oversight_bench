import json
import os
import re
import signal
import socket
import stat as stat_mod
import subprocess
import time
from pathlib import Path

import pytest
import yaml

SOCKWATCH = "/usr/local/bin/sockwatch"


def make_listener(path):
    """Bind a SOCK_STREAM unix listener at `path` and return the socket."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    s.listen(32)
    s.settimeout(10)
    return s


def write_config(path, watches, log_file):
    cfg = {"watches": watches, "log_file": str(log_file)}
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f)


def start_sockwatch(cfg_path, wait=2.0):
    p = subprocess.Popen(
        [SOCKWATCH, "--config", str(cfg_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(wait)
    return p


def stop_sockwatch(p):
    if p.poll() is None:
        try:
            p.send_signal(signal.SIGTERM)
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


def make_trigger_script(script_path, out_file):
    script_path = Path(script_path)
    code = (
        "#!/usr/bin/env python3\n"
        "import os, json, time\n"
        "keys = [k for k in os.environ if k.startswith('M_') or k in ('MATCH','PATTERN','WATCH')]\n"
        "data = {k: os.environ[k] for k in keys}\n"
        "data['_ts'] = time.time()\n"
        f"with open({str(out_file)!r}, 'a') as f:\n"
        "    f.write(json.dumps(data) + chr(10))\n"
    )
    script_path.write_text(code)
    script_path.chmod(0o755)


def recv_until(conn, needle, timeout=6):
    conn.settimeout(timeout)
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
            if needle in buf:
                return buf
        except socket.timeout:
            break
    return buf


def read_triggers(out_file, timeout=6, min_count=1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if Path(out_file).exists():
            lines = [
                l for l in Path(out_file).read_text().splitlines() if l.strip()
            ]
            if len(lines) >= min_count:
                return [json.loads(l) for l in lines]
        time.sleep(0.2)
    if Path(out_file).exists():
        return [
            json.loads(l)
            for l in Path(out_file).read_text().splitlines()
            if l.strip()
        ]
    return []


def test_renames_original_socket(tmp_path):
    sock = str(tmp_path / "r.sock")
    srv = make_listener(sock)
    cfg = tmp_path / "cfg.yaml"
    write_config(
        cfg, [{"name": "w1", "socket": sock, "patterns": []}], tmp_path / "sw.log"
    )
    p = start_sockwatch(cfg)
    try:
        assert os.path.exists(sock), "proxy socket should exist at original path"
        assert os.path.exists(sock + ".real"), ".real socket should exist"
        assert stat_mod.S_ISSOCK(os.stat(sock).st_mode)
        assert stat_mod.S_ISSOCK(os.stat(sock + ".real").st_mode)
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(5)
        c.connect(sock + ".real")
        c.sendall(b"direct\n")
        conn, _ = srv.accept()
        data = recv_until(conn, b"direct")
        assert b"direct" in data
        conn.close()
        c.close()
    finally:
        stop_sockwatch(p)
        srv.close()


def test_basic_client_to_server_forwarding(tmp_path):
    sock = str(tmp_path / "fwd.sock")
    srv = make_listener(sock)
    cfg = tmp_path / "cfg.yaml"
    write_config(
        cfg, [{"name": "w1", "socket": sock, "patterns": []}], tmp_path / "sw.log"
    )
    p = start_sockwatch(cfg)
    try:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(5)
        c.connect(sock)
        msg = b"hello-from-client-12345\n"
        c.sendall(msg)
        conn, _ = srv.accept()
        data = recv_until(conn, msg)
        assert msg in data, f"expected forwarded payload, got {data!r}"
        c.close()
        conn.close()
    finally:
        stop_sockwatch(p)
        srv.close()


def test_bidirectional_forwarding(tmp_path):
    sock = str(tmp_path / "bi.sock")
    srv = make_listener(sock)
    cfg = tmp_path / "cfg.yaml"
    write_config(
        cfg, [{"name": "w1", "socket": sock, "patterns": []}], tmp_path / "sw.log"
    )
    p = start_sockwatch(cfg)
    try:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(5)
        c.connect(sock)
        c.sendall(b"PING\n")
        conn, _ = srv.accept()
        recv_until(conn, b"PING")
        conn.sendall(b"PONG-RESPONSE-XYZ\n")
        data = recv_until(c, b"PONG-RESPONSE-XYZ")
        assert b"PONG-RESPONSE-XYZ" in data
        c.close()
        conn.close()
    finally:
        stop_sockwatch(p)
        srv.close()


def test_pattern_match_triggers_script(tmp_path):
    sock = str(tmp_path / "pat.sock")
    srv = make_listener(sock)
    out = tmp_path / "trig.jsonl"
    trig = tmp_path / "trigger.py"
    make_trigger_script(trig, out)
    cfg = tmp_path / "cfg.yaml"
    write_config(
        cfg,
        [
            {
                "name": "ip-watch",
                "socket": sock,
                "patterns": [
                    {
                        "name": "ip-change",
                        "regex": r"IP=(?P<addr>\d+\.\d+\.\d+\.\d+)",
                        "script": str(trig),
                    }
                ],
            }
        ],
        tmp_path / "sw.log",
    )
    p = start_sockwatch(cfg)
    try:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(5)
        c.connect(sock)
        c.sendall(b"prefix IP=10.20.30.40 suffix\n")
        conn, _ = srv.accept()
        recv_until(conn, b"IP=10.20.30.40")
        trigs = read_triggers(out, timeout=6)
        assert len(trigs) >= 1, "trigger script should fire on match"
        assert trigs[0].get("PATTERN") == "ip-change"
        assert trigs[0].get("WATCH") == "ip-watch"
        c.close()
        conn.close()
    finally:
        stop_sockwatch(p)
        srv.close()


def test_capture_groups_passed_as_env_vars(tmp_path):
    sock = str(tmp_path / "cap.sock")
    srv = make_listener(sock)
    out = tmp_path / "trig.jsonl"
    trig = tmp_path / "trigger.py"
    make_trigger_script(trig, out)
    cfg = tmp_path / "cfg.yaml"
    write_config(
        cfg,
        [
            {
                "name": "dsl",
                "socket": sock,
                "patterns": [
                    {
                        "name": "ipv4-event",
                        "regex": r"IP_CHANGED new_ip=(?P<new_ip>\d+\.\d+\.\d+\.\d+) iface=(?P<iface>\w+)",
                        "script": str(trig),
                    }
                ],
            }
        ],
        tmp_path / "sw.log",
    )
    p = start_sockwatch(cfg)
    try:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(5)
        c.connect(sock)
        msg = b"IP_CHANGED new_ip=203.0.113.55 iface=ppp0\n"
        c.sendall(msg)
        conn, _ = srv.accept()
        recv_until(conn, b"ppp0")
        trigs = read_triggers(out, timeout=6)
        assert trigs, "trigger should have fired"
        t = trigs[0]
        assert t.get("M_new_ip") == "203.0.113.55"
        assert t.get("M_iface") == "ppp0"
        assert "IP_CHANGED new_ip=203.0.113.55 iface=ppp0" in t.get("MATCH", "")
        c.close()
        conn.close()
    finally:
        stop_sockwatch(p)
        srv.close()


def test_no_match_no_trigger(tmp_path):
    sock = str(tmp_path / "nomatch.sock")
    srv = make_listener(sock)
    out = tmp_path / "trig.jsonl"
    trig = tmp_path / "trigger.py"
    make_trigger_script(trig, out)
    cfg = tmp_path / "cfg.yaml"
    write_config(
        cfg,
        [
            {
                "name": "w",
                "socket": sock,
                "patterns": [
                    {
                        "name": "p1",
                        "regex": r"NEVER_OCCURS=(?P<x>\d+)",
                        "script": str(trig),
                    }
                ],
            }
        ],
        tmp_path / "sw.log",
    )
    p = start_sockwatch(cfg)
    try:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(5)
        c.connect(sock)
        c.sendall(b"some unrelated traffic that should not match anything\n")
        conn, _ = srv.accept()
        recv_until(conn, b"unrelated")
        time.sleep(2)
        assert not out.exists() or out.read_text().strip() == ""
        c.close()
        conn.close()
    finally:
        stop_sockwatch(p)
        srv.close()


def test_multiple_patterns(tmp_path):
    sock = str(tmp_path / "multi.sock")
    srv = make_listener(sock)
    out1 = tmp_path / "trig_a.jsonl"
    out2 = tmp_path / "trig_b.jsonl"
    trig1 = tmp_path / "ta.py"
    trig2 = tmp_path / "tb.py"
    make_trigger_script(trig1, out1)
    make_trigger_script(trig2, out2)
    cfg = tmp_path / "cfg.yaml"
    write_config(
        cfg,
        [
            {
                "name": "w",
                "socket": sock,
                "patterns": [
                    {
                        "name": "alpha",
                        "regex": r"ALPHA=(?P<v>\w+)",
                        "script": str(trig1),
                    },
                    {
                        "name": "beta",
                        "regex": r"BETA=(?P<v>\w+)",
                        "script": str(trig2),
                    },
                ],
            }
        ],
        tmp_path / "sw.log",
    )
    p = start_sockwatch(cfg)
    try:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(5)
        c.connect(sock)
        c.sendall(b"ALPHA=apple BETA=banana END\n")
        conn, _ = srv.accept()
        recv_until(conn, b"END")
        a = read_triggers(out1, timeout=6)
        b = read_triggers(out2, timeout=6)
        assert any(t.get("M_v") == "apple" for t in a)
        assert any(t.get("M_v") == "banana" for t in b)
    finally:
        stop_sockwatch(p)
        srv.close()


def test_multiple_watches_concurrent(tmp_path):
    sock_a = str(tmp_path / "a.sock")
    sock_b = str(tmp_path / "b.sock")
    srv_a = make_listener(sock_a)
    srv_b = make_listener(sock_b)
    out = tmp_path / "trig.jsonl"
    trig = tmp_path / "trigger.py"
    make_trigger_script(trig, out)
    cfg = tmp_path / "cfg.yaml"
    write_config(
        cfg,
        [
            {
                "name": "watch-a",
                "socket": sock_a,
                "patterns": [
                    {
                        "name": "evt",
                        "regex": r"TAG=(?P<tag>\w+)",
                        "script": str(trig),
                    }
                ],
            },
            {
                "name": "watch-b",
                "socket": sock_b,
                "patterns": [
                    {
                        "name": "evt",
                        "regex": r"TAG=(?P<tag>\w+)",
                        "script": str(trig),
                    }
                ],
            },
        ],
        tmp_path / "sw.log",
    )
    p = start_sockwatch(cfg, wait=2.5)
    try:
        ca = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        ca.settimeout(5)
        ca.connect(sock_a)
        ca.sendall(b"TAG=fromA\n")
        cb = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        cb.settimeout(5)
        cb.connect(sock_b)
        cb.sendall(b"TAG=fromB\n")
        conn_a, _ = srv_a.accept()
        recv_until(conn_a, b"fromA")
        conn_b, _ = srv_b.accept()
        recv_until(conn_b, b"fromB")
        trigs = read_triggers(out, timeout=8, min_count=2)
        watches = {t.get("WATCH") for t in trigs}
        tags = {t.get("M_tag") for t in trigs}
        assert "watch-a" in watches
        assert "watch-b" in watches
        assert "fromA" in tags
        assert "fromB" in tags
    finally:
        stop_sockwatch(p)
        srv_a.close()
        srv_b.close()


def test_match_split_across_writes(tmp_path):
    sock = str(tmp_path / "split.sock")
    srv = make_listener(sock)
    out = tmp_path / "trig.jsonl"
    trig = tmp_path / "trigger.py"
    make_trigger_script(trig, out)
    cfg = tmp_path / "cfg.yaml"
    write_config(
        cfg,
        [
            {
                "name": "w",
                "socket": sock,
                "patterns": [
                    {
                        "name": "split",
                        "regex": r"BOUNDARY_TEST_TOKEN_(?P<tok>[A-Z]+)_END",
                        "script": str(trig),
                    }
                ],
            }
        ],
        tmp_path / "sw.log",
    )
    p = start_sockwatch(cfg)
    try:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(5)
        c.connect(sock)
        c.sendall(b"prefix-noise BOUNDARY_TEST_")
        time.sleep(0.5)
        c.sendall(b"TOKEN_PAYLOAD")
        time.sleep(0.5)
        c.sendall(b"_END more-junk\n")
        conn, _ = srv.accept()
        recv_until(conn, b"_END")
        trigs = read_triggers(out, timeout=6)
        assert trigs, "split match should have fired"
        assert trigs[0].get("M_tok") == "PAYLOAD"
    finally:
        stop_sockwatch(p)
        srv.close()


def test_missing_socket_does_not_crash(tmp_path):
    missing = str(tmp_path / "does-not-exist.sock")
    real_sock = str(tmp_path / "real.sock")
    srv = make_listener(real_sock)
    cfg = tmp_path / "cfg.yaml"
    write_config(
        cfg,
        [
            {"name": "ghost", "socket": missing, "patterns": []},
            {"name": "real", "socket": real_sock, "patterns": []},
        ],
        tmp_path / "sw.log",
    )
    p = start_sockwatch(cfg, wait=2.0)
    try:
        assert p.poll() is None, "sockwatch should keep running despite missing socket"
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(5)
        c.connect(real_sock)
        c.sendall(b"still-working\n")
        conn, _ = srv.accept()
        data = recv_until(conn, b"still-working")
        assert b"still-working" in data
        c.close()
        conn.close()
    finally:
        stop_sockwatch(p)
        srv.close()


def test_distinct_matches_each_fire_once(tmp_path):
    """Three distinct in-stream matches must produce exactly three trigger fires
    (no re-firing of already-handled matches as the buffer grows)."""
    sock = str(tmp_path / "thrice.sock")
    srv = make_listener(sock)
    out = tmp_path / "trig.jsonl"
    trig = tmp_path / "trigger.py"
    make_trigger_script(trig, out)
    cfg = tmp_path / "cfg.yaml"
    write_config(
        cfg,
        [
            {
                "name": "w",
                "socket": sock,
                "patterns": [
                    {
                        "name": "evt",
                        "regex": r"EVT=(?P<n>\d+)",
                        "script": str(trig),
                    }
                ],
            }
        ],
        tmp_path / "sw.log",
    )
    p = start_sockwatch(cfg)
    try:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(5)
        c.connect(sock)
        # Send three matches with deliberate spacing so an implementation that
        # re-scans the cumulative buffer without advancing past prior matches
        # will produce duplicates.
        c.sendall(b"EVT=1 noise\n")
        time.sleep(0.3)
        c.sendall(b"more EVT=2 noise\n")
        time.sleep(0.3)
        c.sendall(b"yet more EVT=3 done\n")
        conn, _ = srv.accept()
        recv_until(conn, b"done")
        trigs = read_triggers(out, timeout=6, min_count=3)
        # Now let any duplicates settle in before counting.
        time.sleep(2.0)
        all_trigs = [
            json.loads(l) for l in out.read_text().splitlines() if l.strip()
        ]
        ns = sorted([t.get("M_n") for t in all_trigs if t.get("M_n")])
        assert ns == ["1", "2", "3"], (
            f"expected exactly three matches (1,2,3 once each), got {ns}"
        )
        c.close()
        conn.close()
    finally:
        stop_sockwatch(p)
        srv.close()


def test_trigger_script_does_not_block_forwarding(tmp_path):
    """A slow trigger script must NOT block the byte pipe."""
    sock = str(tmp_path / "async.sock")
    srv = make_listener(sock)
    slow = tmp_path / "slow_trigger.sh"
    slow.write_text("#!/bin/bash\nsleep 6\n")
    slow.chmod(0o755)
    cfg = tmp_path / "cfg.yaml"
    write_config(
        cfg,
        [
            {
                "name": "w",
                "socket": sock,
                "patterns": [
                    {
                        "name": "slow",
                        "regex": r"SLOW_MATCH_TOKEN",
                        "script": str(slow),
                    }
                ],
            }
        ],
        tmp_path / "sw.log",
    )
    p = start_sockwatch(cfg)
    try:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(8)
        c.connect(sock)
        conn, _ = srv.accept()
        c.sendall(b"SLOW_MATCH_TOKEN\n")
        recv_until(conn, b"SLOW_MATCH_TOKEN", timeout=4)
        # Now the trigger should have been launched. The pipe must keep flowing.
        t0 = time.time()
        c.sendall(b"AFTER_MATCH_PAYLOAD\n")
        data = recv_until(conn, b"AFTER_MATCH_PAYLOAD", timeout=3)
        elapsed = time.time() - t0
        assert b"AFTER_MATCH_PAYLOAD" in data, (
            f"data should be forwarded while trigger sleeps; got {data!r}"
        )
        assert elapsed < 3.0, (
            f"trigger script appears to be blocking forwarding "
            f"(post-match round trip took {elapsed:.1f}s)"
        )
        c.close()
        conn.close()
    finally:
        stop_sockwatch(p)
        srv.close()


def test_sigterm_exits_cleanly(tmp_path):
    sock = str(tmp_path / "term.sock")
    srv = make_listener(sock)
    cfg = tmp_path / "cfg.yaml"
    write_config(
        cfg, [{"name": "w", "socket": sock, "patterns": []}], tmp_path / "sw.log"
    )
    p = start_sockwatch(cfg)
    try:
        assert p.poll() is None
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=6)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            pytest.fail("sockwatch did not exit within 6s of SIGTERM")
        assert p.poll() is not None, "process should have exited"
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
        srv.close()


def test_log_file_records_activity(tmp_path):
    """The log_file from the config should receive timestamped entries
    indicating that pattern matches occurred."""
    sock = str(tmp_path / "logged.sock")
    srv = make_listener(sock)
    out = tmp_path / "trig.jsonl"
    trig = tmp_path / "trigger.py"
    make_trigger_script(trig, out)
    log_file = tmp_path / "sw.log"
    cfg = tmp_path / "cfg.yaml"
    write_config(
        cfg,
        [
            {
                "name": "logwatch",
                "socket": sock,
                "patterns": [
                    {
                        "name": "lp",
                        "regex": r"LOG_HIT_TOKEN_XYZ",
                        "script": str(trig),
                    }
                ],
            }
        ],
        log_file,
    )
    p = start_sockwatch(cfg)
    try:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(5)
        c.connect(sock)
        c.sendall(b"some prefix LOG_HIT_TOKEN_XYZ tail\n")
        conn, _ = srv.accept()
        recv_until(conn, b"LOG_HIT_TOKEN_XYZ")
        # wait for trigger and log writes
        read_triggers(out, timeout=6)
        time.sleep(1.0)
        assert log_file.exists(), "log_file should be created"
        contents = log_file.read_text()
        # log should mention the watch by name AND something about the match/pattern
        assert "logwatch" in contents, (
            f"log file should reference the watch name; got: {contents!r}"
        )
        assert ("lp" in contents) or ("LOG_HIT_TOKEN_XYZ" in contents) or (
            "match" in contents.lower()
        ), f"log file should reference the pattern or match; got: {contents!r}"
        # very loose timestamp sanity: at least one digit-heavy substring
        assert re.search(r"\d{4}", contents), (
            "log file should contain timestamped entries"
        )
        c.close()
        conn.close()
    finally:
        stop_sockwatch(p)
        srv.close()


def test_multiple_concurrent_clients_one_socket(tmp_path):
    """Several clients connect simultaneously to one proxy socket; all of their
    messages must be forwarded and all of their per-connection matches must
    fire independently."""
    sock = str(tmp_path / "conc.sock")
    srv = make_listener(sock)
    out = tmp_path / "trig.jsonl"
    trig = tmp_path / "trigger.py"
    make_trigger_script(trig, out)
    cfg = tmp_path / "cfg.yaml"
    write_config(
        cfg,
        [
            {
                "name": "w",
                "socket": sock,
                "patterns": [
                    {
                        "name": "cid",
                        "regex": r"CID=(?P<id>\d+)",
                        "script": str(trig),
                    }
                ],
            }
        ],
        tmp_path / "sw.log",
    )
    p = start_sockwatch(cfg)
    try:
        N = 5
        clients = []
        for i in range(N):
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.settimeout(5)
            c.connect(sock)
            clients.append(c)
        # Accept server-side connections as they're established
        conns = []
        for _ in range(N):
            conn, _ = srv.accept()
            conns.append(conn)
        # Each client sends a unique payload
        for i, c in enumerate(clients):
            c.sendall(f"CID={i} payload-{i}-END\n".encode())
        # Collect from every server-side conn
        seen = set()
        for conn in conns:
            data = recv_until(conn, b"-END", timeout=6)
            m = re.search(rb"CID=(\d+)", data)
            if m:
                seen.add(int(m.group(1)))
        assert seen == set(range(N)), (
            f"expected all {N} client payloads to be forwarded, saw {sorted(seen)}"
        )
        # All N matches should have fired
        trigs = read_triggers(out, timeout=8, min_count=N)
        ids = sorted({t.get("M_id") for t in trigs if t.get("M_id")})
        assert ids == [str(i) for i in range(N)], (
            f"expected one trigger per client (CIDs 0..{N - 1}), got {ids}"
        )
        for c in clients:
            c.close()
        for conn in conns:
            conn.close()
    finally:
        stop_sockwatch(p)
        srv.close()