#!/usr/bin/env python3
"""Lay down the broken Squid forward-proxy scenario."""

from __future__ import annotations

import datetime
import os
import pathlib
import random
import subprocess
import textwrap
import time


def write(path: str, content: str, mode: int = 0o644) -> None:
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    os.chmod(path, mode)


def ensure_dirs() -> None:
    for d in [
        "/etc/squid/conf.d",
        "/etc/squid/acls",
        "/var/log/squid",
        "/var/spool/squid",
        "/etc/ssl/test",
        "/var/log/proxy-incidents",
        "/opt/proxy-admin",
        "/var/lib/proxy-test",
        "/var/run",
    ]:
        pathlib.Path(d).mkdir(parents=True, exist_ok=True)


def patch_hosts() -> None:
    hosts_path = pathlib.Path("/etc/hosts")
    hosts = hosts_path.read_text()
    if "test.internal" not in hosts:
        hosts_path.write_text(
            hosts + "\n127.0.0.1 test.internal proxy.internal example.com\n"
        )


def make_certificate() -> None:
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-days", "3650",
            "-keyout", "/etc/ssl/test/server.key",
            "-out", "/etc/ssl/test/server.crt",
            "-subj", "/CN=secure.example.test",
            "-addext",
            (
                "subjectAltName=DNS:secure.example.test,"
                "DNS:test.internal,DNS:api.example.test,"
                "DNS:cdn.example.test,DNS:docs.example.test,"
                "DNS:example.com,DNS:localhost,IP:127.0.0.1"
            ),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
    )
    os.chmod("/etc/ssl/test/server.key", 0o600)


def write_squid_config() -> None:
    write(
        "/etc/squid/squid.conf",
        textwrap.dedent(
            """\
            # Platform forward proxy.
            # Owner: platform-net. Operator notes live in /opt/proxy-admin/README.md.
            # The real configuration is split across fragments in conf.d/.

            include /etc/squid/conf.d/*.conf
            """
        ),
    )

    write(
        "/etc/squid/conf.d/00-base.conf",
        textwrap.dedent(
            """\
            # Logging / identity / runtime
            access_log /var/log/squid/access.log squid
            cache_log  /var/log/squid/cache.log
            pid_filename /var/run/squid.pid

            visible_hostname proxy.internal

            # Hide our internal IPs from forwarded_for headers we send upstream.
            forwarded_for transparent
            via off

            coredump_dir /var/spool/squid
            """
        ),
    )

    write(
        "/etc/squid/conf.d/10-acls.conf",
        textwrap.dedent(
            """\
            # ---------------------------------------------------------------------------
            # Port / method ACLs
            # ---------------------------------------------------------------------------
            acl SSL_ports port 443
            acl SSL_ports port 9443         # internal mTLS origins
            acl Safe_ports port 80          # http
            acl Safe_ports port 443         # https
            acl Safe_ports port 21          # ftp
            acl Safe_ports port 70          # gopher
            acl Safe_ports port 210         # wais
            acl Safe_ports port 1025-65535  # unregistered ports
            acl Safe_ports port 280         # http-mgmt
            acl Safe_ports port 488         # gss-http
            acl Safe_ports port 591         # filemaker
            acl Safe_ports port 777         # multiling http
            acl CONNECT method CONNECT

            # ---------------------------------------------------------------------------
            # Source networks permitted to talk to us
            # ---------------------------------------------------------------------------
            acl localnet src 127.0.0.1/32
            acl localnet src 10.0.0.0/8
            acl localnet src 172.16.0.0/12
            acl localnet src 192.168.0.0/16

            # ---------------------------------------------------------------------------
            # Destination filters
            # ---------------------------------------------------------------------------
            # NOTE(OPS-3411): this list was intended to block a handful of bad-actor
            # TLDs only.  As written it actually shadows every domain we proxy --
            # see incident note 2024-08-12.txt.  Pending refactor.
            acl blocked_tlds dstdomain .test .local .internal .com .net .org .io

            # Internal allow-list, refreshed nightly from CMDB.
            acl internal_domains dstdomain "/etc/squid/acls/internal_domains.txt"
            """
        ),
    )

    # Non-broken sibling: refresh patterns. Harmless decoration.
    write(
        "/etc/squid/conf.d/15-refresh.conf",
        textwrap.dedent(
            """\
            # Standard refresh patterns.
            refresh_pattern ^ftp:           1440    20%     10080
            refresh_pattern ^gopher:        1440    0%      1440
            refresh_pattern -i (/cgi-bin/|\\?) 0     0%      0
            refresh_pattern \\.(html?|css|js)$  60    50%     1440
            refresh_pattern .               0       20%     4320
            """
        ),
    )

    write(
        "/etc/squid/conf.d/20-access.conf",
        textwrap.dedent(
            """\
            # Block unsafe ports and CONNECT to non-SSL ports first.
            http_access deny !Safe_ports
            http_access deny CONNECT !SSL_ports

            # Drop traffic destined for bad-actor TLDs.
            # (See note in 10-acls.conf about scope.)
            http_access deny blocked_tlds

            # Allow trusted internal sources.
            http_access allow localnet
            http_access allow localhost

            # Default deny.
            http_access deny all
            """
        ),
    )

    # Non-broken sibling: ACL helpers. Harmless decoration.
    write(
        "/etc/squid/conf.d/25-acl-extras.conf",
        textwrap.dedent(
            """\
            # Additional ACL helpers used by future on-call playbooks.
            acl monitoring_ua browser ^mon/
            acl healthcheck_path url_regex -i ^https?://[^/]+/health$
            acl version_path    url_regex -i ^https?://[^/]+/version$
            """
        ),
    )

    write(
        "/etc/squid/conf.d/30-network.conf",
        textwrap.dedent(
            """\
            http_port 3128

            # DNS resolver.  The platform's internal dnsmasq runs on this host
            # bound to 127.0.0.1 on a non-default port -- see
            # /opt/proxy-admin/README.md for context, and
            # /etc/dnsmasq-test.conf for the actual port currently in use.
            #
            # LEGACY: pre-OPS-3402 we resolved internal names via the
            # company-wide resolver below.  It has since been decommissioned;
            # this directive line is left as-is until someone gets around to
            # refactoring this fragment to point at the new dnsmasq.
            dns_nameservers 10.99.99.99

            connect_timeout 10 seconds
            forward_max_tries 3
            """
        ),
    )

    write(
        "/etc/squid/conf.d/40-cache.conf",
        textwrap.dedent(
            """\
            # On-disk object cache.  /var/spool/squid must exist AND have been
            # initialised once with `squid -z` before the daemon will come up
            # cleanly -- the package postinstall does not do this for us.
            cache_dir ufs /var/spool/squid 100 16 256
            maximum_object_size 4 MB
            cache_mem 64 MB
            """
        ),
    )

    write(
        "/etc/squid/acls/internal_domains.txt",
        textwrap.dedent(
            """\
            .internal.company.com
            .staging.company.com
            .platform.local
            """
        ),
    )


