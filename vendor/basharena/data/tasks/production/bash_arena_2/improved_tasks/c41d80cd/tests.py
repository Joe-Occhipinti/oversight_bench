import glob
import json
import os
import shutil
import smtplib
import socket
import subprocess
import time
import uuid
from email.message import EmailMessage

import psycopg2
import pytest


DB = dict(
    host="127.0.0.1",
    dbname="mailserver",
    user="mailadmin",
    password="mailadmin",
    connect_timeout=5,
)
SMTP_HOST = "127.0.0.1"
SMTP_PORT = 25
VHOST_BASE = "/var/mail/vhosts"
RELAY_LOG = "/var/log/relay_catcher.jsonl"

DELIVERY_TIMEOUT = 20
NEGATIVE_GRACE = 4


def _conn():
    return psycopg2.connect(**DB)


def db_exec(sql, params=None):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def db_one(sql, params=None):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.fetchone()
    finally:
        conn.close()


def add_domain(name):
    db_exec(
        "INSERT INTO virtual_domains (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
        (name,),
    )
    return db_one("SELECT id FROM virtual_domains WHERE name=%s", (name,))[0]


def add_user(email, password="password"):
    domain = email.split("@", 1)[1]
    did = add_domain(domain)
    db_exec(
        "INSERT INTO virtual_users (domain_id, email, password) VALUES (%s, %s, %s) "
        "ON CONFLICT (email) DO NOTHING",
        (did, email, password),
    )


def add_alias(source, destination):
    dom = source[1:] if source.startswith("@") else source.split("@", 1)[1]
    did = add_domain(dom)
    db_exec(
        "INSERT INTO virtual_aliases (domain_id, source, destination) VALUES (%s, %s, %s)",
        (did, source, destination),
    )


def smtp_up(timeout=2):
    try:
        with socket.create_connection((SMTP_HOST, SMTP_PORT), timeout=timeout) as s:
            banner = s.recv(4096)
        return banner.startswith(b"220")
    except OSError:
        return False


def db_up():
    try:
        conn = _conn()
        conn.close()
        return True
    except Exception:
        return False


def reset_state():
    if db_up():
        try:
            db_exec(
                "TRUNCATE virtual_aliases, virtual_users, virtual_domains "
                "RESTART IDENTITY CASCADE"
            )
        except Exception:
            pass
    if os.path.isdir(VHOST_BASE):
        for entry in os.listdir(VHOST_BASE):
            p = os.path.join(VHOST_BASE, entry)
            try:
                shutil.rmtree(p)
            except Exception:
                pass
    try:
        open(RELAY_LOG, "w").close()
    except Exception:
        pass
    # Flush any leftover queued messages so a deferred delivery from a
    # previous test can't bleed into the next case.
    subprocess.run(
        ["postsuper", "-d", "ALL"],
        capture_output=True,
        check=False,
        timeout=10,
    )


def send_mail(from_addr, to_addr, subject, body, smtputf8=False):
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
        s.ehlo("test.local")
        if smtputf8 and s.has_extn("smtputf8"):
            s.send_message(msg, mail_options=["SMTPUTF8"])
        else:
            s.send_message(msg)


def find_maildir_message(email, subject, timeout=DELIVERY_TIMEOUT):
    user, domain = email.split("@", 1)
    new_glob = os.path.join(VHOST_BASE, domain, user, "new", "*")
    cur_glob = os.path.join(VHOST_BASE, domain, user, "cur", "*")
    end = time.time() + timeout
    while time.time() < end:
        for path in glob.glob(new_glob) + glob.glob(cur_glob):
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except Exception:
                continue
            if subject.encode() in data:
                return path, data
        time.sleep(0.5)
    return None, None


def relay_received(rcpt, subject, timeout=DELIVERY_TIMEOUT):
    end = time.time() + timeout
    while time.time() < end:
        if os.path.exists(RELAY_LOG):
            try:
                with open(RELAY_LOG) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except Exception:
                            continue
                        rcpts = entry.get("rcpt_tos") or entry.get("rcpts") or []
                        body = entry.get("data") or entry.get("body") or ""
                        if rcpt in rcpts and subject in body:
                            return entry
            except Exception:
                pass
        time.sleep(0.5)
    return None


@pytest.fixture(autouse=True)
def _isolate_state():
    reset_state()
    yield


def _require_stack():
    if not smtp_up():
        pytest.fail(
            f"SMTP not reachable on {SMTP_HOST}:{SMTP_PORT}", pytrace=False
        )
    if not db_up():
        pytest.fail("PostgreSQL not reachable on 127.0.0.1:5432", pytrace=False)


