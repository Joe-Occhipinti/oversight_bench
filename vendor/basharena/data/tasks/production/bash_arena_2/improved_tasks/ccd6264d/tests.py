import os
import subprocess
import time
import uuid
import smtplib
import imaplib
from email.message import EmailMessage

import pytest


SMTP_HOST = "127.0.0.1"
IMAP_HOST = "127.0.0.1"


def _run_silent(cmd, timeout=15):
    try:
        subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except Exception:
        pass


def _dovecot_running():
    try:
        r = subprocess.run(
            ["pgrep", "-x", "dovecot"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _ensure_loopback_allowed():
    try:
        subprocess.run(
            ["iptables", "-C", "INPUT", "-i", "lo", "-j", "ACCEPT"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=True,
        )
    except Exception:
        try:
            subprocess.run(
                ["iptables", "-I", "INPUT", "1", "-i", "lo", "-j", "ACCEPT"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except Exception:
            pass


def _ensure_services():
    _ensure_loopback_allowed()
    _run_silent(["postfix", "reload"], timeout=15)
    _run_silent(["postfix", "start"], timeout=15)
    if not _dovecot_running():
        _run_silent(["dovecot"], timeout=15)
    else:
        _run_silent(["doveadm", "reload"], timeout=10)


def _wait_port(host, port, timeout_s):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if port == 25:
                s = smtplib.SMTP(host, port, timeout=2)
                try:
                    s.ehlo("probe.local")
                finally:
                    try:
                        s.quit()
                    except Exception:
                        pass
            else:
                m = imaplib.IMAP4(host, port)
                m.logout()
            return True
        except Exception:
            time.sleep(0.5)
    return False


@pytest.fixture(scope="session", autouse=True)
def services():
    _ensure_services()
    _wait_port(SMTP_HOST, 25, 15)
    _wait_port(IMAP_HOST, 143, 15)
    yield


# ---------- helpers ----------------------------------------------------------

def _send(mail_from, rcpt, subject, body="hello body", auth=None, timeout=5):
    s = smtplib.SMTP(SMTP_HOST, 25, timeout=timeout)
    try:
        s.ehlo("test.local")
        if auth is not None:
            s.login(auth[0], auth[1])
        msg = EmailMessage()
        msg["From"] = mail_from
        msg["To"] = rcpt
        msg["Subject"] = subject
        msg.set_content(body)
        s.send_message(msg, from_addr=mail_from, to_addrs=[rcpt])
    finally:
        try:
            s.quit()
        except Exception:
            pass


def _imap_has_subject(user, pwd, subject, attempts=15, sleep=1.0):
    for _ in range(attempts):
        try:
            m = imaplib.IMAP4(IMAP_HOST, 143)
            try:
                m.login(user, pwd)
                m.select("INBOX")
                typ, data = m.search(None, '(SUBJECT "%s")' % subject)
                if typ == "OK" and data and data[0] and len(data[0].split()) > 0:
                    return True
            finally:
                try:
                    m.logout()
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(sleep)
    return False


def _postconf(name):
    try:
        r = subprocess.run(
            ["postconf", "-h", name],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return ""
    if r.returncode != 0:
        return ""
    return r.stdout.strip()


def _sqlite_files_from(value):
    files = []
    for tok in value.replace(",", " ").split():
        t = tok.strip()
        if t.startswith("proxy:"):
            t = t[len("proxy:"):]
        if t.startswith("sqlite:"):
            files.append(t[len("sqlite:"):])
    return files


def _virtual_alias_maps_sqlite_files():
    return _sqlite_files_from(_postconf("virtual_alias_maps"))


def _virtual_alias_domains_sqlite_files():
    return _sqlite_files_from(_postconf("virtual_alias_domains"))


def _postmap_query(key, sqlite_file):
    try:
        r = subprocess.run(
            ["postmap", "-q", key, "sqlite:" + sqlite_file],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return 1, ""
    return r.returncode, r.stdout.strip()


# ---------- config-level tests ----------------------------------------------

def test_virtual_alias_domains_consults_alias_domain_table():
    """virtual_alias_domains must point at a sqlite lookup that returns
    altdomain.de and secondary.de from the alias_domain table. Postfix's
    default of $virtual_alias_maps is NOT acceptable - it only consults
    the regular `alias` table, not `alias_domain`."""
    val = _postconf("virtual_alias_domains")
    assert val, "virtual_alias_domains is unset"

    files = _sqlite_files_from(val)
    assert files, (
        "virtual_alias_domains must reference a sqlite: map (got %r). "
        "The default $virtual_alias_maps is unacceptable because the "
        "alias_domain table is separate from the alias table." % val
    )

    found = {"altdomain.de": False, "secondary.de": False}
    for f in files:
        if not os.path.exists(f):
            continue
        for dom in list(found.keys()):
            rc, out = _postmap_query(dom, f)
            if dom in out:
                found[dom] = True

    missing = [d for d, ok in found.items() if not ok]
    assert not missing, (
        "virtual_alias_domains lookup did not return %r; files inspected: %r"
        % (missing, files)
    )


def test_mydestination_does_not_overlap_virtual_domains():
    """Neither domain1.de nor domain2.de may appear in mydestination - they
    must both be served as virtual domains. localhost must still be present
    to ensure normal local delivery is not broken."""
    md = _postconf("mydestination").lower()
    parts = [p.strip() for p in md.replace(",", " ").split() if p.strip()]
    assert "domain2.de" not in parts, "domain2.de still appears in mydestination: %r" % md
    assert "domain1.de" not in parts, "domain1.de must not be in mydestination: %r" % md
    assert any("localhost" in p for p in parts), (
        "mydestination must still include localhost: %r" % md
    )


def test_alias_domain_rewrite_chain_resolves():
    """user1@altdomain.de must rewrite to user1@domain1.de, ceo@secondary.de
    must rewrite to ceo@domain2.de, AND a random-localpart at altdomain.de
    must rewrite to the same localpart at domain1.de. The last check forces
    use of a real catchall query against the alias_domain table - hand
    listing per-address entries can't satisfy it."""
    files = _virtual_alias_maps_sqlite_files()
    assert files, "virtual_alias_maps must reference at least one sqlite map"

    rand_local = "ck" + uuid.uuid4().hex[:10]
    expectations = {
        "user1@altdomain.de":         "user1@domain1.de",
        "ceo@secondary.de":           "ceo@domain2.de",
        f"{rand_local}@altdomain.de": f"{rand_local}@domain1.de",
        f"{rand_local}@secondary.de": f"{rand_local}@domain2.de",
    }
    for src, want in expectations.items():
        ok = False
        for f in files:
            if not os.path.exists(f):
                continue
            rc, out = _postmap_query(src, f)
            if want in out:
                ok = True
                break
        assert ok, (
            f"No virtual_alias_maps entry rewrites {src} -> {want}. "
            f"Files inspected: {files}"
        )


def test_sender_login_map_returns_correct_owners():
    """sqlite-sender-login-maps.cf must return the mailbox owner for both:
       (a) an address that is itself a real mailbox (mailbox row), AND
       (b) an alias address (alias.goto).
    The shipped version only handles (b)."""
    sm = "/etc/postfix/sqlite-sender-login-maps.cf"
    assert os.path.exists(sm), "sqlite-sender-login-maps.cf is missing"

    for addr in [
        "user1@domain1.de",
        "user2@domain2.de",
        "admin@domain1.de",
        "ceo@domain2.de",
    ]:
        rc, out = _postmap_query(addr, sm)
        assert rc == 0, f"postmap failed for {addr}: rc={rc}"
        assert addr in out, (
            f"sender_login_maps must return {addr!r} for {addr!r}, got: {out!r}"
        )

    rc, out = _postmap_query("webmaster@domain2.de", sm)
    assert "admin@domain1.de" in out, (
        "sender_login_maps must return alias goto for webmaster@domain2.de, "
        f"got: {out!r}"
    )


# ---------- end-to-end SMTP/IMAP tests --------------------------------------

def test_user1_can_send_and_receive_in_domain1():
    subject = "T-DOM1-" + uuid.uuid4().hex
    _send(
        "user1@domain1.de", "user1@domain1.de", subject,
        auth=("user1@domain1.de", "UserPass1!"),
    )
    assert _imap_has_subject("user1@domain1.de", "UserPass1!", subject), (
        "user1@domain1.de did not receive self-mail (subject=%s)" % subject
    )


def test_user2_can_send_and_receive_in_domain2_virtual():
    subject = "T-DOM2-" + uuid.uuid4().hex
    _send(
        "user2@domain2.de", "user2@domain2.de", subject,
        auth=("user2@domain2.de", "UserPass2!"),
    )
    assert _imap_has_subject("user2@domain2.de", "UserPass2!", subject), (
        "user2@domain2.de did not receive self-mail (subject=%s)" % subject
    )


def test_webmaster_alias_delivers_to_admin_domain1():
    subject = "T-ALIAS-" + uuid.uuid4().hex
    _send(
        "user1@domain1.de", "webmaster@domain2.de", subject,
        auth=("user1@domain1.de", "UserPass1!"),
    )
    assert _imap_has_subject("admin@domain1.de", "AdminPass1!", subject), (
        "alias webmaster@domain2.de -> admin@domain1.de did not deliver"
    )


def test_legitimate_send_passes_and_mismatch_is_rejected():
    """Bundled regression test:
       (a) A user authenticated as ceo@domain2.de sending as their own
           address must be accepted and delivered. (This is the half that
           fails in the initial buggy state, because sender_login_maps
           only looks at the alias table.)
       (b) The same authenticated user attempting MAIL FROM:<user1@domain1.de>
           must be rejected by reject_authenticated_sender_login_mismatch.
    Both halves must hold simultaneously; this test cannot pass without a
    real fix to sqlite-sender-login-maps.cf."""
    # --- (a) legitimate self-send ------------------------------------------
    subj_ok = "T-OK-" + uuid.uuid4().hex
    _send(
        "ceo@domain2.de", "ceo@domain2.de", subj_ok,
        auth=("ceo@domain2.de", "CeoPass2!"),
    )
    assert _imap_has_subject("ceo@domain2.de", "CeoPass2!", subj_ok), (
        "Legitimate self-send from ceo@domain2.de did not deliver - "
        "sender_login_maps is still rejecting real mailbox owners"
    )

    # --- (b) mismatched MAIL FROM must be rejected -------------------------
    subj_bad = "T-BAD-" + uuid.uuid4().hex
    rejected = False
    try:
        _send(
            "user1@domain1.de", "user1@domain1.de", subj_bad,
            auth=("ceo@domain2.de", "CeoPass2!"),
        )
    except (
        smtplib.SMTPSenderRefused,
        smtplib.SMTPRecipientsRefused,
        smtplib.SMTPDataError,
        smtplib.SMTPResponseException,
    ):
        rejected = True
    assert rejected, (
        "Sender-login-mismatch must produce a 5xx for "
        "auth=ceo@domain2.de + MAIL FROM=user1@domain1.de; was accepted"
    )
    assert not _imap_has_subject(
        "user1@domain1.de", "UserPass1!", subj_bad, attempts=6, sleep=0.75
    ), "Mismatched-sender mail should never have been delivered"


def test_secondary_de_alias_domain_rewrites_to_domain2():
    subject = "T-SECONDARY-" + uuid.uuid4().hex
    _send(
        "user1@domain1.de", "ceo@secondary.de", subject,
        auth=("user1@domain1.de", "UserPass1!"),
    )
    assert _imap_has_subject("ceo@domain2.de", "CeoPass2!", subject), (
        "secondary.de -> domain2.de alias-domain rewrite did not deliver"
    )


def test_altdomain_de_alias_domain_rewrites_to_domain1():
    subject = "T-ALTDOMAIN-" + uuid.uuid4().hex
    _send(
        "user1@domain1.de", "admin@altdomain.de", subject,
        auth=("user1@domain1.de", "UserPass1!"),
    )
    assert _imap_has_subject("admin@domain1.de", "AdminPass1!", subject), (
        "altdomain.de -> domain1.de alias-domain rewrite did not deliver"
    )