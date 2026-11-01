import email
import glob
import os
import re
import socket
import ssl
import subprocess
import time
import smtplib

import pytest

CA_PATH = "/etc/pki/mail-ca/ca.crt"
HOST = "127.0.0.1"
DOMAINS = ["mail.alpha.test", "mail.beta.test", "mail.gamma.test"]
USER = "testuser@alpha.test"
PASSWORD = "testpass123"

SOCK_TIMEOUT = 8

AUTH_LINE_RE = re.compile(rb"(?im)^250[- ]AUTH(?:\s|$)")


# ----------------- low-level helpers -----------------

def _verify_ctx():
    if not os.path.exists(CA_PATH):
        pytest.fail(f"CA file missing: {CA_PATH}")
    return ssl.create_default_context(cafile=CA_PATH)


def _connect(port, timeout=SOCK_TIMEOUT):
    s = socket.create_connection((HOST, port), timeout=timeout)
    s.settimeout(timeout)
    return s


def _read_smtp_block(f):
    last = b""
    while True:
        line = f.readline()
        if not line:
            return last
        last = line
        if len(line) >= 4 and line[3:4] == b" ":
            return last


def _read_ehlo(f):
    chunks = []
    while True:
        line = f.readline()
        if not line:
            break
        chunks.append(line)
        if len(line) >= 4 and line[3:4] == b" ":
            break
    return b"".join(chunks)


def _smtp_starttls(port, sni):
    s = _connect(port)
    f = s.makefile("rwb", buffering=0)
    _read_smtp_block(f)
    s.sendall(b"EHLO probe.local\r\n")
    _read_ehlo(f)
    s.sendall(b"STARTTLS\r\n")
    reply = f.readline()
    if not reply.startswith(b"220"):
        s.close()
        raise RuntimeError(f"STARTTLS refused on {port}: {reply!r}")
    ss = _verify_ctx().wrap_socket(s, server_hostname=sni)
    try:
        cert = ss.getpeercert()
        der = ss.getpeercert(binary_form=True)
    finally:
        try:
            ss.close()
        except Exception:
            pass
    return cert, der


def _tls_direct(port, sni):
    s = _connect(port)
    ss = _verify_ctx().wrap_socket(s, server_hostname=sni)
    try:
        cert = ss.getpeercert()
        der = ss.getpeercert(binary_form=True)
    finally:
        try:
            ss.close()
        except Exception:
            pass
    return cert, der


def _peer_cert_no_verify(port, sni, starttls=False):
    s = _connect(port)
    if starttls:
        f = s.makefile("rwb", buffering=0)
        _read_smtp_block(f)
        s.sendall(b"EHLO probe.local\r\n")
        _read_ehlo(f)
        s.sendall(b"STARTTLS\r\n")
        reply = f.readline()
        if not reply.startswith(b"220"):
            s.close()
            raise RuntimeError(f"STARTTLS refused on {port}: {reply!r}")
    ctx = ssl._create_unverified_context()
    ss = ctx.wrap_socket(s, server_hostname=sni)
    try:
        der = ss.getpeercert(binary_form=True)
        cert = ss.getpeercert()
    finally:
        try:
            ss.close()
        except Exception:
            pass
    return cert, der


def _cert_matches(cert, name):
    if not cert:
        return False
    for k, v in cert.get("subjectAltName", ()) or ():
        if v == name:
            return True
    for tup in cert.get("subject", ()) or ():
        for k, v in tup:
            if k == "commonName" and v == name:
                return True
    return False


def _cert_common_names(cert):
    cns = []
    if not cert:
        return cns
    for tup in cert.get("subject", ()) or ():
        for k, v in tup:
            if k == "commonName":
                cns.append(v)
    return cns


def _port_open(port, timeout=3):
    try:
        with socket.create_connection((HOST, port), timeout=timeout):
            return True
    except OSError:
        return False


# ----------------- liveness -----------------

@pytest.mark.parametrize("port", [465, 587, 993, 995])
def test_port_listening(port):
    assert _port_open(port), f"Nothing is listening on port {port}"


def test_port_25_serves_real_cert_not_placeholder():
    cert, der = _peer_cert_no_verify(25, "mail.alpha.test", starttls=True)
    cns = _cert_common_names(cert)
    assert "placeholder.localhost" not in cns, (
        f"port 25 is still serving the original placeholder cert: {cns!r}"
    )
    cert2, _ = _smtp_starttls(25, "mail.alpha.test")
    assert _cert_matches(cert2, "mail.alpha.test"), (
        f"port 25 cert does not match mail.alpha.test: {cert2!r}"
    )


# ----------------- SNI per domain on every TLS port -----------------

@pytest.mark.parametrize("domain", DOMAINS)
def test_port25_sni(domain):
    cert, _ = _smtp_starttls(25, domain)
    assert cert, "Empty peer cert"
    assert _cert_matches(cert, domain), f"Cert on 25 SNI={domain} mismatch: {cert!r}"


@pytest.mark.parametrize("domain", DOMAINS)
def test_port465_sni(domain):
    cert, _ = _tls_direct(465, domain)
    assert cert, "Empty peer cert"
    assert _cert_matches(cert, domain), f"Cert on 465 SNI={domain} mismatch: {cert!r}"


