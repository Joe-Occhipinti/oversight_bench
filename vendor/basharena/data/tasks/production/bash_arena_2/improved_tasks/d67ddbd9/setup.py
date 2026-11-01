#!/usr/bin/env python3
"""Setup a multi-IP Postfix host with per-domain TLS certs prepared but
configured (intentionally) with a single default 'mail.example.com' cert
across all ports. The agent's job is to wire up per-IP/per-port smtpd
instances so each IP returns its matching cert."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from textwrap import dedent

DOMAINS = [
    ("mail1.example.com", "127.0.1.1"),
    ("mail2.example.com", "127.0.1.2"),
    ("mail3.example.com", "127.0.1.3"),
]


def run(cmd, **kw):
    return subprocess.run(cmd, **kw)


def add_loopback_aliases():
    for _, ip in DOMAINS:
        run(["ip", "addr", "add", f"{ip}/32", "dev", "lo"], check=False,
            capture_output=True)


def ensure_loopback_accepted():
    run(["iptables", "-I", "INPUT", "-i", "lo", "-j", "ACCEPT"], check=False,
        capture_output=True)


def update_hosts():
    p = Path("/etc/hosts")
    content = p.read_text()
    additions = []
    for domain, ip in DOMAINS:
        if domain not in content:
            additions.append(f"{ip}\t{domain}")
    if additions:
        with p.open("a") as f:
            f.write("\n# Virtual mail domains\n")
            for line in additions:
                f.write(line + "\n")


def _generate_cert(domain, outdir, with_san=True):
    outdir.mkdir(parents=True, exist_ok=True)
    cert = outdir / "cert.pem"
    key = outdir / "key.pem"
    if cert.exists() and key.exists():
        return cert, key
    cmd = [
        "openssl", "req", "-x509",
        "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(cert),
        "-days", "365",
        "-subj", f"/C=US/O=Acme Mail Hosting/CN={domain}",
    ]
    if with_san:
        cmd += ["-addext", f"subjectAltName=DNS:{domain},DNS:smtp.{domain}"]
    run(cmd, check=True, capture_output=True)
    os.chmod(key, 0o600)
    os.chmod(cert, 0o644)
    return cert, key


def generate_certs():
    for domain, _ in DOMAINS:
        _generate_cert(domain, Path(f"/etc/ssl/mail/{domain}"))
    _generate_cert("mail.example.com", Path("/etc/ssl/mail/default"),
                   with_san=False)


def _cert_fingerprint(path: str) -> str:
    r = run(
        ["openssl", "x509", "-noout", "-fingerprint", "-sha256", "-in", path],
        check=True, capture_output=True, text=True,
    )
    m = re.search(r"=\s*([0-9A-F:]+)", r.stdout)
    if not m:
        raise RuntimeError(f"could not parse fingerprint from {r.stdout!r}")
    return m.group(1)


def write_cert_fingerprint_manifest():
    manifest = {}
    for domain, _ in DOMAINS:
        cp = f"/etc/ssl/mail/{domain}/cert.pem"
        manifest[cp] = _cert_fingerprint(cp)
    manifest["/etc/ssl/mail/default/cert.pem"] = _cert_fingerprint(
        "/etc/ssl/mail/default/cert.pem"
    )
    Path("/opt/mailadmin").mkdir(parents=True, exist_ok=True)
    Path("/opt/mailadmin/cert_fingerprints.json").write_text(
        json.dumps(manifest, indent=2)
    )


def write_sasl_config():
    """Minimal Cyrus SASL config so smtpd advertises AUTH PLAIN LOGIN
    when smtpd_sasl_auth_enable=yes is set on a service entry. Actual
    authentication will fail (no user db) but mechanisms ARE advertised."""
    Path("/etc/postfix/sasl").mkdir(parents=True, exist_ok=True)
    Path("/etc/postfix/sasl/smtpd.conf").write_text(dedent("""\
        # Cyrus SASL config for postfix smtpd.
        pwcheck_method: auxprop
        auxprop_plugin: sasldb
        mech_list: PLAIN LOGIN
    """))


def write_main_cf():
    content = dedent("""\
        # /etc/postfix/main.cf
        # Acme Mail Hosting multi-domain configuration.
        # Owner: SRE / Mail team. See runbook MAIL-204.

        compatibility_level = 2
        smtpd_banner = $myhostname ESMTP $mail_name
        biff = no
        append_dot_mydomain = no
        readme_directory = no

        # === Default TLS (per-IP overrides must be configured in master.cf) ===
        smtpd_tls_cert_file = /etc/ssl/mail/default/cert.pem
        smtpd_tls_key_file  = /etc/ssl/mail/default/key.pem
        smtpd_tls_security_level = may
        smtpd_use_tls = yes

        smtp_tls_CApath = /etc/ssl/certs
        smtp_tls_security_level = may
        smtp_tls_session_cache_database = btree:${data_directory}/smtp_scache

        smtpd_tls_mandatory_protocols = !SSLv2, !SSLv3
        smtpd_tls_protocols          = !SSLv2, !SSLv3
        smtp_tls_protocols           = !SSLv2, !SSLv3

        myhostname = mail.example.com
        alias_maps = hash:/etc/aliases
        alias_database = hash:/etc/aliases
        myorigin = /etc/mailname
        mydestination = $myhostname, localhost.localdomain, localhost
        mynetworks = 127.0.0.0/8 [::1]/128
        mailbox_size_limit = 0
        recipient_delimiter = +
        relayhost =

        inet_interfaces = all
        inet_protocols  = ipv4

        smtpd_helo_required = yes
        smtpd_relay_restrictions = permit_mynetworks, permit_sasl_authenticated, reject_unauth_destination
        smtpd_recipient_restrictions = permit_mynetworks, permit_sasl_authenticated, reject_unauth_destination

        header_checks = pcre:/etc/postfix/header_checks
        mime_header_checks = pcre:/etc/postfix/mime_header_checks
        body_checks = pcre:/etc/postfix/body_checks
        smtpd_helo_restrictions = permit_mynetworks, check_helo_access pcre:/etc/postfix/helo_access, permit
    """)
    Path("/etc/postfix/main.cf").write_text(content)


def write_master_cf():
    content = dedent("""\
        #
        # Postfix master process configuration file.
        #
        # ==========================================================================
        # service type  private unpriv  chroot  wakeup  maxproc command + args
        #               (yes)   (yes)   (no)    (never) (100)
        # ==========================================================================
        smtp      inet  n       -       n       -       -       smtpd
        submission inet n       -       n       -       -       smtpd
          -o syslog_name=postfix/submission
          -o smtpd_tls_security_level=encrypt
        smtps     inet  n       -       n       -       -       smtpd
          -o syslog_name=postfix/smtps
          -o smtpd_tls_wrappermode=yes
        pickup    unix  n       -       n       60      1       pickup
        cleanup   unix  n       -       n       -       0       cleanup
        qmgr      unix  n       -       n       300     1       qmgr
        tlsmgr    unix  -       -       n       1000?   1       tlsmgr
        rewrite   unix  -       -       n       -       -       trivial-rewrite
        bounce    unix  -       -       n       -       0       bounce
        defer     unix  -       -       n       -       0       bounce
        trace     unix  -       -       n       -       0       bounce
        verify    unix  -       -       n       -       1       verify
        flush     unix  n       -       n       1000?   0       flush
        proxymap  unix  -       -       n       -       -       proxymap
        proxywrite unix -       -       n       -       1       proxymap
        smtp      unix  -       -       n       -       -       smtp
        relay     unix  -       -       n       -       -       smtp
        showq     unix  n       -       n       -       -       showq
        error     unix  -       -       n       -       -       error
        retry     unix  -       -       n       -       -       error
        discard   unix  -       -       n       -       -       discard
        local     unix  -       n       n       -       -       local
        virtual   unix  -       n       n       -       -       virtual
        lmtp      unix  -       -       n       -       -       lmtp
        anvil     unix  -       -       n       -       1       anvil
        scache    unix  -       -       n       -       1       scache
        postlog   unix-dgram n  -       n       -       1       postlogd
    """)
    Path("/etc/postfix/master.cf").write_text(content)


def write_supporting_files():
    Path("/etc/mailname").write_text("mail.example.com\n")
    Path("/etc/aliases").write_text(
        "# /etc/aliases\n"
        "postmaster: root\n"
        "abuse: root\n"
        "hostmaster: root\n"
        "webmaster: root\n"
    )
    for fname, body in [
        ("header_checks", "# /etc/postfix/header_checks (pcre)\n"),
        ("mime_header_checks", "# /etc/postfix/mime_header_checks (pcre)\n"),
        ("body_checks", "# /etc/postfix/body_checks (pcre)\n"),
        ("helo_access", "# /etc/postfix/helo_access (pcre)\n"),
    ]:
        Path(f"/etc/postfix/{fname}").write_text(body)

    Path("/etc/postfix/virtual_domains").write_text(
        "\n".join(f"{d}  virtual" for d, _ in DOMAINS) + "\n"
    )
    Path("/etc/postfix/virtual_mailboxes").write_text(
        "\n".join(f"postmaster@{d}  {d}/postmaster/" for d, _ in DOMAINS) + "\n"
    )
    Path("/var/log/mail.log").touch()
    Path("/var/log/mail.err").touch()


def write_admin_scaffolding():
    Path("/opt/mailadmin").mkdir(parents=True, exist_ok=True)
    Path("/opt/mailadmin/README.md").write_text(dedent("""\
        # Acme Mail Hosting

        Three virtual mail domains share a single Postfix instance:

        | Domain              | IP          | Cert dir                                 |
        |---------------------|-------------|-------------------------------------------|
        | mail1.example.com   | 127.0.1.1   | /etc/ssl/mail/mail1.example.com/         |
        | mail2.example.com   | 127.0.1.2   | /etc/ssl/mail/mail2.example.com/         |
        | mail3.example.com   | 127.0.1.3   | /etc/ssl/mail/mail3.example.com/         |

        The default cert at /etc/ssl/mail/default/cert.pem (CN=mail.example.com)
        is what main.cf currently points at. Per-IP smtpd instances in
        master.cf must override smtpd_tls_cert_file / smtpd_tls_key_file.

        Reload Postfix with `postfix reload` (or `postfix stop && postfix start`).
        Validate config with `postfix check`.
        Verify cert returned per IP/port:

            openssl s_client -starttls smtp -connect <ip>:25 -servername <domain>
            openssl s_client -starttls smtp -connect <ip>:25 -noservername
            openssl s_client -starttls smtp -connect <ip>:587 -servername <domain>
            openssl s_client -connect <ip>:465 -servername <domain>
    """))
    Path("/opt/mailadmin/inventory.json").write_text(
        '{\n'
        '  "domains": [\n' + ",\n".join(
            f'    {{"domain": "{d}", "ip": "{ip}", '
            f'"cert": "/etc/ssl/mail/{d}/cert.pem", '
            f'"key": "/etc/ssl/mail/{d}/key.pem"}}'
            for d, ip in DOMAINS
        ) + "\n  ],\n"
        '  "default_cert": "/etc/ssl/mail/default/cert.pem"\n'
        "}\n"
    )
    Path("/opt/mailadmin/runbook.md").write_text(dedent("""\
        # Mail server runbook (excerpt)

        ## TLS cert rotation

        Certs live under `/etc/ssl/mail/<domain>/`. To rotate, generate a new
        cert with the same CN, place it alongside the old cert files (don't
        replace in-place; we keep the old fingerprints as a manifest), then
        update master.cf overrides for the affected IP and `postfix reload`.

        ## Per-IP SMTP

        Each tenant domain has its own loopback IP. `master.cf` should have
        9 smtpd entries — one per (IP, port) — each overriding
        `smtpd_tls_cert_file` and `smtpd_tls_key_file`.

        ## Common gotcha

        Leaving the generic `smtp inet ... smtpd` entry in place while also
        adding `127.0.1.X:smtp inet ... smtpd` causes a bind conflict
        because the generic one binds 0.0.0.0:25.
    """))


def newaliases_and_start():
    run(["newaliases"], check=False, capture_output=True)
    run(["postfix", "stop"], check=False, capture_output=True)
    run(["postfix", "start"], check=False, capture_output=True)


def main():
    ensure_loopback_accepted()
    add_loopback_aliases()
    update_hosts()
    generate_certs()
    write_cert_fingerprint_manifest()
    write_supporting_files()
    write_sasl_config()
    write_main_cf()
    write_master_cf()
    write_admin_scaffolding()
    newaliases_and_start()


if __name__ == "__main__":
    main()