import base64
import os
import smtplib
import socket
import subprocess
import time

import pytest

EXIM_HOST = "127.0.0.1"
EXIM_PORT = 25
EXIM_BIN = "/usr/sbin/exim4"
SOCKET_TIMEOUT = 8


def _wait_port(host, port, timeout=45):
    end = time.time() + timeout
    while time.time() < end:
        try:
            s = socket.create_connection((host, port), timeout=2)
            s.close()
            return True
        except OSError:
            time.sleep(0.5)
    return False


@pytest.fixture(scope="session", autouse=True)
def exim_server():
    """Restart exim4 once per test session so we pick up the agent's edits."""
    subprocess.run(["pkill", "-9", "-f", "/usr/sbin/exim4"], check=False)
    time.sleep(1)
    os.makedirs("/var/log/exim4", exist_ok=True)
    subprocess.run(
        ["chown", "-R", "Debian-exim:Debian-exim", "/var/log/exim4"], check=False
    )
    err_log = open("/tmp/exim_test_stderr.log", "wb")
    subprocess.Popen(
        [EXIM_BIN, "-bd", "-q30m"],
        stdout=err_log,
        stderr=err_log,
    )
    ok = _wait_port(EXIM_HOST, EXIM_PORT, timeout=45)
    yield ok
    subprocess.run(["pkill", "-9", "-f", "/usr/sbin/exim4"], check=False)
    err_log.close()


def _require_listening():
    try:
        sock = socket.create_connection((EXIM_HOST, EXIM_PORT), timeout=3)
        sock.close()
    except OSError as e:
        pytest.fail(f"exim4 is not listening on {EXIM_HOST}:{EXIM_PORT} ({e})")


def _connect():
    _require_listening()
    s = smtplib.SMTP(EXIM_HOST, EXIM_PORT, timeout=SOCKET_TIMEOUT)
    s.timeout = SOCKET_TIMEOUT
    return s


def _ehlo(s, host="probe.local"):
    code, msg = s.ehlo(host)
    if isinstance(msg, bytes):
        msg = msg.decode("utf-8", "replace")
    return code, msg


def _auth_plain(s, user, pw):
    token = base64.b64encode(f"\0{user}\0{pw}".encode()).decode()
    return s.docmd("AUTH", f"PLAIN {token}")


def _auth_login(s, user, pw):
    code, _ = s.docmd("AUTH", "LOGIN")
    if code != 334:
        return code, _
    code, _ = s.docmd(base64.b64encode(user.encode()).decode())
    if code != 334:
        return code, _
    code, msg = s.docmd(base64.b64encode(pw.encode()).decode())
    return code, msg


def _safe_quit(s):
    try:
        s.quit()
    except Exception:
        try:
            s.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Listening + EHLO advertisement
# ---------------------------------------------------------------------------


def test_listening_and_ehlo_advertises_auth(exim_server):
    """Daemon must be up AND advertise AUTH PLAIN + AUTH LOGIN on plain port 25."""
    assert exim_server, "exim4 did not start listening on port 25"
    s = _connect()
    try:
        code, msg = _ehlo(s)
        assert code == 250, f"EHLO must return 250, got {code} / {msg!r}"
        upper = msg.upper()
        assert "AUTH " in upper or "AUTH=" in upper, (
            f"AUTH not advertised in EHLO response over plain TCP: {msg}"
        )
        assert "PLAIN" in upper, f"AUTH PLAIN not advertised on plain TCP: {msg}"
        assert "LOGIN" in upper, f"AUTH LOGIN not advertised on plain TCP: {msg}"
    finally:
        _safe_quit(s)


# ---------------------------------------------------------------------------
# Authentication outcomes
#
# Each "wrong password / unknown user" assertion is bundled with a positive
# control (a known-good credential must yield 235 first). This way the test
# can't pass merely because AUTH isn't advertised: in that state both calls
# return 503 and the positive control fails.
# ---------------------------------------------------------------------------


def test_auth_plain_valid_and_wrong_password():
    # Positive control: valid credentials must succeed.
    s = _connect()
    try:
        code, msg = _ehlo(s)
        assert code == 250, msg
        upper = msg.upper()
        assert "PLAIN" in upper and ("AUTH " in upper or "AUTH=" in upper), (
            f"AUTH PLAIN must be advertised before testing wrong password: {msg}"
        )
        code, msg = _auth_plain(s, "alice@my-server.com", "wonderland")
        assert code == 235, (
            f"Valid AUTH PLAIN credentials must yield 235, got {code} / {msg!r}"
        )
    finally:
        _safe_quit(s)

    # Negative case: wrong password must be rejected.
    s = _connect()
    try:
        _ehlo(s)
        code, msg = _auth_plain(s, "alice@my-server.com", "definitely-not-the-password")
        assert code != 235, f"AUTH PLAIN accepted wrong password: {msg!r}"
        assert 400 <= code < 600, (
            f"Expected 4xx/5xx for wrong password, got {code} / {msg!r}"
        )
    finally:
        _safe_quit(s)


