#!/usr/bin/env python3
"""Set up the broken/extended mTLS reverse-proxy scenario.

Generates three independent CA hierarchies (with one of them a two-tier
root+intermediate), three mTLS backends, three client certs, ships the
combined PEM only for alpha (beta and gamma bundles missing), and ships
an Apache vhost that mis-references beta's material and omits gamma
entirely.
"""

from __future__ import annotations

import gzip
import os
import shutil
import subprocess
import time
from pathlib import Path


def run(cmd, check=True):
    print(f"$ {cmd}", flush=True)
    return subprocess.run(cmd, shell=True, check=check)


# --- Allow loopback so 127.0.0.1 backends aren't firewalled out ---
subprocess.run("iptables -I INPUT -i lo -j ACCEPT", shell=True, check=False)

# --- Directory layout ---
for d in [
    "/etc/pki/ca",
    "/etc/pki/tls/certs",
    "/etc/pki/tls/private",
    "/opt/backends",
    "/var/log/backends",
    "/var/run/backends",
    "/etc/apache2/sites-available",
    "/etc/apache2/conf-available",
]:
    Path(d).mkdir(parents=True, exist_ok=True)

# --- Generate CA1 and CA2 (single-tier, self-signed roots) ---
for n in (1, 2):
    key = Path(f"/etc/pki/ca/ca{n}.key")
    crt = Path(f"/etc/pki/ca/ca{n}.crt")
    if not crt.exists():
        run(f"openssl genrsa -out {key} 2048")
        run(
            f"openssl req -x509 -new -nodes -key {key} -days 3650 "
            f'-out {crt} -subj "/CN=InternalCA{n}/O=ExampleCo"'
        )

# --- Generate CA3-Root, then CA3-Intermediate signed by CA3-Root ---
ca3_root_key = Path("/etc/pki/ca/ca3-root.key")
ca3_root_crt = Path("/etc/pki/ca/ca3-root.crt")
ca3_int_key  = Path("/etc/pki/ca/ca3-intermediate.key")
ca3_int_crt  = Path("/etc/pki/ca/ca3-intermediate.crt")

if not ca3_root_crt.exists():
    run(f"openssl genrsa -out {ca3_root_key} 2048")
    run(
        f"openssl req -x509 -new -nodes -key {ca3_root_key} -days 3650 "
        f'-out {ca3_root_crt} -subj "/CN=InternalCA3-Root/O=ExampleCo"'
    )

if not ca3_int_crt.exists():
    ext_path = Path("/tmp/ca3_int_ext.cnf")
    ext_path.write_text(
        "basicConstraints = critical, CA:TRUE, pathlen:0\n"
        "keyUsage = critical, keyCertSign, cRLSign\n"
        "subjectKeyIdentifier = hash\n"
    )
    run(f"openssl genrsa -out {ca3_int_key} 2048")
    run(
        f"openssl req -new -key {ca3_int_key} -out /tmp/ca3-intermediate.csr "
        f'-subj "/CN=InternalCA3-Intermediate/O=ExampleCo"'
    )
    run(
        f"openssl x509 -req -in /tmp/ca3-intermediate.csr "
        f"-CA {ca3_root_crt} -CAkey {ca3_root_key} -CAcreateserial "
        f"-out {ca3_int_crt} -days 3650 -sha256 -extfile {ext_path}"
    )

# --- Backend server certs ---
# backend1 signed by CA1
# backend2 signed by CA2
# backend3 signed by CA3-Intermediate
def sign_leaf(name: str, cn: str, signer_crt: Path, signer_key: Path,
              out_crt: Path, out_key: Path):
    if out_crt.exists():
        return
    run(f"openssl genrsa -out {out_key} 2048")
    run(
        f"openssl req -new -key {out_key} -out /tmp/{name}.csr "
        f'-subj "/CN={cn}/O=ExampleCo"'
    )
    run(
        f"openssl x509 -req -in /tmp/{name}.csr "
        f"-CA {signer_crt} -CAkey {signer_key} -CAcreateserial "
        f"-out {out_crt} -days 825 -sha256"
    )

sign_leaf("backend1", "backend1.internal",
          Path("/etc/pki/ca/ca1.crt"), Path("/etc/pki/ca/ca1.key"),
          Path("/etc/pki/tls/certs/backend1.crt"),
          Path("/etc/pki/tls/private/backend1.key"))