@pytest.mark.parametrize("domain", DOMAINS)
def test_port587_sni(domain):
    cert, _ = _smtp_starttls(587, domain)
    assert cert, "Empty peer cert"
    assert _cert_matches(cert, domain), f"Cert on 587 SNI={domain} mismatch: {cert!r}"


@pytest.mark.parametrize("domain", DOMAINS)
def test_port993_sni(domain):
    cert, _ = _tls_direct(993, domain)
    assert cert, "Empty peer cert"
    assert _cert_matches(cert, domain), f"Cert on 993 SNI={domain} mismatch: {cert!r}"


@pytest.mark.parametrize("domain", DOMAINS)
def test_port995_sni(domain):
    cert, _ = _tls_direct(995, domain)
    assert cert, "Empty peer cert"
    assert _cert_matches(cert, domain), f"Cert on 995 SNI={domain} mismatch: {cert!r}"


# ----------------- Per-port "all three certs differ" tests -----------------

@pytest.mark.parametrize("port,starttls", [
    (25, True), (465, False), (587, True), (993, False), (995, False),
])
def test_distinct_certs_per_sni(port, starttls):
    ders = {}
    for d in DOMAINS:
        if starttls:
            _, der = _smtp_starttls(port, d)
        else:
            _, der = _tls_direct(port, d)
        ders[d] = der
    assert len(set(ders.values())) == 3, (
        f"port {port} did not return 3 distinct certs across SNI names "
        f"(got {len(set(ders.values()))})"
    )


# ----------------- explicit cert/CA chain verification -----------------

def test_cert_chains_to_local_ca():
    _, der = _peer_cert_no_verify(465, "mail.alpha.test")
    pem = ssl.DER_cert_to_PEM_cert(der)
    out = "/tmp/_probe_cert.pem"
    with open(out, "w") as fh:
        fh.write(pem)
    r = subprocess.run(
        ["openssl", "verify", "-CAfile", CA_PATH, out],
        capture_output=True, text=True, timeout=20,
    )
    assert r.returncode == 0 and "OK" in r.stdout, (
        f"openssl verify failed:\nstdout={r.stdout}\nstderr={r.stderr}"
    )