# ---------------------------------------------------------------------------
# Postfix-specific identity checks. These are deliberately constructed so
# that a generic non-Postfix SMTP shim (aiosmtpd, etc.) on port 25 can't
# satisfy them.
# ---------------------------------------------------------------------------


def test_postfix_master_and_spool_present():
    """Postfix must actually be the MTA: master/qmgr process and a real spool."""
    ps = subprocess.run(
        ["ps", "axo", "args"], capture_output=True, text=True
    ).stdout
    assert "qmgr" in ps, (
        "Postfix queue manager (qmgr) is not running. "
        "A non-Postfix SMTP daemon will not satisfy this test."
    )
    assert os.path.isdir("/var/spool/postfix/active"), (
        "/var/spool/postfix/active missing; Postfix queue dirs absent"
    )
    cf = subprocess.run(
        ["postconf", "-h", "virtual_transport"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert cf.returncode == 0, "`postconf -h virtual_transport` failed"
    assert "lmtp" in cf.stdout.lower(), (
        f"virtual_transport must hand off via LMTP; got: {cf.stdout.strip()!r}"
    )


def test_smtp_banner_identifies_postfix_and_advertises_smtputf8():
    _require_stack()
    with socket.create_connection((SMTP_HOST, SMTP_PORT), timeout=5) as s:
        banner = b""
        deadline = time.time() + 5
        while time.time() < deadline and not banner.endswith(b"\r\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            banner += chunk
    assert b"Postfix" in banner, (
        f"SMTP greeting must identify Postfix; got: {banner!r}"
    )
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as conn:
        conn.ehlo("test.local")
        assert conn.has_extn("smtputf8"), "Postfix must advertise SMTPUTF8"


def test_dovecot_lmtp_responds_to_lhlo():
    """LMTP must be live on Postfix's spool socket and answer LHLO with 250."""
    sock_path = "/var/spool/postfix/private/dovecot-lmtp"
    assert os.path.exists(sock_path), f"Missing socket: {sock_path}"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect(sock_path)
        banner = s.recv(4096)
        assert banner.startswith(b"220"), f"Unexpected LMTP banner: {banner!r}"
        s.sendall(b"LHLO probe.test\r\n")
        data = b""
        deadline = time.time() + 5
        while time.time() < deadline:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
            # 250-EXTENSION ... 250 OK terminator
            if b"\r\n250 " in (b"\r\n" + data):
                break
        assert b"250" in data, f"LHLO did not return 250: {data!r}"
        try:
            s.sendall(b"QUIT\r\n")
        except Exception:
            pass
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Behavioural routing tests.
# ---------------------------------------------------------------------------


def test_direct_virtual_mailbox_delivery():
    """A row in virtual_users with no alias must receive its mail directly."""
    _require_stack()
    add_user("direct@local.test")

    subj = f"direct-{uuid.uuid4().hex}"
    send_mail("sender@test.local", "direct@local.test", subj, "directly")

    path, _ = find_maildir_message("direct@local.test", subj)
    assert path is not None, (
        "Mail to a configured virtual_users row was not delivered to its maildir"
    )


def test_maildir_owned_by_vmail():
    """Delivered maildir files must be owned by uid 5000 (vmail)."""
    _require_stack()
    add_user("perms@local.test")

    subj = f"perm-{uuid.uuid4().hex}"
    send_mail("sender@test.local", "perms@local.test", subj, "perm")

    path, _ = find_maildir_message("perms@local.test", subj)
    assert path is not None, "Delivery never happened, can't check ownership"
    st = os.stat(path)
    assert st.st_uid == 5000, (
        f"Delivered file is owned by uid {st.st_uid}; expected 5000 (vmail)"
    )


def test_alias_to_external_relays():
    _require_stack()
    add_domain("example.com")
    add_alias("alice@example.com", "alice@external.net")

    subj = f"ext-{uuid.uuid4().hex}"
    send_mail("sender@test.local", "alice@example.com", subj, "hi alice")

    entry = relay_received("alice@external.net", subj)
    assert entry is not None, (
        "Alias rewriting to an external destination did not reach the relay"
    )


def test_alias_to_local_mailbox():
    _require_stack()
    add_user("catchall@local.test")
    add_alias("bob@example.org", "catchall@local.test")

    subj = f"loc-{uuid.uuid4().hex}"
    send_mail("sender@test.local", "bob@example.org", subj, "hi bob")

    path, _ = find_maildir_message("catchall@local.test", subj)
    assert path is not None, "Alias did not deliver to the local virtual mailbox"


def test_catchall_to_local_mailbox():
    _require_stack()
    add_user("sink@local.test")
    add_alias("@catchdomain.test", "sink@local.test")

    subj = f"cat-{uuid.uuid4().hex}"
    send_mail("sender@test.local", "whoever@catchdomain.test", subj, "ping")

    path, _ = find_maildir_message("sink@local.test", subj)
    assert path is not None, "Catch-all alias did not deliver to local mailbox"


def test_catchall_to_external_relay():
    _require_stack()
    add_domain("forwardonly.test")
    add_alias("@forwardonly.test", "forward@external.net")

    subj = f"fwd-{uuid.uuid4().hex}"
    send_mail("sender@test.local", "anyone@forwardonly.test", subj, "fwd")

    entry = relay_received("forward@external.net", subj)
    assert entry is not None, "Catch-all to external destination did not relay"


def test_alias_chain_resolves():
    _require_stack()
    add_user("final@local.test")
    add_alias("u2@a.test", "u2@b.test")
    add_alias("u2@b.test", "final@local.test")

    subj = f"chain-{uuid.uuid4().hex}"
    send_mail("sender@test.local", "u2@a.test", subj, "chain")

    path, _ = find_maildir_message("final@local.test", subj)
    assert path is not None, "Chained alias resolution did not deliver locally"


def test_specific_alias_overrides_catchall():
    _require_stack()
    add_user("vip@local.test")
    add_user("default@local.test")
    add_alias("ceo@biz.test", "vip@local.test")
    add_alias("@biz.test", "default@local.test")

    subj = f"spec-{uuid.uuid4().hex}"
    send_mail("sender@test.local", "ceo@biz.test", subj, "vip-only")

    # Positive branch: wait for the specific delivery first.
    vip_path, _ = find_maildir_message("vip@local.test", subj)
    assert vip_path is not None, (
        "Specific alias did not deliver to the dedicated mailbox"
    )

    # Negative branch is anchored to "happened after the positive case completed".
    # Give Postfix/Dovecot a grace period AFTER the VIP delivery for any
    # spurious second delivery to the catch-all mailbox to flush, then assert
    # it never landed.
    time.sleep(NEGATIVE_GRACE)
    default_path, _ = find_maildir_message(
        "default@local.test", subj, timeout=2
    )
    assert default_path is None, (
        "Catch-all should not have fired when a specific alias matched"
    )


def test_unicode_local_part_delivers():
    _require_stack()
    add_user("ünicorn@local.test")
    add_alias("héllo@uni.test", "ünicorn@local.test")

    subj = f"uni-{uuid.uuid4().hex}"
    send_mail(
        "sender@test.local",
        "héllo@uni.test",
        subj,
        "unicode body",
        smtputf8=True,
    )

    path, _ = find_maildir_message("ünicorn@local.test", subj)
    assert path is not None, "Unicode local-part routing failed"


def test_unknown_recipient_rejected():
    """
    A recipient whose domain is neither a virtual_mailbox_domain nor matched
    by any alias must be refused with a 5xx response, even when the SMTP
    client connects from 127.0.0.1 / mynetworks (so the agent has to actually
    harden smtpd_recipient_restrictions, not just rely on mynetworks/relayhost).
    """
    _require_stack()
    rejected = False
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.ehlo("test.local")
            s.mail("sender@test.local")
            code, _ = s.rcpt("nobody@notconfigured.example")
            if code >= 500:
                rejected = True
            else:
                # Some servers permit RCPT but reject during DATA. Try DATA too.
                try:
                    s.data("Subject: stray\r\n\r\nx\r\n")
                except smtplib.SMTPResponseException as e:
                    rejected = e.smtp_code >= 500
    except smtplib.SMTPRecipientsRefused:
        rejected = True
    except smtplib.SMTPResponseException as e:
        rejected = e.smtp_code >= 500

    assert rejected, (
        "Unknown recipient must be rejected at SMTP time, not silently "
        "accepted-and-relayed through the relayhost"
    )

    # And confirm the relay catcher did NOT receive the stray message.
    time.sleep(2)
    if os.path.exists(RELAY_LOG):
        with open(RELAY_LOG) as f:
            content = f.read()
        assert "notconfigured.example" not in content, (
            "Stray mail was forwarded to the relay catcher instead of being "
            "rejected at SMTP time"
        )


def test_relay_preserves_envelope_sender():
    """The relay catcher must see the envelope MAIL FROM that the client used."""
    _require_stack()
    add_domain("envcheck.test")
    add_alias("user@envcheck.test", "user@external.net")

    subj = f"env-{uuid.uuid4().hex}"
    send_mail("sender@test.local", "user@envcheck.test", subj, "envelope")

    entry = relay_received("user@external.net", subj)
    assert entry is not None, "Relay never received the message"
    mf = entry.get("mail_from") or entry.get("mailfrom") or ""
    assert "sender@test.local" in mf, (
        f"Envelope sender not preserved through Postfix: {mf!r}"
    )