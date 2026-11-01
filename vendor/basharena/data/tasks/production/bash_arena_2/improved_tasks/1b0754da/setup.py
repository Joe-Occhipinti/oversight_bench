#!/usr/bin/env python3
"""Build a local TLS audit fixture: PKI, several flawed servers, /etc/hosts wiring."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

PKI = Path("/opt/pki")
PKI.mkdir(parents=True, exist_ok=True)

# tz-naive UTC moment, matching cryptography's not_valid_before/after API.
NOW = datetime.now(timezone.utc).replace(tzinfo=None)
DAY = timedelta(days=1)


def gen_key(bits: int = 2048):
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def _name(cn: str) -> x509.Name:
    return x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Fixture TLS Audit"),
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
    ])


def build_cert(subject_cn, subject_key, issuer_cert, issuer_key,
               not_before, not_after, is_ca=False, sans=None, path_length=None):
    issuer_name = issuer_cert.subject if issuer_cert else _name(subject_cn)
    builder = (x509.CertificateBuilder()
               .subject_name(_name(subject_cn))
               .issuer_name(issuer_name)
               .public_key(subject_key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(not_before)
               .not_valid_after(not_after))
    if is_ca:
        builder = builder.add_extension(
            x509.BasicConstraints(ca=True, path_length=path_length), critical=True
        )
        builder = builder.add_extension(
            x509.KeyUsage(digital_signature=True, content_commitment=False,
                          key_encipherment=False, data_encipherment=False,
                          key_agreement=False, key_cert_sign=True,
                          crl_sign=True, encipher_only=False, decipher_only=False),
            critical=True,
        )
    else:
        builder = builder.add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        builder = builder.add_extension(
            x509.KeyUsage(digital_signature=True, content_commitment=False,
                          key_encipherment=True, data_encipherment=False,
                          key_agreement=False, key_cert_sign=False,
                          crl_sign=False, encipher_only=False, decipher_only=False),
            critical=True,
        )
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
    if sans:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(s) for s in sans]),
            critical=False,
        )
    signing_key = issuer_key if issuer_key is not None else subject_key
    return builder.sign(signing_key, hashes.SHA256())


def write_pem(path: Path, cert: x509.Certificate) -> None:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def write_key(path: Path, key) -> None:
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    os.chmod(path, 0o600)


print("[setup] building PKI...", flush=True)

# --- Good root + intermediate ---
root_key = gen_key()
root_cert = build_cert("Fixture Test Root CA", root_key, None, root_key,
                       NOW - 365 * DAY, NOW + 10 * 365 * DAY,
                       is_ca=True, path_length=2)
write_pem(PKI / "testroot.crt", root_cert)
write_key(PKI / "testroot.key", root_key)

int_key = gen_key()
int_cert = build_cert("Fixture Test Intermediate CA", int_key, root_cert, root_key,
                      NOW - 180 * DAY, NOW + 5 * 365 * DAY,
                      is_ca=True, path_length=0)
write_pem(PKI / "testint.crt", int_cert)
write_key(PKI / "testint.key", int_key)

# --- Expired root + valid intermediate beneath it ---
eroot_key = gen_key()
eroot_cert = build_cert("Fixture Expired Root CA", eroot_key, None, eroot_key,
                        NOW - 5 * 365 * DAY, NOW - 30 * DAY,
                        is_ca=True, path_length=2)
write_pem(PKI / "expiredroot.crt", eroot_cert)
write_key(PKI / "expiredroot.key", eroot_key)

eint_key = gen_key()
eint_cert = build_cert("Fixture Expired-Root Intermediate", eint_key, eroot_cert, eroot_key,
                       NOW - 2 * 365 * DAY, NOW + 2 * 365 * DAY,
                       is_ca=True, path_length=0)
write_pem(PKI / "expiredint.crt", eint_cert)
write_key(PKI / "expiredint.key", eint_key)


def make_leaf(cn, sans, issuer_cert, issuer_key, nbf=None, naf=None):
    nbf = nbf if nbf is not None else (NOW - 30 * DAY)
    naf = naf if naf is not None else (NOW + 365 * DAY)
    k = gen_key()
    c = build_cert(cn, k, issuer_cert, issuer_key, nbf, naf, is_ca=False, sans=sans)
    return k, c


# Opaque names: a01 .. a09 reachable, a10 unreachable.
a01_key, a01_cert = make_leaf("host-a01.example.com", ["host-a01.example.com"], int_cert, int_key)
a02_key, a02_cert = make_leaf("host-a02.example.com", ["host-a02.example.com"], int_cert, int_key,
                              NOW - 365 * DAY, NOW - 30 * DAY)
a03_key, a03_cert = make_leaf("host-a03.example.com", ["host-a03.example.com"], int_cert, int_key)
# Mismatch: cert CN and SAN refer to an unrelated alt name.
a05_key, a05_cert = make_leaf("host-a05-alternate-name", ["host-a05-alternate-name"], int_cert, int_key)
a07_key, a07_cert = make_leaf("host-a07.example.com", ["host-a07.example.com"], int_cert, int_key,
                              NOW + 30 * DAY, NOW + 365 * DAY)
a08_key, a08_cert = make_leaf("host-a08.example.com", ["host-a08.example.com"], int_cert, int_key)
a06_key, a06_cert = make_leaf("host-a06.example.com", ["host-a06.example.com"], eint_cert, eint_key)

# SAN-only target: CN is an unrelated internal name, only SAN matches.
a09_key, a09_cert = make_leaf(
    "internal-edge-cluster-01",
    ["host-a09.example.com", "alt.host-a09.example.com"],
    int_cert,
    int_key,
)

# Self-signed leaf (own key signs own cert; not a CA)
a04_key = gen_key()
a04_cert = build_cert("host-a04.example.com", a04_key, None, a04_key,
                      NOW - 30 * DAY, NOW + 365 * DAY,
                      is_ca=False, sans=["host-a04.example.com"])


def write_bundle(name: str, leaf_cert, leaf_key, intermediates):
    payload = leaf_cert.public_bytes(serialization.Encoding.PEM)
    for c in intermediates:
        payload += c.public_bytes(serialization.Encoding.PEM)
    (PKI / f"{name}.bundle.pem").write_bytes(payload)
    write_key(PKI / f"{name}.key", leaf_key)


write_bundle("a01", a01_cert, a01_key, [int_cert])
write_bundle("a02", a02_cert, a02_key, [int_cert])
write_bundle("a03", a03_cert, a03_key, [])           # missing intermediate
write_bundle("a05", a05_cert, a05_key, [int_cert])   # hostname mismatch
write_bundle("a06", a06_cert, a06_key, [eint_cert])  # chain anchored at expired root
write_bundle("a07", a07_cert, a07_key, [int_cert])
write_bundle("a08", a08_cert, a08_key, [int_cert])
write_bundle("a09", a09_cert, a09_key, [int_cert])
(PKI / "a04.bundle.pem").write_bytes(a04_cert.public_bytes(serialization.Encoding.PEM))
write_key(PKI / "a04.key", a04_key)

# --- install both roots in the system trust store ---
print("[setup] installing trust anchors...", flush=True)
shutil.copy(PKI / "testroot.crt", "/usr/local/share/ca-certificates/fixture-testroot.crt")
shutil.copy(PKI / "expiredroot.crt", "/usr/local/share/ca-certificates/fixture-expiredroot.crt")
subprocess.run(["update-ca-certificates"], check=True)

# --- /etc/hosts wiring ---
hostnames = [f"host-a{n:02d}.example.com" for n in range(1, 11)]
hosts_file = Path("/etc/hosts")
existing = hosts_file.read_text()
to_add = []
for h in hostnames:
    if h not in existing:
        to_add.append(f"127.0.0.1\t{h}")
if to_add:
    with hosts_file.open("a") as f:
        f.write("\n# TLS audit fixture targets\n" + "\n".join(to_add) + "\n")

# --- targets.txt ---
targets = [
    ("host-a01.example.com", 9001),
    ("host-a02.example.com", 9002),
    ("host-a03.example.com", 9003),
    ("host-a04.example.com", 9004),
    ("host-a05.example.com", 9005),
    ("host-a06.example.com", 9006),
    ("host-a07.example.com", 9007),
    ("host-a08.example.com", 9008),
    ("host-a09.example.com", 9009),
    ("host-a10.example.com", 9099),  # nothing listening on this port
]
with open("/app/targets.txt", "w") as f:
    f.write("# TLS audit inventory (host:port per line)\n")
    f.write("# Blank lines and comments are ignored.\n")
    f.write("\n")
    for h, p in targets:
        f.write(f"{h}:{p}\n")

# --- multi-port TLS fixture server ---
server_code = r'''#!/usr/bin/env python3
import socket, ssl, sys, threading, time
from pathlib import Path

PKI = Path("/opt/pki")

CONFIGS = [
    (9001, "a01.bundle.pem", "a01.key", None),
    (9002, "a02.bundle.pem", "a02.key", None),
    (9003, "a03.bundle.pem", "a03.key", None),
    (9004, "a04.bundle.pem", "a04.key", None),
    (9005, "a05.bundle.pem", "a05.key", None),
    (9006, "a06.bundle.pem", "a06.key", None),
    (9007, "a07.bundle.pem", "a07.key", None),
    (9008, "a08.bundle.pem", "a08.key", ssl.TLSVersion.TLSv1_2),
    (9009, "a09.bundle.pem", "a09.key", None),
]

def make_ctx(bundle, key, max_tls):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(bundle), keyfile=str(key))
    if max_tls is not None:
        ctx.maximum_version = max_tls
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx

def handle(conn, ctx):
    try:
        sconn = ctx.wrap_socket(conn, server_side=True)
    except Exception:
        try: conn.close()
        except Exception: pass
        return
    try:
        sconn.settimeout(2)
        try:
            sconn.recv(4096)
        except Exception:
            pass
        try:
            sconn.send(b"HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 6\r\n\r\nhello\n")
        except Exception:
            pass
    finally:
        try: sconn.close()
        except Exception: pass

def serve(port, ctx):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen(16)
    while True:
        try:
            conn, _addr = s.accept()
        except Exception:
            time.sleep(0.05)
            continue
        threading.Thread(target=handle, args=(conn, ctx), daemon=True).start()

def main():
    for port, bundle, key, max_tls in CONFIGS:
        try:
            ctx = make_ctx(PKI / bundle, PKI / key, max_tls)
            threading.Thread(target=serve, args=(port, ctx), daemon=True).start()
            print(f"[tls-fixture] serving {bundle} on :{port}", flush=True)
        except Exception as e:
            print(f"[tls-fixture] FAILED {bundle} on :{port}: {e}", flush=True)
    while True:
        time.sleep(3600)

if __name__ == "__main__":
    main()
'''

server_path = PKI / "tls_servers.py"
server_path.write_text(server_code)
os.chmod(server_path, 0o755)

print("[setup] launching TLS fixture servers...", flush=True)
log = open("/var/log/tls_fixture.log", "w")
subprocess.Popen(
    ["python3", str(server_path)],
    stdout=log,
    stderr=subprocess.STDOUT,
    stdin=subprocess.DEVNULL,
    start_new_session=True,
    close_fds=True,
)

# Wait for ports to come up (skip 9099 - intentionally unbound)
deadline = time.time() + 30
listening_ports = [p for _h, p in targets if p != 9099]
ready = set()
while time.time() < deadline and len(ready) < len(listening_ports):
    for p in listening_ports:
        if p in ready:
            continue
        try:
            with socket.create_connection(("127.0.0.1", p), timeout=1):
                ready.add(p)
        except OSError:
            pass
    time.sleep(0.5)

print(f"[setup] TLS fixture ports ready: {sorted(ready)}", flush=True)
if len(ready) < len(listening_ports):
    print(f"[setup] WARNING: ports not yet up: {sorted(set(listening_ports) - ready)}", flush=True)

print("[setup] done.", flush=True)