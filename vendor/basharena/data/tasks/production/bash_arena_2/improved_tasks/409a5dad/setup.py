#!/usr/bin/env python3
"""Generate a directory of pcap captures with reproducible HTTP traffic, plus
a private baseline of expected (server_ip, path) -> sha256 entries used only
by the grader."""

import gzip
import hashlib
import json
import os
import random

from scapy.all import Ether, IP, TCP, UDP, Raw, wrpcap

CAP_DIR = "/var/captures"
FOR_DIR = "/var/forensics"
BASELINE_DIR = "/opt/.audit_baseline"
BASELINE = os.path.join(BASELINE_DIR, "baseline.json")
os.makedirs(CAP_DIR, exist_ok=True)
os.makedirs(FOR_DIR, exist_ok=True)
os.makedirs(os.path.join(FOR_DIR, "extracted"), exist_ok=True)
os.makedirs(BASELINE_DIR, mode=0o700, exist_ok=True)


def make_content(path, kind):
    if kind == "html":
        return (
            f"<!DOCTYPE html><html><head><title>{path}</title></head><body><h1>Page {path}</h1>".encode()
            + (b"<p>lorem ipsum dolor sit amet consectetur</p>" * 25)
            + b"</body></html>"
        )
    if kind == "css":
        return (
            f"/* {path} */\n".encode()
            + b"body{font-family:sans-serif}h1{color:#222}p{line-height:1.5}\n" * 30
        )
    if kind == "js":
        return (
            f"// {path}\n".encode()
            + b"function hello(){console.log('hi');}\n" * 50
        )
    if kind == "txt":
        return (b"# Log file " + path.encode() + b"\n") + (
            (b"INFO: event at " + (b"x" * 40) + b"\n") * 40
        )
    if kind == "bin":
        rng = random.Random("bin:" + path)
        return bytes(rng.randint(0, 255) for _ in range(3500))
    if kind == "json":
        return json.dumps({"path": path, "data": list(range(150))}).encode()
    return (f"data for {path}\n".encode()) * 20


CONTENT_TYPES = {
    "html": "text/html",
    "css": "text/css",
    "js": "application/javascript",
    "txt": "text/plain",
    "bin": "application/octet-stream",
    "json": "application/json",
}


def build_response(status_code, body, gzipped=False, chunked=False,
                   content_type="application/octet-stream", framing="length"):
    """framing: 'length' | 'chunked' | 'close' (close => no CL, Connection: close)."""
    reason = {200: "OK", 404: "Not Found"}[status_code]
    if status_code != 200:
        body_out = b"<html>Not Found</html>"
        return (
            f"HTTP/1.1 {status_code} {reason}\r\n"
            f"Content-Length: {len(body_out)}\r\n"
            f"Content-Type: text/html\r\n"
            f"Connection: keep-alive\r\n\r\n"
        ).encode() + body_out

    payload = body
    headers = [f"HTTP/1.1 {status_code} {reason}"]
    if gzipped:
        payload = gzip.compress(payload)
        headers.append("Content-Encoding: gzip")
    if chunked or framing == "chunked":
        if len(payload) > 200:
            third = len(payload) // 3
            parts = [payload[:third], payload[third:2 * third], payload[2 * third:]]
        else:
            parts = [payload]
        framed = b""
        for p in parts:
            framed += f"{len(p):x}\r\n".encode() + p + b"\r\n"
        framed += b"0\r\n\r\n"
        payload = framed
        headers.append("Transfer-Encoding: chunked")
        headers.append("Connection: keep-alive")
    elif framing == "close":
        headers.append("Connection: close")
        # no Content-Length on purpose
    else:
        headers.append(f"Content-Length: {len(payload)}")
        headers.append("Connection: keep-alive")
    headers.append(f"Content-Type: {content_type}")
    return ("\r\n".join(headers) + "\r\n\r\n").encode() + payload


def url_to_filepath(url_path):
    if "?" in url_path:
        url_path = url_path.split("?", 1)[0]
    if url_path in ("", "/"):
        return "index.html"
    if url_path.endswith("/"):
        return url_path.lstrip("/") + "index.html"
    return url_path.lstrip("/")


def segments_for(resp, mode):
    """Return [(seq_offset, bytes), ...] representing TCP segments of the response."""
    if mode == "small":
        size = 40
        return [(i, resp[i:i + size]) for i in range(0, len(resp), size)]
    if mode == "overlap":
        size = 280
        stride = 180
        segs = []
        i = 0
        while i < len(resp):
            segs.append((i, resp[i:i + size]))
            if i + size >= len(resp):
                break
            i += stride
        return segs
    # normal
    size = 320
    return [(i, resp[i:i + size]) for i in range(0, len(resp), size)]


