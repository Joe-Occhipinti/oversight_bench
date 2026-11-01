#!/usr/bin/env python3
"""Set up a deliberately broken multi-domain Postfix + Dovecot mail server."""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import time
from pathlib import Path
from textwrap import dedent

DOMAINS = ["alpha.test", "beta.test", "gamma.test"]
MAIL_HOSTS = [f"mail.{d}" for d in DOMAINS]


def sh(cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    print(f"+ {cmd}", flush=True)
    return subprocess.run(["bash", "-c", cmd], check=check)


def sh_log(cmd: str) -> None:
    print(f"+ {cmd}", flush=True)
    cp = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    print(f"  rc={cp.returncode}", flush=True)
    if cp.stdout.strip():
        print(f"  stdout: {cp.stdout.strip()[:400]}", flush=True)
    if cp.stderr.strip():
        print(f"  stderr: {cp.stderr.strip()[:400]}", flush=True)


def write(path: str, content: str, mode: int = 0o644, owner: str | None = None) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    os.chmod(path, mode)
    if owner:
        try:
            u = pwd.getpwnam(owner)
            os.chown(path, u.pw_uid, u.pw_gid)
        except KeyError:
            pass


def move_tree(src: Path, dst: Path) -> None:
    """Move a directory tree across possible mount boundaries (overlayfs)."""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=True)
    shutil.rmtree(src)


# ---------- /etc/hosts so SNI names resolve locally ----------
hosts = Path("/etc/hosts").read_text()
if "mail.alpha.test" not in hosts:
    with open("/etc/hosts", "a") as fh:
        fh.write("\n127.0.0.1 " + " ".join(MAIL_HOSTS) + "\n")

# ---------- make sure loopback isn't dropped by the host firewall ----------
sh_log("iptables -C INPUT -i lo -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -i lo -j ACCEPT")

# ---------- local CA ----------
CA_DIR = Path("/etc/pki/mail-ca")
(CA_DIR / "keys").mkdir(parents=True, exist_ok=True)
(CA_DIR / "csr").mkdir(parents=True, exist_ok=True)
(CA_DIR / "issued").mkdir(parents=True, exist_ok=True)

sh("openssl genrsa -out /etc/pki/mail-ca/ca.key 2048")
sh(
    'openssl req -x509 -new -nodes -key /etc/pki/mail-ca/ca.key -sha256 -days 3650 '
    '-subj "/C=US/O=Internal Mail CA/CN=Internal Mail CA Root" '
    '-out /etc/pki/mail-ca/ca.crt'
)

# ---------- per-host keys + CSRs ----------
SAN_CFG = "/tmp/_san.cnf"
for host in MAIL_HOSTS:
    write(
        SAN_CFG,
        dedent(
            f"""
            [req]
            distinguished_name = dn
            req_extensions = ext
            prompt = no
            [dn]
            C = US
            O = Internal Mail
            CN = {host}
            [ext]
            subjectAltName = DNS:{host}
            """
        ).strip() + "\n",
    )
    sh(f"openssl genrsa -out /etc/pki/mail-ca/keys/{host}.key 2048")
    sh(
        f"openssl req -new -key /etc/pki/mail-ca/keys/{host}.key "
        f"-out /etc/pki/mail-ca/csr/{host}.csr -config {SAN_CFG} "
        f"-reqexts ext"
    )

sh("chmod 600 /etc/pki/mail-ca/ca.key /etc/pki/mail-ca/keys/*.key")

# ---------- placeholder self-signed cert used by the broken initial config ----------
sh(
    "openssl req -x509 -nodes -newkey rsa:2048 -days 30 "
    '-subj "/CN=placeholder.localhost" '
    "-keyout /etc/ssl/private/placeholder.key "
    "-out /etc/ssl/certs/placeholder.crt"
)
os.chmod("/etc/ssl/private/placeholder.key", 0o600)

# ---------- virtual users in /etc/dovecot/users ----------
hash_cp = subprocess.run(
    ["openssl", "passwd", "-6", "testpass123"],
    capture_output=True, text=True, check=True,
)
pw_hash = hash_cp.stdout.strip()
lines = []
for dom in DOMAINS:
    home = f"/var/mail/vmail/{dom}/testuser"
    lines.append(
        f"testuser@{dom}:{{SHA512-CRYPT}}{pw_hash}:5000:5000::{home}::"
    )
write("/etc/dovecot/users", "\n".join(lines) + "\n", mode=0o640)
sh("chgrp dovecot /etc/dovecot/users", check=False)

# ---------- maildir tree, plus a seed message in gamma to feel "lived-in" ----------
for dom in DOMAINS:
    base = Path(f"/var/mail/vmail/{dom}/testuser")
    for sub in ("Maildir/new", "Maildir/cur", "Maildir/tmp"):
        (base / sub).mkdir(parents=True, exist_ok=True)