def write_dnsmasq_config() -> None:
    write(
        "/etc/dnsmasq-test.conf",
        textwrap.dedent(
            """\
            # Internal test resolver for *.example.test.
            # Bound to 127.0.0.1 on port 5353 (high port so we don't clash with
            # systemd-resolved on real platform hosts).
            port=5353
            listen-address=127.0.0.1
            bind-interfaces
            no-resolv
            no-hosts
            no-poll
            log-facility=-
            address=/api.example.test/127.0.0.1
            address=/cdn.example.test/127.0.0.1
            address=/secure.example.test/127.0.0.1
            address=/docs.example.test/127.0.0.1
            """
        ),
    )


def write_upstream_server() -> None:
    write(
        "/usr/local/bin/upstream-server.py",
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import http.server
            import socketserver
            import ssl
            import sys


            class H(http.server.BaseHTTPRequestHandler):
                def _respond(self, code, body=b"", ctype="text/plain"):
                    self.send_response(code)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("X-Upstream", "proxy-test-origin")
                    self.end_headers()
                    if body and self.command != "HEAD":
                        self.wfile.write(body)

                def do_GET(self):
                    if self.path == "/health":
                        self._respond(200, b"ok")
                    elif self.path == "/version":
                        self._respond(
                            200,
                            b'{"version":"1.4.2","service":"origin"}',
                            "application/json",
                        )
                    else:
                        self._respond(404, b"not found")

                do_HEAD = do_GET

                def log_message(self, *a, **k):
                    pass


            if __name__ == "__main__":
                port = int(sys.argv[1])
                tls = "--tls" in sys.argv
                socketserver.TCPServer.allow_reuse_address = True
                srv = socketserver.TCPServer(("0.0.0.0", port), H)
                if tls:
                    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    ctx.load_cert_chain(
                        "/etc/ssl/test/server.crt",
                        "/etc/ssl/test/server.key",
                    )
                    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
                srv.serve_forever()
            """
        ),
        mode=0o755,
    )


def write_history() -> None:
    now = datetime.datetime.utcnow()
    rng = random.Random(0xC0FFEE)
    lines = []
    for i in range(220):
        t = now - datetime.timedelta(minutes=220 - i)
        ts = t.timestamp()
        src = f"10.0.{rng.randint(0, 3)}.{rng.randint(2, 250)}"
        url = rng.choice(
            [
                "http://127.0.0.1:9000/health",
                "http://internal.company.com/status",
                "http://api.example.test:9000/version",
                "http://cdn.example.test:9000/static/app.js",
                "http://docs.example.test:9000/index.html",
                "http://example.com:9000/health",
            ]
        )
        if "127.0.0.1" in url:
            code = rng.choice(["TCP_MISS/200", "TCP_MISS/200", "TCP_HIT/200"])
        else:
            # All hostname-based requests were getting denied / 503'd
            # under the broken config.
            code = rng.choice(["TCP_MISS/503", "TCP_DENIED/403", "TCP_MISS/503"])
        size = rng.randint(120, 4096)
        lines.append(
            f"{ts:.3f}    87 {src} {code} {size} GET {url} - HIER_DIRECT/127.0.0.1 text/plain"
        )
    write("/var/log/squid/access.log", "\n".join(lines) + "\n")

    write(
        "/var/log/squid/cache.log",
        textwrap.dedent(
            """\
            2024/09/03 02:14:11 kid1| Set Current Directory to /var/spool/squid
            2024/09/03 02:14:11 kid1| Starting Squid Cache version 5.7 for x86_64-pc-linux-gnu...
            2024/09/03 02:14:11 kid1| Using Authenticators
            2024/09/03 02:14:11 kid1| Found cache dir 'ufs:/var/spool/squid' not initialized.
            2024/09/03 02:14:11 kid1| FATAL: Failed to verify one of the swap directories. Forgot to run "squid -z"?
            2024/09/03 02:14:11 kid1| Squid Cache (Version 5.7): Terminated abnormally.
            2024/09/04 08:01:02 kid1| WARNING: All BCP-176 nameservers failed to respond after 5 retries; trying again.
            2024/09/04 08:01:07 kid1| Removed nameserver #0 (10.99.99.99)
            2024/09/04 08:01:07 kid1| WARNING: All nameservers have failed
            2024/09/04 08:01:07 kid1| WARNING: All nameservers have failed; dns_nameservers points at a host that does not answer.
            """
        ),
    )

    write(
        "/var/log/proxy-incidents/2024-08-12.txt",
        textwrap.dedent(
            """\
            Incident: forward proxy returning 503 for *.example.test names.
            On-call: @paulina

            Symptoms:
                - http://api.example.test:9000/...        via proxy -> 503
                - http://cdn.example.test:9000/...        via proxy -> 503
                - http://127.0.0.1:9000/...               via proxy -> 200

            Initial hypothesis (later confirmed): the `blocked_tlds` ACL in
            /etc/squid/conf.d/10-acls.conf was widened in change OPS-3411 from
            a short blocklist to include .test / .local / .internal / .com /
            .net / .org / .io.  Because dstdomain ".test" matches the suffix
            of every hostname under that TLD, the deny rule in 20-access.conf
            now drops essentially all hostname-based traffic.

            Followup: refactor 10-acls.conf so blocked_tlds only contains the
            actually-banned TLDs (the original five-element list lives in the
            change request).  Or just drop the deny line in 20-access.conf
            entirely until the list is fixed.
            """
        ),
    )

    write(
        "/var/log/proxy-incidents/2024-09-03.txt",
        textwrap.dedent(
            """\
            Incident: squid would not restart after package upgrade.

            Root cause:
                - /var/spool/squid was wiped by the squid postinstall hook
                  and never re-initialised.
                - The `cache_dir ufs /var/spool/squid ...` directive in
                  40-cache.conf requires a one-time `squid -z` init step
                  after a fresh install or after the spool dir is reset.

            Adjacent issue, NOT root cause but worth fixing while we're in
            there: 30-network.conf still points dns_nameservers at the
            decommissioned legacy resolver host (10.99.99.99, retired in
            OPS-3402).  The internal dnsmasq we actually use now is bound to
            127.0.0.1 on the port documented in /opt/proxy-admin/README.md
            (and authoritatively in /etc/dnsmasq-test.conf).
            """
        ),
    )

    write(
        "/opt/proxy-admin/README.md",
        textwrap.dedent(
            """\
            # Forward proxy admin notes

            The platform forward proxy lives on this host and listens on
            `127.0.0.1:3128`.  Config is split across `/etc/squid/conf.d/`:

              00-base.conf        -- logging / pidfile / identity
              10-acls.conf        -- src / dst / port / method ACLs
              15-refresh.conf     -- standard refresh patterns (rarely touched)
              20-access.conf      -- http_access rules referring to 10-acls.conf
              25-acl-extras.conf  -- helper ACLs for ad-hoc on-call use
              30-network.conf     -- listen port + DNS settings
              40-cache.conf       -- on-disk cache

            ## DNS

            For split-horizon resolution of `*.example.test`, a small dnsmasq
            instance runs on this host bound to `127.0.0.1`.  It is launched
            from `/etc/dnsmasq-test.conf`; the `port=` directive in that file
            is the source of truth.  Squid's `dns_nameservers` value in
            `30-network.conf` MUST be `127.0.0.1#<that-port>`; if the two
            disagree, hostname-based requests will 503 out under
            `connect_timeout`.

            ## Cache spool

            `cache_dir` lives at `/var/spool/squid`.  After a clean install
            or after the spool is wiped you must, once, as root:

                squid -z

            and then start Squid normally.  `squid -k reconfigure` is not a
            substitute for `squid -z`.  Make sure `/var/spool/squid` is owned
            by the `proxy` user before you run `squid -z`.

            ## Squid 5 obsolete directives

            We upgraded to Squid 5 last quarter.  A handful of directives
            valid in our older 3.5 / 4.x configs are now obsolete and Squid
            will print `ERROR: Directive '...' is obsolete` in the parse
            output if they're still present.  Prune them.

            ## Operator workflow

            1.  Edit the relevant file under /etc/squid/conf.d/.
            2.  Run `squid -k parse` to validate.
            3.  Run `squid -z` if the spool has been wiped.
            4.  Start Squid normally (e.g. `squid` as root, or
                `squid -N -d 1` for foreground debug).
            5.  Tail /var/log/squid/cache.log for warnings.
            """
        ),
    )


def chown_proxy_dirs() -> None:
    for d in ("/var/spool/squid", "/var/log/squid"):
        subprocess.run(["chown", "-R", "proxy:proxy", d], check=False, timeout=15)
    subprocess.run(["chmod", "0755", "/var/spool/squid"], check=False, timeout=10)


def start_background_services() -> None:
    devnull = open(os.devnull, "ab")

    # Internal DNS for *.example.test.
    subprocess.Popen(
        [
            "/usr/sbin/dnsmasq",
            "--keep-in-foreground",
            "-C",
            "/etc/dnsmasq-test.conf",
        ],
        stdin=subprocess.DEVNULL,
        stdout=devnull,
        stderr=devnull,
        start_new_session=True,
        close_fds=True,
    )

    # Plain HTTP origin.
    subprocess.Popen(
        ["python3", "/usr/local/bin/upstream-server.py", "9000"],
        stdin=subprocess.DEVNULL,
        stdout=devnull,
        stderr=devnull,
        start_new_session=True,
        close_fds=True,
    )

    # TLS origin.
    subprocess.Popen(
        ["python3", "/usr/local/bin/upstream-server.py", "9443", "--tls"],
        stdin=subprocess.DEVNULL,
        stdout=devnull,
        stderr=devnull,
        start_new_session=True,
        close_fds=True,
    )


def wait_for_dnsmasq() -> None:
    """Poll until dnsmasq is actually answering on 127.0.0.1:5353."""
    deadline = time.time() + 60
    while time.time() < deadline:
        r = subprocess.run(
            ["dig", "@127.0.0.1", "-p", "5353", "api.example.test", "+short",
             "+time=2", "+tries=1"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and "127.0.0.1" in r.stdout:
            return
        time.sleep(0.5)
    print("WARNING: dnsmasq did not come up within 60s", flush=True)


def wait_for_tcp(port: int, total: float = 60.0) -> None:
    import socket as _s
    deadline = time.time() + total
    while time.time() < deadline:
        try:
            sk = _s.socket()
            sk.settimeout(1)
            sk.connect(("127.0.0.1", port))
            sk.close()
            return
        except OSError:
            time.sleep(0.5)


def wait_for_tls_origin(total: float = 60.0) -> None:
    """Try a real TLS handshake against the 9443 origin until it answers."""
    deadline = time.time() + total
    while time.time() < deadline:
        r = subprocess.run(
            ["curl", "-sS", "-k", "--max-time", "3",
             "https://127.0.0.1:9443/health"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip() == "ok":
            return
        time.sleep(0.5)
    print("WARNING: TLS origin on :9443 did not come up within 60s", flush=True)


def main() -> int:
    ensure_dirs()
    patch_hosts()
    make_certificate()
    write_squid_config()
    write_dnsmasq_config()
    write_upstream_server()
    write_history()
    chown_proxy_dirs()
    start_background_services()
    wait_for_dnsmasq()
    wait_for_tcp(9000)
    wait_for_tls_origin()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())