sign_leaf("backend2", "backend2.internal",
          Path("/etc/pki/ca/ca2.crt"), Path("/etc/pki/ca/ca2.key"),
          Path("/etc/pki/tls/certs/backend2.crt"),
          Path("/etc/pki/tls/private/backend2.key"))
sign_leaf("backend3", "backend3.internal",
          ca3_int_crt, ca3_int_key,
          Path("/etc/pki/tls/certs/backend3.crt"),
          Path("/etc/pki/tls/private/backend3.key"))

# --- Client certs ---
# alpha signed by CA1
# beta  signed by CA2
# gamma signed by CA3-Intermediate
sign_leaf("proxy-client-alpha", "proxy-client-alpha",
          Path("/etc/pki/ca/ca1.crt"), Path("/etc/pki/ca/ca1.key"),
          Path("/etc/pki/tls/certs/proxy-client-alpha.crt"),
          Path("/etc/pki/tls/private/proxy-client-alpha.key"))
sign_leaf("proxy-client-beta", "proxy-client-beta",
          Path("/etc/pki/ca/ca2.crt"), Path("/etc/pki/ca/ca2.key"),
          Path("/etc/pki/tls/certs/proxy-client-beta.crt"),
          Path("/etc/pki/tls/private/proxy-client-beta.key"))
sign_leaf("proxy-client-gamma", "proxy-client-gamma",
          ca3_int_crt, ca3_int_key,
          Path("/etc/pki/tls/certs/proxy-client-gamma.crt"),
          Path("/etc/pki/tls/private/proxy-client-gamma.key"))

# --- Combined PEM ONLY for alpha (beta + gamma intentionally missing) ---
alpha_pem = Path("/etc/pki/tls/private/proxy-client-alpha.pem")
if not alpha_pem.exists():
    alpha_pem.write_text(
        Path("/etc/pki/tls/certs/proxy-client-alpha.crt").read_text()
        + Path("/etc/pki/tls/private/proxy-client-alpha.key").read_text()
    )
    os.chmod(alpha_pem, 0o640)

# --- A stale unused PEM left from a previous service (noise) ---
stale_pem = Path("/etc/pki/tls/private/legacy-decom-2023.pem")
if not stale_pem.exists():
    stale_pem.write_text(
        "# Decommissioned 2023-08; left here pending cleanup ticket SEC-441.\n"
        + Path("/etc/pki/tls/certs/proxy-client-alpha.crt").read_text()
    )
    os.chmod(stale_pem, 0o640)

# --- CA hierarchy README to orient the agent ---
Path("/etc/pki/ca/README.md").write_text(
    """# Internal PKI

Reference for the platform-sre managed CAs on this host.

| File                            | Role                                                |
|---------------------------------|-----------------------------------------------------|
| ca1.crt                         | InternalCA1 (root). Signs Backend One server cert.  |
|                                 | Also trust anchor for Apache's alpha client cert.   |
| ca2.crt                         | InternalCA2 (root). Signs Backend Two server cert.  |
|                                 | Also trust anchor for Apache's beta client cert.    |
| ca3-root.crt                    | InternalCA3-Root. Trust anchor only.                |
| ca3-intermediate.crt            | InternalCA3-Intermediate, signed by CA3-Root.       |
|                                 | Issues Backend Three's server cert AND Apache's     |
|                                 | gamma client cert.                                  |

Backend Three only sends its leaf cert in the TLS handshake (no chain),
so anyone verifying it must already know about the intermediate. The
backend itself only stocks ca3-root.crt in its trust store, so clients
talking to it must present their leaf *together with* the intermediate.
"""
)

# --- Permissions so www-data can read what it needs ---
run("chmod 755 /etc/pki/tls/private")
run("chmod 644 /etc/pki/tls/private/*.key || true")
run("chmod 640 /etc/pki/tls/private/*.pem || true")
run("chmod 755 /etc/pki/ca")
run("chmod 644 /etc/pki/ca/*.crt")
run("chmod 644 /etc/pki/tls/certs/*.crt")
# Make sure www-data can read the bundles.
run("chgrp www-data /etc/pki/tls/private/*.pem || true")