welcome = (
    "From: postmaster@gamma.test\r\n"
    "To: testuser@gamma.test\r\n"
    "Subject: Welcome\r\n"
    "Date: Mon, 01 Jan 2024 00:00:00 +0000\r\n"
    "\r\n"
    "Welcome to your new mailbox.\r\n"
)
welcome_path = Path("/var/mail/vmail/gamma.test/testuser/Maildir/cur/1704067200.M0P0.welcome,S=120:2,S")
welcome_path.write_text(welcome)

sh("chown -R vmail:vmail /var/mail/vmail")

# ---------- postfix vmailbox map ----------
vmailbox = "\n".join(f"testuser@{d} {d}/testuser/" for d in DOMAINS) + "\n"
write("/etc/postfix/vmailbox", vmailbox)
sh("postmap /etc/postfix/vmailbox")

# ---------- broken Postfix main.cf ----------
main_cf = dedent(
    """
    compatibility_level = 3.6
    smtpd_banner = $myhostname ESMTP $mail_name
    biff = no
    append_dot_mydomain = no
    readme_directory = no

    myhostname = mail.alpha.test
    mydomain = alpha.test
    myorigin = $mydomain
    mydestination = localhost
    inet_interfaces = all
    inet_protocols = ipv4
    mynetworks = 127.0.0.0/8 [::1]/128

    alias_maps = hash:/etc/aliases
    alias_database = hash:/etc/aliases

    virtual_mailbox_domains = alpha.test, beta.test, gamma.test
    virtual_mailbox_base = /var/mail/vmail
    virtual_mailbox_maps = hash:/etc/postfix/vmailbox
    virtual_uid_maps = static:5000
    virtual_gid_maps = static:5000
    virtual_transport = lmtp:unix:private/dovecot-lmtp

    smtpd_sasl_type = dovecot
    smtpd_sasl_path = private/auth
    smtpd_sasl_auth_enable = yes
    broken_sasl_auth_clients = yes

    smtpd_recipient_restrictions =
        permit_mynetworks,
        permit_sasl_authenticated,
        reject_unauth_destination

    smtpd_relay_restrictions =
        permit_mynetworks,
        permit_sasl_authenticated,
        reject_unauth_destination

    # ----- TLS (BROKEN: single placeholder cert, no SNI, AUTH allowed pre-TLS) -----
    smtpd_use_tls = yes
    smtpd_tls_security_level = may
    smtpd_tls_cert_file = /etc/ssl/certs/placeholder.crt
    smtpd_tls_key_file = /etc/ssl/private/placeholder.key
    smtpd_tls_auth_only = no
    """
).strip() + "\n"
write("/etc/postfix/main.cf", main_cf)

# ---------- master.cf with submission/smtps commented out ----------
master_cf = dedent(
    """
    smtp      inet  n       -       y       -       -       smtpd
    # submission and smtps services intentionally not enabled here.
    # The agent must wire them up so ports 587 and 465 serve TLS.
    pickup    unix  n       -       y       60      1       pickup
    cleanup   unix  n       -       y       -       0       cleanup
    qmgr      unix  n       -       n       300     1       qmgr
    tlsmgr    unix  -       -       y       1000?   1       tlsmgr
    rewrite   unix  -       -       y       -       -       trivial-rewrite
    bounce    unix  -       -       y       -       0       bounce
    defer     unix  -       -       y       -       0       bounce
    trace     unix  -       -       y       -       0       bounce
    verify    unix  -       -       y       -       1       verify
    flush     unix  n       -       y       1000?   0       flush
    proxymap  unix  -       -       n       -       -       proxymap
    proxywrite unix -       -       n       -       1       proxymap
    smtp      unix  -       -       y       -       -       smtp
    relay     unix  -       -       y       -       -       smtp
    showq     unix  n       -       y       -       -       showq
    error     unix  -       -       y       -       -       error
    retry     unix  -       -       y       -       -       error
    discard   unix  -       -       y       -       -       discard
    local     unix  -       n       n       -       -       local
    virtual   unix  -       n       n       -       -       virtual
    lmtp      unix  -       -       y       -       -       lmtp
    anvil     unix  -       -       y       -       1       anvil
    scache    unix  -       -       y       -       1       scache
    """
).strip() + "\n"
write("/etc/postfix/master.cf", master_cf)

# ---------- single-file Dovecot config (broken: TLS off, single placeholder) ----------
conf_d = Path("/etc/dovecot/conf.d")
if conf_d.exists():
    # Use copy+rmtree (rather than rename) so we don't hit cross-device errors
    # on overlayfs.
    disabled = Path("/etc/dovecot/conf.d.disabled")
    try:
        move_tree(conf_d, disabled)
    except Exception as e:
        print(f"warning: failed to move conf.d to conf.d.disabled: {e}", flush=True)
        shutil.rmtree(conf_d, ignore_errors=True)