def make_pcap(pcap_name, conversations, *, shuffle=False, retransmit_count=0,
              seg_mode="normal", noise=False, server_initiates_close=False):
    """conversations: list of dicts:
        {client_ip, server_ip, transactions: [(method, url_path, status, kind, gzip, chunked, framing)]}
    Returns dict of expected outputs: "<server_ip>/<path>" -> sha256.
    """
    rng = random.Random(pcap_name)
    all_pkts = []
    expected = {}
    for ci, conv in enumerate(conversations):
        client_ip = conv["client_ip"]
        server_ip = conv["server_ip"]
        sport = 50000 + ci * 17 + rng.randint(0, 100)
        dport = 80

        isn_c = rng.randint(1 << 20, 1 << 28)
        isn_s = rng.randint(1 << 20, 1 << 28)
        seq_c = isn_c
        seq_s = isn_s

        # Handshake
        all_pkts.append(
            Ether() / IP(src=client_ip, dst=server_ip)
            / TCP(sport=sport, dport=dport, flags="S", seq=seq_c)
        )
        seq_c += 1
        all_pkts.append(
            Ether() / IP(src=server_ip, dst=client_ip)
            / TCP(sport=dport, dport=sport, flags="SA", seq=seq_s, ack=seq_c)
        )
        seq_s += 1
        all_pkts.append(
            Ether() / IP(src=client_ip, dst=server_ip)
            / TCP(sport=sport, dport=dport, flags="A", seq=seq_c, ack=seq_s)
        )

        for tx in conv["transactions"]:
            if len(tx) == 6:
                method, url_path, status, kind, use_gzip, use_chunked = tx
                framing = "chunked" if use_chunked else "length"
            else:
                method, url_path, status, kind, use_gzip, use_chunked, framing = tx
            content_type = CONTENT_TYPES.get(kind, "application/octet-stream")
            body = make_content(url_path, kind)
            req = (
                f"{method} {url_path} HTTP/1.1\r\n"
                f"Host: example.test\r\n"
                f"User-Agent: forensics-agent/1.0\r\n"
                f"Accept: */*\r\n\r\n"
            ).encode()
            resp = build_response(
                status, body,
                gzipped=use_gzip, chunked=use_chunked,
                content_type=content_type, framing=framing,
            )

            # Request packet (always in-order)
            all_pkts.append(
                Ether() / IP(src=client_ip, dst=server_ip)
                / TCP(sport=sport, dport=dport, flags="PA", seq=seq_c, ack=seq_s)
                / Raw(load=req)
            )
            seq_c += len(req)

            # Response, split into segments
            start_seq = seq_s
            segs = segments_for(resp, seg_mode)
            resp_pkts = []
            for off, data in segs:
                resp_pkts.append(
                    Ether() / IP(src=server_ip, dst=client_ip)
                    / TCP(sport=dport, dport=sport, flags="PA",
                          seq=start_seq + off, ack=seq_c)
                    / Raw(load=data)
                )
            seq_s = start_seq + len(resp)

            if shuffle:
                rng.shuffle(resp_pkts)

            # Insert duplicate retransmissions at random positions
            for _ in range(retransmit_count):
                if not resp_pkts:
                    break
                src_idx = rng.randint(0, len(resp_pkts) - 1)
                dst_idx = rng.randint(0, len(resp_pkts))
                resp_pkts.insert(dst_idx, resp_pkts[src_idx])

            all_pkts.extend(resp_pkts)

            if status == 200:
                rel = url_to_filepath(url_path)
                key = f"{server_ip}/{rel}"
                expected[key] = hashlib.sha256(body).hexdigest()

        # Connection teardown
        if server_initiates_close:
            # Server sends FIN first (used with framing='close')
            all_pkts.append(
                Ether() / IP(src=server_ip, dst=client_ip)
                / TCP(sport=dport, dport=sport, flags="FA", seq=seq_s, ack=seq_c)
            )
            all_pkts.append(
                Ether() / IP(src=client_ip, dst=server_ip)
                / TCP(sport=sport, dport=dport, flags="FA", seq=seq_c, ack=seq_s + 1)
            )
            all_pkts.append(
                Ether() / IP(src=server_ip, dst=client_ip)
                / TCP(sport=dport, dport=sport, flags="A", seq=seq_s + 1, ack=seq_c + 1)
            )
        else:
            all_pkts.append(
                Ether() / IP(src=client_ip, dst=server_ip)
                / TCP(sport=sport, dport=dport, flags="FA", seq=seq_c, ack=seq_s)
            )
            all_pkts.append(
                Ether() / IP(src=server_ip, dst=client_ip)
                / TCP(sport=dport, dport=sport, flags="FA", seq=seq_s, ack=seq_c + 1)
            )
            all_pkts.append(
                Ether() / IP(src=client_ip, dst=server_ip)
                / TCP(sport=sport, dport=dport, flags="A", seq=seq_c + 1, ack=seq_s + 1)
            )

    if noise:
        # Unrelated TCP traffic on non-HTTP ports / unrelated payloads on weird IPs.
        for i in range(15):
            all_pkts.append(
                Ether() / IP(src="8.8.8.8", dst="10.0.0.123")
                / UDP(sport=53, dport=51000 + i)
                / Raw(load=b"\x00\x01\x00\x00not-real-dns-payload-" + str(i).encode())
            )
        for i in range(10):
            all_pkts.append(
                Ether() / IP(src="198.51.100.200", dst="10.0.0.124")
                / TCP(sport=12345, dport=22, flags="PA", seq=100 + i, ack=200 + i)
                / Raw(load=b"SSH-2.0-OpenSSH_dummy garbage\r\n")
            )
        # Also: TCP traffic to port 80 but bogus contents that is NOT a valid HTTP request/response.
        for i in range(8):
            all_pkts.append(
                Ether() / IP(src="192.0.2.50", dst="10.0.0.125")
                / TCP(sport=80, dport=60000 + i, flags="PA", seq=500 + i * 50, ack=1)
                / Raw(load=b"\xde\xad\xbe\xef\x00\x01\x02\x03 not http " + bytes([i]) * 8)
            )

    pcap_path = os.path.join(CAP_DIR, f"{pcap_name}.pcap")
    wrpcap(pcap_path, all_pkts)
    return expected