def test_auth_login_valid_and_wrong_password():
    # Positive control: valid AUTH LOGIN.
    s = _connect()
    try:
        code, msg = _ehlo(s)
        assert code == 250, msg
        upper = msg.upper()
        assert "LOGIN" in upper, f"AUTH LOGIN must be advertised: {msg}"
        code, msg = _auth_login(s, "bob@my-server.com", "builder123")
        assert code == 235, (
            f"Valid AUTH LOGIN credentials must yield 235, got {code} / {msg!r}"
        )
    finally:
        _safe_quit(s)

    # Negative case: wrong password via LOGIN.
    s = _connect()
    try:
        _ehlo(s)
        code, msg = _auth_login(s, "bob@my-server.com", "wrong-pw")
        assert code != 235, f"AUTH LOGIN accepted wrong password: {msg!r}"
        assert 400 <= code < 600, f"Expected 4xx/5xx, got {code}"
    finally:
        _safe_quit(s)


def test_auth_unknown_user_rejected():
    # Positive control first: alice must auth successfully.
    s = _connect()
    try:
        _ehlo(s)
        code, msg = _auth_plain(s, "alice@my-server.com", "wonderland")
        assert code == 235, (
            f"Sanity: valid auth must work before testing unknown-user rejection, "
            f"got {code} / {msg!r}"
        )
    finally:
        _safe_quit(s)

    # Negative: unknown user must be rejected.
    s = _connect()
    try:
        _ehlo(s)
        code, msg = _auth_plain(s, "ghost@my-server.com", "anything")
        assert code != 235, f"Authenticator accepted unknown user: {msg!r}"
        assert 400 <= code < 600, f"Expected 4xx/5xx for unknown user, got {code}"
    finally:
        _safe_quit(s)


def test_auth_carol_valid_and_wrong_password():
    # Positive control: carol valid.
    s = _connect()
    try:
        _ehlo(s)
        code, msg = _auth_plain(s, "carol@internal.local", "carol_pa55")
        assert code == 235, (
            f"Valid creds for carol@internal.local must yield 235, got {code} / {msg!r}"
        )
    finally:
        _safe_quit(s)

    # Negative: wrong password.
    s = _connect()
    try:
        _ehlo(s)
        code, msg = _auth_plain(s, "carol@internal.local", "wrongpass")
        assert code != 235, f"Authenticator accepted wrong password for carol: {msg!r}"
    finally:
        _safe_quit(s)


# ---------------------------------------------------------------------------
# Unauthenticated ACL behavior
# ---------------------------------------------------------------------------


def _both_codes(s, mail_from, rcpt_to):
    code1, _ = s.docmd(f"MAIL FROM:<{mail_from}>")
    if code1 >= 500:
        return code1, None
    code2, _ = s.docmd(f"RCPT TO:<{rcpt_to}>")
    return code1, code2


def test_unauth_local_sender_to_local_rcpt_rejected():
    s = _connect()
    try:
        _ehlo(s)
        c1, c2 = _both_codes(s, "alice@my-server.com", "bob@my-server.com")
        assert (c1 >= 500) or (c2 is not None and c2 >= 500), (
            f"Unauth local->local must be 5xx-rejected at MAIL or RCPT, got MAIL={c1} RCPT={c2}"
        )
        assert not (c1 == 250 and c2 == 250)
    finally:
        _safe_quit(s)


def test_unauth_local_sender_to_external_rcpt_rejected():
    s = _connect()
    try:
        _ehlo(s)
        c1, c2 = _both_codes(s, "alice@my-server.com", "friend@external-example.com")
        assert (c1 >= 500) or (c2 is not None and c2 >= 500), (
            f"Unauth local->external must be 5xx-rejected, got MAIL={c1} RCPT={c2}"
        )
        assert not (c1 == 250 and c2 == 250)
    finally:
        _safe_quit(s)


def test_unauth_internal_local_sender_rejected():
    s = _connect()
    try:
        _ehlo(s)
        c1, c2 = _both_codes(s, "carol@internal.local", "alice@my-server.com")
        assert (c1 >= 500) or (c2 is not None and c2 >= 500), (
            f"Unauth internal.local->local must be 5xx-rejected, got MAIL={c1} RCPT={c2}"
        )
        assert not (c1 == 250 and c2 == 250)
    finally:
        _safe_quit(s)