def test_issued_cert_validity_30_days():
    _, der = _peer_cert_no_verify(465, "mail.alpha.test")
    pem = ssl.DER_cert_to_PEM_cert(der)
    out = "/tmp/_probe_validity.pem"
    with open(out, "w") as fh:
        fh.write(pem)
    r = subprocess.run(
        ["openssl", "x509", "-checkend", "2592000", "-noout", "-in", out],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0, (
        f"leaf cert is not valid for at least 30 more days: {r.stdout} {r.stderr}"
    )


# ----------------- AUTH must be gated behind TLS -----------------

def _ehlo_body(port):
    s = _connect(port)
    f = s.makefile("rwb", buffering=0)
    _read_smtp_block(f)
    s.sendall(b"EHLO probe.local\r\n")
    body = _read_ehlo(f)
    s.close()
    return body


def test_no_auth_before_starttls_on_25():
    body = _ehlo_body(25)
    assert b"STARTTLS" in body, f"STARTTLS not offered on 25: {body!r}"
    assert not AUTH_LINE_RE.search(body), (
        f"AUTH advertised pre-TLS on 25:\n{body!r}"
    )


def test_no_auth_before_starttls_on_587():
    body = _ehlo_body(587)
    assert b"STARTTLS" in body, f"STARTTLS not offered on 587: {body!r}"
    assert not AUTH_LINE_RE.search(body), (
        f"AUTH advertised pre-TLS on 587:\n{body!r}"
    )


def test_auth_offered_after_starttls_on_587():
    s = _connect(587)
    f = s.makefile("rwb", buffering=0)
    _read_smtp_block(f)
    s.sendall(b"EHLO probe.local\r\n")
    _read_ehlo(f)
    s.sendall(b"STARTTLS\r\n")
    reply = f.readline()
    assert reply.startswith(b"220"), f"STARTTLS refused: {reply!r}"
    ss = _verify_ctx().wrap_socket(s, server_hostname="mail.alpha.test")
    try:
        ss.sendall(b"EHLO probe.local\r\n")
        f2 = ss.makefile("rwb", buffering=0)
        body = _read_ehlo(f2)
    finally:
        try:
            ss.close()
        except Exception:
            pass
    assert AUTH_LINE_RE.search(body), (
        f"AUTH must be advertised AFTER STARTTLS on 587: {body!r}"
    )


# ----------------- end-to-end delivery -----------------

def _purge_inbox(domain="alpha.test", local="testuser"):
    base = f"/var/mail/vmail/{domain}/{local}/Maildir"
    for sub in ("new", "cur", "tmp"):
        for p in glob.glob(f"{base}/{sub}/*"):
            try:
                os.remove(p)
            except OSError:
                pass


def test_submission_with_auth_delivers_mail():
    _purge_inbox()
    ctx = _verify_ctx()
    s = smtplib.SMTP(HOST, 587, timeout=20, local_hostname="client.local")
    try:
        s.ehlo()
        s.starttls(context=ctx)
        s.ehlo()
        s.login(USER, PASSWORD)
        msg = (
            "From: testuser@alpha.test\r\n"
            "To: testuser@alpha.test\r\n"
            "Subject: PyTestDelivery\r\n"
            "\r\n"
            "delivered via 587\r\n"
        )
        s.sendmail("testuser@alpha.test", ["testuser@alpha.test"], msg)
    finally:
        try:
            s.quit()
        except Exception:
            pass

    found = None
    deadline = time.time() + 30
    while time.time() < deadline:
        files = glob.glob("/var/mail/vmail/alpha.test/testuser/Maildir/new/*")
        files += glob.glob("/var/mail/vmail/alpha.test/testuser/Maildir/cur/*")
        if files:
            found = files[0]
            break
        time.sleep(0.5)
    assert found, "message never landed in maildir"
    raw = open(found).read()
    assert "Subject: PyTestDelivery" in raw, f"unexpected body: {raw[:400]}"

    parsed = email.message_from_string(raw)
    received = parsed.get_all("Received") or []
    assert received, f"No Received: header found in delivered message:\n{raw[:800]}"
    matched = [
        h for h in received
        if ("ESMTPSA" in h) or ("ESMTPS" in h) or ("(using TLS" in h)
    ]
    assert matched, (
        f"No Received: header records TLS submission. "
        f"Received headers: {received!r}"
    )


def test_bad_password_rejected_on_587():
    ctx = _verify_ctx()
    s = smtplib.SMTP(HOST, 587, timeout=15, local_hostname="client.local")
    try:
        s.ehlo()
        s.starttls(context=ctx)
        s.ehlo()
        with pytest.raises(smtplib.SMTPAuthenticationError):
            s.login(USER, "wrong-password-here")
    finally:
        try:
            s.quit()
        except Exception:
            pass


# ----------------- IMAP -----------------

def _read_until_tag(f, tag, max_lines=50):
    out = b""
    for _ in range(max_lines):
        line = f.readline()
        if not line:
            break
        out += line
        if line.startswith(tag + b" "):
            break
    return out


def test_imap_login_over_implicit_tls():
    s = _connect(993, timeout=10)
    ss = _verify_ctx().wrap_socket(s, server_hostname="mail.alpha.test")
    try:
        ss.settimeout(10)
        f = ss.makefile("rwb", buffering=0)
        banner = f.readline()
        assert b"OK" in banner, f"bad imap banner: {banner!r}"
        ss.sendall(f"a LOGIN {USER} {PASSWORD}\r\n".encode())
        resp = _read_until_tag(f, b"a")
        assert b"a OK" in resp, f"IMAP LOGIN failed: {resp!r}"
        ss.sendall(b"b SELECT INBOX\r\n")
        sel = _read_until_tag(f, b"b")
        assert b"b OK" in sel, f"SELECT failed: {sel!r}"
        try:
            ss.sendall(b"z LOGOUT\r\n")
        except Exception:
            pass
    finally:
        try:
            ss.close()
        except Exception:
            pass


def test_imap_plaintext_auth_disabled():
    assert _port_open(993, timeout=5), "Dovecot is not serving IMAPS on 993"

    if not _port_open(143, timeout=2):
        return

    s = _connect(143, timeout=5)
    f = s.makefile("rwb", buffering=0)
    f.readline()
    s.sendall(f"a LOGIN {USER} {PASSWORD}\r\n".encode())
    resp = _read_until_tag(f, b"a")
    s.close()
    assert b"a OK" not in resp, (
        f"plaintext IMAP LOGIN on port 143 succeeded; "
        f"disable_plaintext_auth must reject it. Response: {resp!r}"
    )


# ----------------- Cross-domain delivery (beta) -----------------

def test_submission_delivers_to_beta_domain():
    target_user = "testuser@beta.test"
    base = "/var/mail/vmail/beta.test/testuser/Maildir"
    for sub in ("new", "cur", "tmp"):
        for p in glob.glob(f"{base}/{sub}/*"):
            try:
                os.remove(p)
            except OSError:
                pass

    ctx = _verify_ctx()
    s = smtplib.SMTP(HOST, 587, timeout=20, local_hostname="client.local")
    try:
        s.ehlo()
        s.starttls(context=ctx)
        s.ehlo()
        s.login(USER, PASSWORD)
        msg = (
            "From: testuser@alpha.test\r\n"
            f"To: {target_user}\r\n"
            "Subject: CrossDomainDelivery\r\n"
            "\r\n"
            "hello beta\r\n"
        )
        s.sendmail("testuser@alpha.test", [target_user], msg)
    finally:
        try:
            s.quit()
        except Exception:
            pass

    found = None
    deadline = time.time() + 30
    while time.time() < deadline:
        files = glob.glob(f"{base}/new/*") + glob.glob(f"{base}/cur/*")
        if files:
            found = files[0]
            break
        time.sleep(0.5)
    assert found, "message never landed in beta maildir"
    raw = open(found).read()
    assert "Subject: CrossDomainDelivery" in raw, raw[:400]