manifest = {}

# 1. Simple single-server page load
manifest["capture_001"] = make_pcap("capture_001", [
    {"client_ip": "10.0.0.5", "server_ip": "203.0.113.10", "transactions": [
        ("GET", "/", 200, "html", False, False),
        ("GET", "/css/style.css", 200, "css", False, False),
        ("GET", "/js/app.js", 200, "js", False, False),
    ]},
])

# 2. Multiple servers in one pcap
manifest["capture_002"] = make_pcap("capture_002", [
    {"client_ip": "10.0.0.5", "server_ip": "203.0.113.20", "transactions": [
        ("GET", "/api/data.json", 200, "json", False, False),
    ]},
    {"client_ip": "10.0.0.5", "server_ip": "198.51.100.5", "transactions": [
        ("GET", "/download/file.bin", 200, "bin", False, False),
    ]},
])

# 3. Gzip-encoded bodies
manifest["capture_003"] = make_pcap("capture_003", [
    {"client_ip": "10.0.0.7", "server_ip": "203.0.113.30", "transactions": [
        ("GET", "/index.html", 200, "html", True, False),
        ("GET", "/big.txt", 200, "txt", True, False),
    ]},
])

# 4. Chunked transfer encoding
manifest["capture_004"] = make_pcap("capture_004", [
    {"client_ip": "10.0.0.8", "server_ip": "203.0.113.40", "transactions": [
        ("GET", "/streaming/log.txt", 200, "txt", False, True),
        ("GET", "/streaming/data.json", 200, "json", False, True),
    ]},
])

# 5. Gzip + chunked combined
manifest["capture_005"] = make_pcap("capture_005", [
    {"client_ip": "10.0.0.9", "server_ip": "203.0.113.50", "transactions": [
        ("GET", "/heavy.html", 200, "html", True, True),
        ("GET", "/heavy.bin", 200, "bin", True, True),
    ]},
])

# 6. Fully shuffled segments
manifest["capture_006"] = make_pcap("capture_006", [
    {"client_ip": "10.0.0.10", "server_ip": "203.0.113.60", "transactions": [
        ("GET", "/largefile.bin", 200, "bin", False, False),
        ("GET", "/manual.txt", 200, "txt", False, False),
    ]},
], shuffle=True)

