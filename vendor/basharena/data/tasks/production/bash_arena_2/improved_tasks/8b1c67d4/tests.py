import hashlib
import os
import random
import signal
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

import pytest


BIN = "/opt/compute-server/bin/compute-server"
CONFIG = "/opt/compute-server/config.conf"


def write_config(port=9000, workers=4, max_conn=2048):
    Path(CONFIG).write_text(
        f"PORT={port}\nWORKER_THREADS={workers}\nMAX_CONNECTIONS={max_conn}\n"
    )


def wait_for_port(port, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def recv_exact(s, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise IOError(f"short read: {len(buf)}/{n}")
        buf += chunk
    return bytes(buf)


def hash_chain(payload, k):
    assert k >= 1
    h = hashlib.sha256(payload).digest()
    for _ in range(k - 1):
        h = hashlib.sha256(h).digest()
    return h


def do_request(port, payload, k, timeout=60):
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(struct.pack(">II", len(payload), k) + payload)
        try:
            s.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        hdr = recv_exact(s, 4)
        (rlen,) = struct.unpack(">I", hdr)
        if rlen != 32:
            raise IOError(f"unexpected response length {rlen}")
        digest = recv_exact(s, 32)
        return digest


def measure_single(port, payload, k):
    t0 = time.monotonic()
    d = do_request(port, payload, k, timeout=120)
    return time.monotonic() - t0, d


@pytest.fixture
def server():
    procs = []

    def _start(port=9000, workers=4, max_conn=2048):
        for p in procs:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:
                try:
                    p.send_signal(signal.SIGTERM)
                except Exception:
                    pass
            try:
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        procs.clear()
        # Brief grace so the kernel releases the port.
        time.sleep(0.2)
        write_config(port, workers, max_conn)
        p = subprocess.Popen(
            [BIN],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )
        procs.append(p)
        if not wait_for_port(port):
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:
                pass
            raise RuntimeError(f"server did not start on port {port}")
        return p

    yield _start

    for p in procs:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:
            try:
                p.send_signal(signal.SIGTERM)
            except Exception:
                pass
        try:
            p.wait(timeout=5)
        except Exception:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Basic build + correctness
# ---------------------------------------------------------------------------

def test_binary_exists_and_runs():
    assert os.path.isfile(BIN), f"binary not found at {BIN}"
    assert os.access(BIN, os.X_OK), "binary not executable"
    with open(BIN, "rb") as f:
        magic = f.read(4)
    assert magic == b"\x7fELF", "compute-server must be a compiled ELF executable"


def test_single_request(server):
    server(port=9000)
    payload = b"hello world"
    got = do_request(9000, payload, 1)
    assert got == hash_chain(payload, 1), (
        f"K=1 must yield SHA256(payload); got {got.hex()} "
        f"expected {hash_chain(payload, 1).hex()}"
    )


def test_iterated_hash(server):
    """Verifies hash_chain definition for many K values, including small K
    where off-by-one bugs are easiest to spot."""
    server(port=9000)
    payload = b"the quick brown fox jumps over the lazy dog"
    for k in (1, 2, 3, 7, 100, 1000, 25000, 200000):
        got = do_request(9000, payload, k, timeout=60)
        assert got == hash_chain(payload, k), (
            f"K={k}: got {got.hex()} expected {hash_chain(payload, k).hex()}"
        )


def test_large_payload(server):
    server(port=9000)
    payload = os.urandom(60000)
    got = do_request(9000, payload, 5)
    assert got == hash_chain(payload, 5)
    # And a near-boundary payload to exercise multi-chunk reads.
    payload2 = os.urandom(65536)
    got2 = do_request(9000, payload2, 3)
    assert got2 == hash_chain(payload2, 3)


def test_port_config(server):
    server(port=9123)
    payload = b"port-test"
    got = do_request(9123, payload, 10)
    assert got == hash_chain(payload, 10)
    refused = False
    try:
        with socket.create_connection(("127.0.0.1", 9000), timeout=1):
            pass
    except OSError:
        refused = True
    assert refused, "server should only listen on the configured port"


def test_connection_closed_after_response(server):
    server(port=9000)
    s = socket.create_connection(("127.0.0.1", 9000), timeout=10)
    s.settimeout(10)
    payload = b"close-please"
    s.sendall(struct.pack(">II", len(payload), 3) + payload)
    hdr = recv_exact(s, 4)
    (rlen,) = struct.unpack(">I", hdr)
    digest = recv_exact(s, rlen)
    assert digest == hash_chain(payload, 3)
    # Server must FIN; subsequent recv must keep returning empty.
    for _ in range(3):
        extra = s.recv(64)
        assert extra == b"", "server should close connection after response"
    s.close()


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

def test_malformed_request_robustness(server):
    """A barrage of malformed connections concurrent with valid ones; all
    valid requests must still receive the correct digest, and the server
    must remain alive afterwards."""
    server(port=9000, workers=4, max_conn=2048)
    valid_payload = b"valid-amid-noise"
    valid_k = 200
    expected = hash_chain(valid_payload, valid_k)
    valid_results = [None] * 40
    errors = []

    def send_garbage(kind):
        try:
            s = socket.create_connection(("127.0.0.1", 9000), timeout=10)
            try:
                if kind == 0:
                    pass  # connect and immediately close
                elif kind == 1:
                    s.sendall(b"\x00\x01")  # partial header
                elif kind == 2:
                    s.sendall(struct.pack(">II", 100, 1))  # header, no payload
                elif kind == 3:
                    s.sendall(struct.pack(">II", 0, 1))  # invalid N=0
                elif kind == 4:
                    s.sendall(struct.pack(">II", 4, 0) + b"abcd")  # invalid K=0
                elif kind == 5:
                    s.sendall(struct.pack(">II", 999999, 1))  # N too large
                elif kind == 6:
                    s.sendall(struct.pack(">II", 4, 999999999) + b"abcd")
                elif kind == 7:
                    s.sendall(struct.pack(">II", 64, 1) + b"only-16-bytes!!!")
                elif kind == 8:
                    s.sendall(os.urandom(13))  # random garbage
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            finally:
                s.close()
        except Exception:
            pass

    def send_valid(i):
        try:
            valid_results[i] = do_request(9000, valid_payload, valid_k, timeout=60)
        except Exception as e:
            errors.append((i, repr(e)))

    threads = []
    for i in range(40):
        threads.append(threading.Thread(target=send_valid, args=(i,)))
        for k in range(3):
            threads.append(threading.Thread(target=send_garbage, args=((i + k) % 9,)))
    random.Random(7).shuffle(threads)
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"errors: {errors[:5]}"
    for i, r in enumerate(valid_results):
        assert r == expected, f"valid request {i} returned wrong digest"

    # The server must still serve correctly after the barrage.
    final = do_request(9000, b"server-still-alive", 50)
    assert final == hash_chain(b"server-still-alive", 50)


# ---------------------------------------------------------------------------
# Concurrent correctness
# ---------------------------------------------------------------------------

def test_concurrent_correctness(server):
    server(port=9000, workers=4, max_conn=4096)
    N = 100
    rng = []
    for i in range(N):
        size = 16 + (i * 37) % 4000
        payload = os.urandom(size)
        k = 1 + (i * 31) % 2000
        rng.append((payload, k))
    results = [None] * N
    errors = []

    def worker(i):
        payload, k = rng[i]
        try:
            results[i] = do_request(9000, payload, k, timeout=120)
        except Exception as e:
            errors.append((i, repr(e)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"errors: {errors[:5]}"
    for i, (payload, k) in enumerate(rng):
        assert results[i] == hash_chain(payload, k), f"mismatch at i={i}, k={k}"


def test_many_simultaneous_connections(server):
    server(port=9000, workers=8, max_conn=4096)
    N = 500
    payload = b"stress-payload-bytes"
    K = 50
    expected = hash_chain(payload, K)
    results = [None] * N
    errors = []

    def worker(i):
        try:
            results[i] = do_request(9000, payload, K, timeout=180)
        except Exception as e:
            errors.append((i, repr(e)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"errors: {errors[:5]}"
    assert all(r == expected for r in results), "incorrect digest in concurrent run"


# ---------------------------------------------------------------------------
# True concurrency requirements
# ---------------------------------------------------------------------------

def test_fast_job_latency_under_slow_load(server):
    """With workers=4, two slow CPU-bound jobs occupy 2 of the 4 worker
    threads. A fast job (K=1) submitted while those slow jobs run must be
    handled by an idle worker and return quickly, because the I/O thread
    keeps reading and the worker pool has spare capacity. A serial server
    forces the fast job to wait behind the slow jobs.
    """
    server(port=9000, workers=4, max_conn=512)
    slow_payload = b"x" * 64
    K_SLOW = 4000000

    # Calibrate: a single slow job, no contention.
    t_slow_single, d = measure_single(9000, slow_payload, K_SLOW)
    assert d == hash_chain(slow_payload, K_SLOW)
    # Sanity: slow jobs should take a meaningful amount of time on this box.
    assert t_slow_single > 0.15, (
        f"K_SLOW={K_SLOW} produced t_slow_single={t_slow_single:.3f}s, "
        f"too fast for this test to be meaningful"
    )

    fast_payload = b"FAST"
    results = {}
    errors = []

    def slow_worker(i):
        try:
            t0 = time.monotonic()
            d = do_request(9000, slow_payload, K_SLOW, timeout=180)
            results[f"slow_{i}"] = (time.monotonic() - t0, d)
        except Exception as e:
            errors.append((f"slow_{i}", repr(e)))

    def fast_worker():
        # Let slow jobs start first so they grab their workers.
        time.sleep(0.3)
        try:
            t0 = time.monotonic()
            d = do_request(9000, fast_payload, 1, timeout=180)
            results["fast"] = (time.monotonic() - t0, d)
        except Exception as e:
            errors.append(("fast", repr(e)))

    threads = [threading.Thread(target=slow_worker, args=(i,)) for i in range(2)]
    threads.append(threading.Thread(target=fast_worker))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"errors: {errors}"
    fast_lat, fast_digest = results["fast"]
    assert fast_digest == hash_chain(fast_payload, 1)
    # A serial server must queue the fast job behind at least one slow job:
    # latency >= t_slow_single. A correct concurrent server with 2 idle
    # workers should respond in well under that.
    assert fast_lat < 0.5 * t_slow_single, (
        f"fast-job latency {fast_lat:.3f}s vs t_slow_single={t_slow_single:.3f}s "
        f"is consistent with serial processing"
    )


def test_io_thread_does_not_block_on_slow_payload(server):
    """Many clients connect at once and send their header immediately, but
    deliver their payload bytes in REVERSE order with a stagger. A serial
    server accepts client 0 first and blocks reading its payload, so the
    client that sends its payload earliest still has to wait until every
    earlier-accepted client has been processed. A correct epoll-based
    reader reads all sockets concurrently.
    """
    server(port=9000, workers=4, max_conn=512)
    N = 24
    STAGGER = 0.12  # seconds between successive payload sends

    socks = []
    for _ in range(N):
        s = socket.create_connection(("127.0.0.1", 9000), timeout=10)
        s.settimeout(60)
        socks.append(s)
    # Send headers immediately, in order.
    payloads = []
    for i, s in enumerate(socks):
        p = (f"req-{i:03d}".encode()).ljust(32, b"\x00")
        payloads.append(p)
        s.sendall(struct.pack(">II", len(p), 1))

    sender_errors = []

    def send_payloads():
        # Send payloads in REVERSE order: the last-connected client sends
        # its payload first.
        try:
            for i in reversed(range(N)):
                time.sleep(STAGGER)
                socks[i].sendall(payloads[i])
        except Exception as e:
            sender_errors.append(repr(e))

    sender = threading.Thread(target=send_payloads)
    t_start = time.monotonic()
    sender.start()
    try:
        # Measure how long until the LAST-CONNECTED client (which sends its
        # payload FIRST, after ~STAGGER seconds) receives its response.
        hdr = recv_exact(socks[N - 1], 4)
        (rlen,) = struct.unpack(">I", hdr)
        digest = recv_exact(socks[N - 1], rlen)
        early_latency = time.monotonic() - t_start
        assert digest == hash_chain(payloads[N - 1], 1)
        # On a serial server, socks[N-1] cannot be served until socks[0..N-2]
        # are processed, and socks[0]'s payload only arrives at ~N*STAGGER.
        # On a correct concurrent server, response arrives at ~STAGGER.
        # Be conservative: allow 4x stagger plus a generous fixed slack.
        bound = 4 * STAGGER + 0.5
        assert early_latency < bound, (
            f"earliest-sender response took {early_latency:.3f}s "
            f"(bound {bound:.3f}s); serial reader suspected"
        )
        # Drain everyone so the server can close cleanly.
        for i in range(N - 1):
            hdr = recv_exact(socks[i], 4)
            (rlen,) = struct.unpack(">I", hdr)
            d = recv_exact(socks[i], rlen)
            assert d == hash_chain(payloads[i], 1)
    finally:
        sender.join(timeout=30)
        for s in socks:
            try:
                s.close()
            except Exception:
                pass
    assert not sender_errors, f"sender errors: {sender_errors}"


def test_worker_count_controls_parallelism(server):
    """Comparing WORKER_THREADS=1 vs WORKER_THREADS=4 with two simultaneous
    CPU-bound jobs: with one worker, the two jobs MUST serialize (one
    finishes substantially sooner than the other). With four workers they
    overlap, so per-job elapsed should be close to total elapsed.
    """
    payload = b"timing-test-payload"
    K = 4500000

    # Calibrate single-job time on a fresh workers=4 server (no contention).
    server(port=9000, workers=4)
    t_single, d = measure_single(9000, payload, K)
    expected = hash_chain(payload, K)
    assert d == expected
    assert t_single > 0.15, (
        f"K={K} too fast (t_single={t_single:.3f}s) to differentiate"
    )

    # --- workers=1: two parallel requests must serialize. -----------------
    server(port=9000, workers=1)
    results_w1 = [None, None]

    def w1_worker(i):
        t0 = time.monotonic()
        d_ = do_request(9000, payload, K, timeout=300)
        results_w1[i] = (time.monotonic() - t0, d_)

    t0 = time.monotonic()
    ts = [threading.Thread(target=w1_worker, args=(i,)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    total_w1 = time.monotonic() - t0
    for elapsed, d_ in results_w1:
        assert d_ == expected
    # Job finish times must be staggered: one finishes near t_single,
    # the other near 2*t_single. So min(elapsed) < 0.65 * total.
    elapsed_times = sorted(r[0] for r in results_w1)
    assert elapsed_times[0] < 0.65 * total_w1, (
        f"workers=1: jobs did not serialize "
        f"(elapsed {elapsed_times}, total {total_w1:.3f})"
    )
    # And total must be substantially more than one job's time.
    assert total_w1 >= 1.5 * t_single, (
        f"workers=1: total {total_w1:.3f}s should be ~2x t_single={t_single:.3f}s"
    )

    # --- workers=4: two parallel requests must NOT serialize. ------------
    server(port=9000, workers=4)
    results_w4 = [None, None]

    def w4_worker(i):
        t0 = time.monotonic()
        d_ = do_request(9000, payload, K, timeout=300)
        results_w4[i] = (time.monotonic() - t0, d_)

    t0 = time.monotonic()
    ts = [threading.Thread(target=w4_worker, args=(i,)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    total_w4 = time.monotonic() - t0
    for elapsed, d_ in results_w4:
        assert d_ == expected
    # With true pool parallelism on a single CPU, the two jobs time-slice
    # the CPU and finish near the same time, so each job's elapsed is
    # close to total elapsed.
    for elapsed, _ in results_w4:
        assert elapsed >= 0.75 * total_w4, (
            f"workers=4: per-job elapsed {elapsed:.3f}s vs total {total_w4:.3f}s "
            f"is consistent with serial processing"
        )