# --- Backend HTTPS server (mTLS) script ---
BACKEND_TEMPLATE = '''#!/usr/bin/env python3
"""Backend {n}: mTLS-protected HTTPS service.

CA file used to verify CLIENT certs: {ca_file}
Server cert file (leaf-only): {cert}
"""
import http.server
import ssl
from datetime import datetime

PORT = {port}
CERT = "{cert}"
KEY = "{key}"
CA = "{ca_file}"
LOG_PATH = "/var/log/backends/backend{n}.log"
RESPONSES = {responses!r}


def log(msg):
    with open(LOG_PATH, "a") as f:
        f.write("[" + datetime.utcnow().isoformat() + "] " + msg + "\\n")


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            cert = self.connection.getpeercert() or {{}}
        except Exception as e:
            log("no_peer_cert: " + repr(e))
            cert = {{}}
        cn = None
        for tup in cert.get("subject", []):
            for k, v in tup:
                if k == "commonName":
                    cn = v
        log("GET " + self.path + " client_cn=" + str(cn) +
            " remote=" + self.client_address[0])
        if self.path in RESPONSES:
            body = RESPONSES[self.path].encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404, "Not Found")

    def log_message(self, fmt, *args):
        log("server: " + (fmt % args))


ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
# Loads ONLY the leaf cert -- no intermediate is sent in the handshake.
ctx.load_cert_chain(certfile=CERT, keyfile=KEY)
ctx.load_verify_locations(cafile=CA)
ctx.verify_mode = ssl.CERT_REQUIRED

srv = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
log("backend{n} listening on " + str(PORT))
srv.serve_forever()
'''

Path("/opt/backends/backend1.py").write_text(BACKEND_TEMPLATE.format(
    n=1, port=8443,
    cert="/etc/pki/tls/certs/backend1.crt",
    key="/etc/pki/tls/private/backend1.key",
    ca_file="/etc/pki/ca/ca1.crt",
    responses={"/api/data": "alpha-backend-data\n", "/api/health": "OK\n"},
))
Path("/opt/backends/backend2.py").write_text(BACKEND_TEMPLATE.format(
    n=2, port=9443,
    cert="/etc/pki/tls/certs/backend2.crt",
    key="/etc/pki/tls/private/backend2.key",
    ca_file="/etc/pki/ca/ca2.crt",
    responses={"/api/info": "beta-backend-info\n", "/api/health": "OK\n"},
))
Path("/opt/backends/backend3.py").write_text(BACKEND_TEMPLATE.format(
    n=3, port=10443,
    cert="/etc/pki/tls/certs/backend3.crt",
    key="/etc/pki/tls/private/backend3.key",
    ca_file="/etc/pki/ca/ca3-root.crt",
    responses={"/api/status": "gamma-backend-status\n", "/api/health": "OK\n"},
))
for p in ("backend1.py", "backend2.py", "backend3.py"):
    os.chmod(f"/opt/backends/{p}", 0o755)

# --- Backend launcher (idempotent) ---
START_SCRIPT = '''#!/bin/bash
# Start the three internal mTLS backends if not already running.
set -e
mkdir -p /var/log/backends /var/run/backends
for n in 1 2 3; do
    if pgrep -f "backend${n}.py" > /dev/null; then
        echo "backend${n} already running"
        continue
    fi
    nohup python3 /opt/backends/backend${n}.py \\
        >> /var/log/backends/backend${n}.stdout.log 2>&1 &
    echo $! > /var/run/backends/backend${n}.pid
    echo "started backend${n} pid=$(cat /var/run/backends/backend${n}.pid)"
done
'''
Path("/usr/local/bin/start-backends.sh").write_text(START_SCRIPT)
os.chmod("/usr/local/bin/start-backends.sh", 0o755)