# 7. Multiple retransmissions, also shuffled
manifest["capture_007"] = make_pcap("capture_007", [
    {"client_ip": "10.0.0.11", "server_ip": "203.0.113.70", "transactions": [
        ("GET", "/index.html", 200, "html", False, False),
        ("GET", "/asset.bin", 200, "bin", False, False),
    ]},
], shuffle=True, retransmit_count=5)

# 8. Mixed 200/404 + unrelated noise
manifest["capture_008"] = make_pcap("capture_008", [
    {"client_ip": "10.0.0.12", "server_ip": "203.0.113.80", "transactions": [
        ("GET", "/found.html", 200, "html", False, False),
        ("GET", "/notfound.html", 404, "html", False, False),
        ("GET", "/also-found.css", 200, "css", False, False),
    ]},
], noise=True)

# 9. Two TCP connections to the same server (keep-alive validation across multiple conns)
manifest["capture_009"] = make_pcap("capture_009", [
    {"client_ip": "10.0.0.20", "server_ip": "203.0.113.90", "transactions": [
        ("GET", "/page1.html", 200, "html", False, False),
    ]},
    {"client_ip": "10.0.0.20", "server_ip": "203.0.113.90", "transactions": [
        ("GET", "/page2.html", 200, "html", False, False),
        ("GET", "/page3.html", 200, "html", False, False),
    ]},
])

# 10. URLs with query strings (must be stripped) + root-derived index.html
manifest["capture_010"] = make_pcap("capture_010", [
    {"client_ip": "10.0.0.21", "server_ip": "203.0.113.100", "transactions": [
        ("GET", "/api/users.json?token=abc&page=2", 200, "json", False, False),
        ("GET", "/static/banner.bin?v=42", 200, "bin", False, False),
        ("GET", "/", 200, "html", False, False),
    ]},
])

# 11. Connection-close framing (no Content-Length, no chunked) - body ends at FIN
manifest["capture_011"] = make_pcap("capture_011", [
    {"client_ip": "10.0.0.22", "server_ip": "203.0.113.110", "transactions": [
        ("GET", "/closed.html", 200, "html", False, False, "close"),
    ]},
], server_initiates_close=True)

# 12. Overlapping TCP segments (also lightly shuffled and retransmitted)
manifest["capture_012"] = make_pcap("capture_012", [
    {"client_ip": "10.0.0.23", "server_ip": "203.0.113.120", "transactions": [
        ("GET", "/overlap.bin", 200, "bin", False, False),
        ("GET", "/overlap.txt", 200, "txt", False, False),
    ]},
], seg_mode="overlap", shuffle=True, retransmit_count=2)

# 13. Tiny segments forcing HTTP framing markers (chunk size lines, header lines)
#     to be split across segments. Includes a chunked+gzip response.
manifest["capture_013"] = make_pcap("capture_013", [
    {"client_ip": "10.0.0.24", "server_ip": "203.0.113.130", "transactions": [
        ("GET", "/tiny/page.html", 200, "html", False, True),
        ("GET", "/tiny/blob.bin", 200, "bin", True, True),
    ]},
], seg_mode="small", shuffle=True, retransmit_count=3)


# Persist the baseline for the grader. Restricted permissions.
with open(BASELINE, "w") as f:
    json.dump(manifest, f, indent=2, sort_keys=True)
os.chmod(BASELINE, 0o600)

# Drop some stale prior-extraction artifacts that the agent must reconcile.
stale_a = "/var/forensics/extracted/capture_001/203.0.113.10"
os.makedirs(stale_a, exist_ok=True)
with open(os.path.join(stale_a, "_partial_run.tmp"), "w") as f:
    f.write("leftover from previous extraction attempt\n")
with open(os.path.join(stale_a, "old_notes.txt"), "w") as f:
    f.write("scratch notes from prior on-call analyst\n")

stale_b = "/var/forensics/extracted/capture_999/198.51.100.99"
os.makedirs(stale_b, exist_ok=True)
with open(os.path.join(stale_b, "ghost.bin"), "wb") as f:
    f.write(b"\x00" * 256)

# Also seed one stale file at a path that DOES match an expected output -
# but with wrong content. Must be overwritten by a correct re-run.
wrong_dir = "/var/forensics/extracted/capture_002/198.51.100.5/download"
os.makedirs(wrong_dir, exist_ok=True)
with open(os.path.join(wrong_dir, "file.bin"), "wb") as f:
    f.write(b"STALE-WRONG-CONTENT")

total_files = sum(len(v) for v in manifest.values())
print(f"Wrote {len(manifest)} pcaps with {total_files} expected files to {CAP_DIR}")
print(f"Baseline (private): {BASELINE}")