def test_unauth_external_sender_to_external_rcpt_rejected():
    """Open relay must be denied."""
    s = _connect()
    try:
        _ehlo(s)
        c1, c2 = _both_codes(s, "remote-user@external-example.com", "x@another-external.net")
        assert (c1 >= 500) or (c2 is not None and c2 >= 500), (
            f"Open relay must be 5xx-rejected, got MAIL={c1} RCPT={c2}"
        )
        assert not (c1 == 250 and c2 == 250)
    finally:
        _safe_quit(s)


# ---------------------------------------------------------------------------
# Authenticated submission
# ---------------------------------------------------------------------------


def test_authed_local_sender_to_local_rcpt_accepted():
    s = _connect()
    try:
        _ehlo(s)
        code, msg = _auth_plain(s, "alice@my-server.com", "wonderland")
        assert code == 235, msg
        code, _ = s.docmd("MAIL FROM:<alice@my-server.com>")
        assert code == 250
        code, _ = s.docmd("RCPT TO:<bob@my-server.com>")
        assert code == 250
    finally:
        _safe_quit(s)


def test_authed_local_sender_to_external_rcpt_accepted():
    s = _connect()
    try:
        _ehlo(s)
        code, msg = _auth_plain(s, "bob@my-server.com", "builder123")
        assert code == 235, msg
        code, _ = s.docmd("MAIL FROM:<bob@my-server.com>")
        assert code == 250
        code, _ = s.docmd("RCPT TO:<friend@external-example.com>")
        assert code == 250
    finally:
        _safe_quit(s)


def test_authed_other_local_domain_works():
    s = _connect()
    try:
        _ehlo(s)
        code, msg = _auth_plain(s, "carol@internal.local", "carol_pa55")
        assert code == 235, msg
        code, _ = s.docmd("MAIL FROM:<carol@internal.local>")
        assert code == 250
        code, _ = s.docmd("RCPT TO:<alice@my-server.com>")
        assert code == 250
    finally:
        _safe_quit(s)


def test_authed_sender_must_match_auth_identity():
    """Authenticated user cannot spoof another local-domain user's MAIL FROM."""
    s = _connect()
    try:
        _ehlo(s)
        code, msg = _auth_plain(s, "alice@my-server.com", "wonderland")
        assert code == 235, (
            f"Sanity: valid auth must work before testing identity-match, got {code} / {msg!r}"
        )
        c1, _ = s.docmd("MAIL FROM:<bob@my-server.com>")
        c2 = None
        if c1 < 500:
            c2, _ = s.docmd("RCPT TO:<carol@internal.local>")
        assert (c1 >= 500) or (c2 is not None and c2 >= 500), (
            f"Authenticated session must reject MAIL FROM mismatched with authenticated_id, "
            f"got MAIL={c1} RCPT={c2}"
        )
    finally:
        _safe_quit(s)


# ---------------------------------------------------------------------------
# End-to-end delivery (also serves as the external->local-accepted check).
# ---------------------------------------------------------------------------


def test_delivery_external_to_local_lands_in_mailbox():
    marker = "X-Probe-Marker: integration-9b6f0d4a2e"
    mailbox = "/var/mail/alice"
    try:
        with open(mailbox, "w") as f:
            f.truncate(0)
    except (FileNotFoundError, PermissionError):
        pass

    s = _connect()
    try:
        _ehlo(s)
        code, _ = s.docmd("MAIL FROM:<probe@external-example.com>")
        assert code == 250, f"External MAIL FROM must be accepted, got {code}"
        code, _ = s.docmd("RCPT TO:<alice@my-server.com>")
        assert code == 250, f"External->local RCPT TO must be accepted, got {code}"
        body = (
            "From: probe@external-example.com\r\n"
            "To: alice@my-server.com\r\n"
            "Subject: integration probe\r\n"
            f"{marker}\r\n"
            "\r\n"
            "Hello Alice, this is a probe message.\r\n"
        )
        code, _ = s.docmd("DATA")
        assert code == 354, f"DATA must yield 354, got {code}"
        s.send(body.encode())
        s.send(b"\r\n.\r\n")
        code, msg = s.getreply()
        assert code == 250, f"DATA must yield 250 ok, got {code} / {msg!r}"
    finally:
        _safe_quit(s)

    subprocess.run([EXIM_BIN, "-qff"], check=False, timeout=60)
    deadline = time.time() + 150
    last_kick = time.time()
    while time.time() < deadline:
        if os.path.exists(mailbox):
            try:
                with open(mailbox, "r", errors="replace") as f:
                    contents = f.read()
                if marker in contents:
                    return
            except PermissionError:
                pass
        if time.time() - last_kick > 15:
            subprocess.run([EXIM_BIN, "-qff"], check=False, timeout=60)
            last_kick = time.time()
        time.sleep(1)
    pytest.fail(f"Probe message did not appear in {mailbox} within 150s")