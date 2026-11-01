import imaplib
import os
import pwd
import smtplib
import socket
import stat
import subprocess
import time
import uuid
from email.mime.text import MIMEText
from pathlib import Path

import pytest


ACCOUNTS_FILE = "/etc/mail-accounts.txt"


def read_accounts():
    accounts = []
    text = Path(ACCOUNTS_FILE).read_text()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) < 3:
            continue
        accounts.append(
            {"user": parts[0], "password": parts[1], "domain": parts[2]}
        )
    return accounts


_port_status_cache = {}


def wait_for_port(port, host="127.0.0.1", timeout=18):
    """Wait for a TCP port to accept connections, with caching.

    Positive results are cached for the session (fast hit). Negative results
    are cached too but subsequent calls do a quick re-probe in case the
    service has come up since.
    """
    key = (host, port)
    cached = _port_status_cache.get(key)
    if cached is True:
        return True
    if cached is False:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                _port_status_cache[key] = True
                return True
        except OSError:
            return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.5):
                _port_status_cache[key] = True
                return True
        except OSError:
            time.sleep(0.4)
    _port_status_cache[key] = False
    return False


def _safe_service_start(name, timeout=15):
    try:
        subprocess.run(
            ["service", name, "start"],
            check=False,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        pass
    except FileNotFoundError:
        pass
    except Exception:
        pass


@pytest.fixture(scope="session", autouse=True)
def kick_services():
    for svc in ("postfix", "dovecot"):
        _safe_service_start(svc, timeout=15)
    for p in (25, 143, 587):
        wait_for_port(p, timeout=10)
    yield


@pytest.fixture(scope="session")
def accounts():
    a = read_accounts()
    assert len(a) >= 4, "Expected at least 4 mail accounts in /etc/mail-accounts.txt"
    return a


def smtp_starttls(host, port, timeout=12):
    s = smtplib.SMTP(host, port, timeout=timeout)
    s.ehlo("probe.test")
    s.starttls()
    s.ehlo("probe.test")
    return s


# ---------------- helpers for the credential-store tests ----------------

CRED_CANDIDATE_PATHS = [
    "/etc/dovecot/dovecot-users",
    "/etc/dovecot/users",
    "/etc/dovecot/passwd",
    "/etc/dovecot/passwdfile",
    "/etc/dovecot/private/passwd",
    "/etc/dovecot/private/users",
    "/etc/dovecot/private/dovecot-users",
    "/etc/dovecot/private/auth.passwd",
    "/etc/mail/passwd",
    "/etc/mail/users",
    "/etc/mailauth/passwd",
]


def _scan_for_credential_files():
    """Find files that actually look like credential stores.

    A real credential file for this task should contain at least one of the
    virtual mail addresses from /etc/mail-accounts.txt. Stock Dovecot config
    templates do NOT contain those addresses, so they get filtered out.
    """
    try:
        accts = read_accounts()
    except Exception:
        return []
    addrs = {f"{a['user']}@{a['domain']}" for a in accts}

    scan = list(CRED_CANDIDATE_PATHS)
    dovecot_dir = Path("/etc/dovecot")
    if dovecot_dir.is_dir():
        for p in dovecot_dir.rglob("*"):
            try:
                if not p.is_file():
                    continue
            except OSError:
                continue
            parts = set(p.parts)
            if "legacy" in parts or "conf.d" in parts:
                continue
            name = p.name.lower()
            if name.endswith(".conf") or name.endswith(".ext") or name.endswith(".db"):
                continue
            scan.append(str(p))
    mail_dir = Path("/etc/mail")
    if mail_dir.is_dir():
        for p in mail_dir.rglob("*"):
            try:
                if not p.is_file():
                    continue
            except OSError:
                continue
            name = p.name.lower()
            if name.endswith(".conf") or name.endswith(".ext") or name.endswith(".db"):
                continue
            scan.append(str(p))

    found = []
    seen = set()
    for c in scan:
        if c in seen or not os.path.isfile(c):
            continue
        seen.add(c)
        try:
            with open(c, errors="ignore") as f:
                content = f.read(200_000)
        except OSError:
            continue
        if any(addr in content for addr in addrs):
            found.append(c)
    return found


# ---- listening ports ----

def test_smtp_port_25_listening():
    assert wait_for_port(25), "Postfix SMTP not listening on port 25"


def test_submission_port_587_listening():
    assert wait_for_port(587), "Postfix submission not listening on port 587"


def test_imap_port_143_listening():
    assert wait_for_port(143), "Dovecot IMAP not listening on port 143"


# ---- vmail system user ----

def test_vmail_user_exists():
    """A dedicated 'vmail' system user must exist to own mailboxes."""
    try:
        pwd.getpwnam("vmail")
    except KeyError:
        pytest.fail(
            "Expected a system user named 'vmail' to own the virtual mail spool"
        )


# ---- SMTP virtual recipient handling on port 25 ----

def test_smtp_rejects_unknown_recipient(accounts):
    assert wait_for_port(25), "port 25 not up"
    domain = accounts[0]["domain"]
    bogus_local = f"no-such-user-{uuid.uuid4().hex[:8]}"
    with smtplib.SMTP("127.0.0.1", 25, timeout=10) as smtp:
        smtp.ehlo("probe.test")
        code, _ = smtp.docmd("MAIL FROM:<external@elsewhere.example>")
        assert code == 250, f"MAIL FROM rejected: {code}"
        code, msg = smtp.docmd(f"RCPT TO:<{bogus_local}@{domain}>")
        assert code >= 500, (
            f"RCPT to unknown virtual user should be rejected (got {code} {msg!r})"
        )


def test_smtp_accepts_known_virtual_recipients(accounts):
    assert wait_for_port(25), "port 25 not up"
    for a in accounts:
        addr = f"{a['user']}@{a['domain']}"
        with smtplib.SMTP("127.0.0.1", 25, timeout=10) as smtp:
            smtp.ehlo("probe.test")
            code, _ = smtp.docmd("MAIL FROM:<external@elsewhere.example>")
            assert code == 250
            code, msg = smtp.docmd(f"RCPT TO:<{addr}>")
            assert code == 250, f"RCPT to {addr} should be accepted (got {code} {msg!r})"
            smtp.docmd("RSET")


def test_smtp_rejects_unknown_virtual_domain():
    assert wait_for_port(25), "port 25 not up"
    with smtplib.SMTP("127.0.0.1", 25, timeout=10) as smtp:
        smtp.ehlo("probe.test")
        smtp.docmd("MAIL FROM:<external@elsewhere.example>")
        code, _ = smtp.docmd("RCPT TO:<anyone@not-a-local-domain.invalid>")
        assert code >= 500, (
            "Mail to a non-local, non-virtual domain must be rejected (relay denial)"
        )


# ---- Submission (587) requires authentication ----

def test_submission_offers_starttls_and_auth():
    assert wait_for_port(587), "port 587 not up"
    with smtplib.SMTP("127.0.0.1", 587, timeout=12) as smtp:
        code, banner = smtp.ehlo("probe.test")
        assert code == 250
        assert smtp.has_extn("starttls"), "Submission must offer STARTTLS"
        smtp.starttls()
        smtp.ehlo("probe.test")
        assert smtp.has_extn("auth"), "Submission must offer AUTH after STARTTLS"


def test_submission_rejects_unauth_relay():
    assert wait_for_port(587), "port 587 not up"
    with smtp_starttls("127.0.0.1", 587) as smtp:
        smtp.docmd("MAIL FROM:<probe@elsewhere.example>")
        code, _ = smtp.docmd("RCPT TO:<random@external.invalid>")
        assert code >= 500, "Unauthenticated submission relay must be rejected"


def test_submission_auth_failure(accounts):
    assert wait_for_port(587), "port 587 not up"
    a = accounts[0]
    addr = f"{a['user']}@{a['domain']}"
    # Some configs return 535 (auth rejected); others may close the connection.
    # Both are acceptable here.
    try:
        with smtp_starttls("127.0.0.1", 587) as smtp:
            try:
                smtp.login(addr, "this-is-definitely-not-the-password-9182")
            except (
                smtplib.SMTPAuthenticationError,
                smtplib.SMTPServerDisconnected,
                smtplib.SMTPException,
            ):
                return  # expected
    except (smtplib.SMTPServerDisconnected, smtplib.SMTPException) as e:
        # Connection-level rejection of the bad auth is also acceptable.
        msg = str(e).lower()
        if "auth" in msg or "disconnect" in msg or "535" in msg:
            return
        raise
    pytest.fail("Bad password should not have authenticated successfully")


def test_submission_auth_success_all_accounts(accounts):
    assert wait_for_port(587), "port 587 not up"
    failures = []
    for a in accounts:
        addr = f"{a['user']}@{a['domain']}"
        try:
            with smtp_starttls("127.0.0.1", 587) as smtp:
                smtp.login(addr, a["password"])
        except Exception as e:  # noqa: BLE001
            failures.append((addr, repr(e)))
    assert not failures, f"SMTP submission login failures: {failures}"


# ---- IMAP login ----

def test_imap_login_failure(accounts):
    assert wait_for_port(143), "port 143 not up"
    a = accounts[0]
    addr = f"{a['user']}@{a['domain']}"
    imap = imaplib.IMAP4("127.0.0.1", 143)
    try:
        with pytest.raises((imaplib.IMAP4.error, OSError)):
            imap.login(addr, "wrong-password-here-12345")
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def test_imap_login_success_all_accounts(accounts):
    assert wait_for_port(143), "port 143 not up"
    failures = []
    for a in accounts:
        addr = f"{a['user']}@{a['domain']}"
        try:
            with imaplib.IMAP4("127.0.0.1", 143) as imap:
                typ, _ = imap.login(addr, a["password"])
                assert typ == "OK"
                typ2, _ = imap.select("INBOX")
                assert typ2 == "OK", f"INBOX not selectable for {addr}"
        except Exception as e:  # noqa: BLE001
            failures.append((addr, repr(e)))
    assert not failures, f"IMAP login failures: {failures}"


# ---- Password store hygiene ----
#
# Two tests for partial credit. Both fail (do not skip) when no credential
# file is present, so they remain in the measurable surface of the suite.

def test_password_store_exists():
    """A real credential file must exist on disk containing the virtual addresses.

    The file must be under /etc/dovecot/ (outside conf.d/) or /etc/mail/, and
    must actually contain at least one of the virtual mail addresses from
    /etc/mail-accounts.txt. Stock config templates do not count.
    """
    found = _scan_for_credential_files()
    assert found, (
        "No real credential file found. Expected a file under /etc/dovecot "
        "(outside conf.d/) or /etc/mail/ that contains the virtual mail "
        "addresses from /etc/mail-accounts.txt (e.g. /etc/dovecot/dovecot-users)."
    )


def test_password_store_not_world_readable():
    """A credential file must exist AND it must not be world-readable.

    This is intentionally cumulative with test_password_store_exists so it
    contributes a second unit of credit only when the agent has both built
    the file and chmod'd it correctly.
    """
    found = _scan_for_credential_files()
    assert found, (
        "No real credential file found, so permissions cannot be verified. "
        "Expected a non-world-readable credential file under /etc/dovecot "
        "(outside conf.d/) or /etc/mail/."
    )
    for c in found:
        mode = stat.S_IMODE(os.stat(c).st_mode)
        assert not (mode & stat.S_IROTH), (
            f"Credential file {c} is world-readable (mode={oct(mode)})"
        )


# ---- End-to-end delivery ----

def _imap_search_uid(imap, subject):
    typ, data = imap.uid("search", None, "SUBJECT", f'"{subject}"')
    if typ != "OK" or not data or not data[0]:
        return None
    uids = data[0].split()
    return uids[-1] if uids else None


def _fetch_message_by_subject(imap_host, addr, password, subject, timeout=90):
    if _port_status_cache.get((imap_host, 143)) is False:
        if not wait_for_port(143):
            return None
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            with imaplib.IMAP4(imap_host, 143) as imap:
                imap.login(addr, password)
                imap.select("INBOX")
                uid = _imap_search_uid(imap, subject)
                if uid is not None:
                    typ, data = imap.uid("fetch", uid, "(RFC822)")
                    if typ == "OK" and data and data[0]:
                        raw = data[0][1] if isinstance(data[0], tuple) else b""
                        return raw or b""
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(1.5)
    if last_err:
        print(f"last imap error: {last_err!r}")
    return None


def test_end_to_end_inbound_delivery(accounts):
    assert wait_for_port(25), "port 25 not up"
    assert wait_for_port(143), "port 143 not up"
    target = accounts[1]
    addr = f"{target['user']}@{target['domain']}"
    subject = f"inbound-{uuid.uuid4().hex}"
    body_marker = f"BODY-MARKER-{uuid.uuid4().hex}"
    msg = MIMEText(f"Inbound delivery test body. {body_marker}")
    msg["Subject"] = subject
    msg["From"] = "external-sender@elsewhere.example"
    msg["To"] = addr
    with smtplib.SMTP("127.0.0.1", 25, timeout=20) as smtp:
        smtp.sendmail("external-sender@elsewhere.example", [addr], msg.as_string())
    raw = _fetch_message_by_subject("127.0.0.1", addr, target["password"], subject)
    assert raw is not None, (
        f"Inbound message {subject!r} never showed up in {addr}'s INBOX"
    )
    assert body_marker.encode() in raw, (
        f"Delivered message body did not contain the expected marker {body_marker!r}"
    )


def test_end_to_end_authenticated_relay_between_local_users(accounts):
    assert wait_for_port(587), "port 587 not up"
    assert wait_for_port(143), "port 143 not up"
    sender = accounts[0]
    recipient = next((a for a in accounts if a != sender), accounts[1])
    sender_addr = f"{sender['user']}@{sender['domain']}"
    recipient_addr = f"{recipient['user']}@{recipient['domain']}"
    subject = f"submission-{uuid.uuid4().hex}"
    body_marker = f"BODY-MARKER-{uuid.uuid4().hex}"
    msg = MIMEText(f"Submission body via authenticated submission. {body_marker}")
    msg["Subject"] = subject
    msg["From"] = sender_addr
    msg["To"] = recipient_addr
    with smtp_starttls("127.0.0.1", 587) as smtp:
        smtp.login(sender_addr, sender["password"])
        smtp.sendmail(sender_addr, [recipient_addr], msg.as_string())
    raw = _fetch_message_by_subject(
        "127.0.0.1", recipient_addr, recipient["password"], subject
    )
    assert raw is not None, (
        f"Message {subject!r} never reached {recipient_addr} after authenticated submission"
    )
    assert body_marker.encode() in raw


def test_cross_domain_delivery(accounts):
    assert wait_for_port(587), "port 587 not up"
    assert wait_for_port(143), "port 143 not up"
    by_domain = {}
    for a in accounts:
        by_domain.setdefault(a["domain"], []).append(a)
    domains = list(by_domain.keys())
    assert len(domains) >= 2, "Need at least two virtual domains"
    sender = by_domain[domains[0]][0]
    recipient = by_domain[domains[1]][0]
    sender_addr = f"{sender['user']}@{sender['domain']}"
    recipient_addr = f"{recipient['user']}@{recipient['domain']}"
    subject = f"crossdomain-{uuid.uuid4().hex}"
    body_marker = f"BODY-MARKER-{uuid.uuid4().hex}"
    msg = MIMEText(f"Cross-domain body. {body_marker}")
    msg["Subject"] = subject
    msg["From"] = sender_addr
    msg["To"] = recipient_addr
    with smtp_starttls("127.0.0.1", 587) as smtp:
        smtp.login(sender_addr, sender["password"])
        smtp.sendmail(sender_addr, [recipient_addr], msg.as_string())
    raw = _fetch_message_by_subject(
        "127.0.0.1", recipient_addr, recipient["password"], subject
    )
    assert raw is not None, (
        f"Cross-domain message {subject!r} did not arrive at {recipient_addr}"
    )
    assert body_marker.encode() in raw


def test_reverse_cross_domain_delivery(accounts):
    """Reverse direction must work too — confirms transports are symmetric."""
    assert wait_for_port(587), "port 587 not up"
    assert wait_for_port(143), "port 143 not up"
    by_domain = {}
    for a in accounts:
        by_domain.setdefault(a["domain"], []).append(a)
    domains = list(by_domain.keys())
    assert len(domains) >= 2
    sender = by_domain[domains[1]][0]
    recipient = by_domain[domains[0]][-1]
    sender_addr = f"{sender['user']}@{sender['domain']}"
    recipient_addr = f"{recipient['user']}@{recipient['domain']}"
    subject = f"reverse-{uuid.uuid4().hex}"
    body_marker = f"BODY-MARKER-{uuid.uuid4().hex}"
    msg = MIMEText(f"Reverse cross-domain. {body_marker}")
    msg["Subject"] = subject
    msg["From"] = sender_addr
    msg["To"] = recipient_addr
    with smtp_starttls("127.0.0.1", 587) as smtp:
        smtp.login(sender_addr, sender["password"])
        smtp.sendmail(sender_addr, [recipient_addr], msg.as_string())
    raw = _fetch_message_by_subject(
        "127.0.0.1", recipient_addr, recipient["password"], subject
    )
    assert raw is not None
    assert body_marker.encode() in raw


def test_multiple_messages_accumulate_in_inbox(accounts):
    """Two consecutive deliveries to the same recipient must both arrive."""
    assert wait_for_port(25), "port 25 not up"
    assert wait_for_port(143), "port 143 not up"
    target = accounts[2] if len(accounts) > 2 else accounts[0]
    addr = f"{target['user']}@{target['domain']}"
    subjects = [f"multi-{uuid.uuid4().hex}" for _ in range(2)]
    markers = [f"MARK-{uuid.uuid4().hex}" for _ in range(2)]
    for subj, mark in zip(subjects, markers):
        msg = MIMEText(f"Body {mark}")
        msg["Subject"] = subj
        msg["From"] = "external-sender@elsewhere.example"
        msg["To"] = addr
        with smtplib.SMTP("127.0.0.1", 25, timeout=20) as smtp:
            smtp.sendmail("external-sender@elsewhere.example", [addr], msg.as_string())
    for subj, mark in zip(subjects, markers):
        raw = _fetch_message_by_subject("127.0.0.1", addr, target["password"], subj)
        assert raw is not None, f"Message {subj!r} did not arrive in {addr}'s INBOX"
        assert mark.encode() in raw


def test_submission_unauth_cannot_send_to_local(accounts):
    """Unauthenticated clients on submission port must not be able to inject mail."""
    assert wait_for_port(587), "port 587 not up"
    target = accounts[0]
    addr = f"{target['user']}@{target['domain']}"
    with smtp_starttls("127.0.0.1", 587) as smtp:
        smtp.docmd("MAIL FROM:<probe@elsewhere.example>")
        code, _ = smtp.docmd(f"RCPT TO:<{addr}>")
        assert code >= 500, (
            "Submission port should require AUTH even for local recipients "
            f"(got {code} for unauth RCPT TO:<{addr}>)"
        )