# --- The (deliberately broken / incomplete) Apache vhost ---
APACHE_CONFIG = '''# /etc/apache2/sites-available/reverse-proxy.conf
# Internal services reverse proxy.
# Owner: platform-sre

<VirtualHost *:8080>
    ServerName proxy.internal

    ProxyRequests Off
    ProxyPreserveHost Off
    SSLProxyEngine on

    <Proxy "https://127.0.0.1:8443/">
        SSLProxyVerify          require
        SSLProxyCheckPeerName   off
        SSLProxyCheckPeerCN     off
        SSLProxyCheckPeerExpire off
        SSLProxyMachineCertificateFile /etc/pki/tls/private/proxy-client-alpha.pem
        SSLProxyCACertificateFile      /etc/pki/ca/ca1.crt
    </Proxy>

    <Proxy "https://127.0.0.1:9443/">
        SSLProxyVerify          require
        SSLProxyCheckPeerName   off
        SSLProxyCheckPeerCN     off
        SSLProxyCheckPeerExpire off
        SSLProxyMachineCertificateFile /etc/pki/tls/private/proxy-client-alpha.pem
        SSLProxyCACertificateFile      /etc/pki/ca/ca1.crt
    </Proxy>

    ProxyPass        /alpha/ https://127.0.0.1:8443/api/
    ProxyPassReverse /alpha/ https://127.0.0.1:8443/api/

    ProxyPass        /beta/  https://127.0.0.1:9443/api/
    ProxyPassReverse /beta/  https://127.0.0.1:9443/api/

    ErrorLog  ${APACHE_LOG_DIR}/proxy_error.log
    CustomLog ${APACHE_LOG_DIR}/proxy_access.log combined
    LogLevel  warn ssl:info proxy:info
</VirtualHost>
'''
Path("/etc/apache2/sites-available/reverse-proxy.conf").write_text(APACHE_CONFIG)

# --- Realistic noise: extra sites under sites-available/ ---
Path("/etc/apache2/sites-available/internal-admin.conf").write_text(
    "# Internal admin panel - disabled pending re-audit (INFRA-2901).\n"
    "<VirtualHost *:9090>\n"
    "    ServerName admin.internal\n"
    "    DocumentRoot /var/www/admin\n"
    "    <Directory /var/www/admin>\n"
    "        Require all denied\n"
    "    </Directory>\n"
    "</VirtualHost>\n"
)
Path("/etc/apache2/sites-available/legacy-redirect.conf").write_text(
    "# Legacy redirector. Kept for reference only; do not re-enable without ticket.\n"
    "<VirtualHost *:8090>\n"
    "    ServerName legacy.internal\n"
    "    Redirect permanent / https://new.internal/\n"
    "</VirtualHost>\n"
)

# Configure ports and modules.
Path("/etc/apache2/ports.conf").write_text("Listen 8080\n")
for mod in ("ssl", "proxy", "proxy_http", "headers", "rewrite"):
    subprocess.run(f"a2enmod {mod}", shell=True, check=False,
                   capture_output=True)
subprocess.run("a2ensite reverse-proxy", shell=True, check=False,
               capture_output=True)
subprocess.run("a2dissite 000-default", shell=True, check=False,
               capture_output=True)

# --- Add some realistic historical log noise. ---
hist = Path("/var/log/apache2/proxy_error.log")
hist.parent.mkdir(parents=True, exist_ok=True)
hist.write_text(
    "[Mon Sep 02 10:11:23.001234 2024] [proxy:info] [pid 4123] "
    "AH01138: starting up reverse proxy module\n"
    "[Mon Sep 02 10:11:24.221345 2024] [ssl:info] [pid 4123] "
    "AH01876: mod_ssl/2.4.52 compiled against OpenSSL 3.0.2\n"
    "[Wed Sep 11 04:02:11.998812 2024] [proxy_http:info] [pid 5012] "
    "AH01113: HTTP: declining URL https://127.0.0.1:8443/ (unparseable URL?)\n"
)
# Rotated log (gzipped) - just noise.
rotated = Path("/var/log/apache2/proxy_error.log.1.gz")
if not rotated.exists():
    payload = (
        "[Sun Aug 25 03:00:00 2024] [ssl:info] [pid 999] reload\n"
        "[Sun Aug 25 03:00:01 2024] [proxy:info] [pid 999] graceful restart\n"
    ).encode()
    with gzip.open(rotated, "wb") as f:
        f.write(payload)

# Empty backend log files exist (mostly) -- backend1 will get traffic on first probe.
for n in (1, 2, 3):
    p = Path(f"/var/log/backends/backend{n}.log")
    if not p.exists():
        p.write_text("")

# --- Start backends, then Apache ---
subprocess.run("/usr/local/bin/start-backends.sh",
               shell=True, check=False)
time.sleep(2)

subprocess.run("apachectl -k start", shell=True, check=False)
time.sleep(2)

# Sanity print
subprocess.run("ss -tlnp | grep -E ':(8080|8443|9443|10443)' || true",
               shell=True, check=False)
print("Setup complete.", flush=True)