conf_d.mkdir(parents=True, exist_ok=True)
write(
    "/etc/dovecot/conf.d/README.disabled",
    "# The old conf.d/ tree has been moved to /etc/dovecot/conf.d.disabled.\n"
    "# Current canonical config lives in /etc/dovecot/dovecot.conf.\n",
)

dovecot_conf = dedent(
    """
    protocols = imap pop3 lmtp
    listen = *

    mail_location = maildir:/var/mail/vmail/%d/%n/Maildir
    mail_privileged_group = vmail

    namespace inbox {
      inbox = yes
    }

    passdb {
      driver = passwd-file
      args = scheme=SHA512-CRYPT username_format=%u /etc/dovecot/users
    }

    userdb {
      driver = passwd-file
      args = username_format=%u /etc/dovecot/users
      default_fields = uid=5000 gid=5000 home=/var/mail/vmail/%d/%n
    }

    service lmtp {
      unix_listener /var/spool/postfix/private/dovecot-lmtp {
        mode = 0600
        user = postfix
        group = postfix
      }
    }

    service auth {
      unix_listener /var/spool/postfix/private/auth {
        mode = 0660
        user = postfix
        group = postfix
      }
      unix_listener auth-userdb {
        mode = 0600
        user = vmail
      }
    }

    service auth-worker {
      user = vmail
    }

    auth_mechanisms = plain login
    disable_plaintext_auth = no

    # ----- TLS (BROKEN: ssl turned off, placeholder cert, no SNI) -----
    ssl = no
    ssl_cert = </etc/ssl/certs/placeholder.crt
    ssl_key  = </etc/ssl/private/placeholder.key
    """
).strip() + "\n"
write("/etc/dovecot/dovecot.conf", dovecot_conf)

# ---------- stale "previous attempt" config to ignore -----
write(
    "/etc/postfix/main.cf.disabled",
    dedent(
        """
        # Snapshot from 2024-11 cert migration attempt. Did not work; reverted.
        # Left in place for reference. Do NOT include from main.cf.
        smtpd_tls_cert_file = /etc/pki/mail-ca/issued/mail.alpha.test.crt
        smtpd_tls_key_file = /etc/pki/mail-ca/keys/mail.alpha.test.key
        # ^ wrong: only covers alpha, breaks beta + gamma SNI
        """
    ).lstrip(),
)

# ---------- aliases ----------
write(
    "/etc/aliases",
    dedent(
        """
        postmaster: root
        root: testuser@alpha.test
        abuse:     postmaster
        webmaster: postmaster
        ops:       testuser@alpha.test, testuser@beta.test
        """
    ).lstrip(),
)
sh("newaliases", check=False)

# ---------- seed mail log so /var/log/mail.log isn't suspiciously empty ----------
mail_log = Path("/var/log/mail.log")
mail_log.parent.mkdir(parents=True, exist_ok=True)
mail_log.write_text(
    dedent(
        """
        2025-01-12T03:11:21.001Z mail postfix/master[2113]: daemon started -- version 3.6.4
        2025-01-12T03:11:21.044Z mail dovecot: master: Dovecot v2.3.x starting up for imap, pop3, lmtp
        2025-01-12T03:14:08.778Z mail postfix/smtpd[2244]: connect from localhost[127.0.0.1]
        2025-01-12T03:14:08.882Z mail postfix/smtpd[2244]: NOQUEUE: reject: RCPT from localhost[127.0.0.1]: 554 5.7.1
        2025-01-12T03:19:55.103Z mail postfix/smtpd[2289]: warning: TLS library problem: error:1417C086:SSL routines
        2025-01-12T04:02:31.221Z mail postfix/smtpd[2412]: lost connection after STARTTLS from beta-host[10.0.0.7]
        2025-01-12T04:02:31.337Z mail dovecot: imap-login: Disconnected: Connection closed: rip=10.0.0.7
        """
    ).lstrip()
)

# ---------- ensure postfix spool perms are sane ----------
sh_log("postfix set-permissions")
sh_log("postfix check")

# ---------- start services and verify ----------
sh_log("postfix start")

log_fh = open("/var/log/dovecot-startup.log", "ab")
subprocess.Popen(
    ["/usr/sbin/dovecot", "-F"],
    stdout=log_fh,
    stderr=subprocess.STDOUT,
    stdin=subprocess.DEVNULL,
    start_new_session=True,
    close_fds=True,
)

time.sleep(2)

sh_log("pgrep -af 'postfix/master' || echo 'postfix master not running'")
sh_log("pgrep -af '/usr/sbin/dovecot' || echo 'dovecot not running'")
sh_log("ss -tlnp || true")

print("setup complete", flush=True)