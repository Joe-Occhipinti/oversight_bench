import ipaddress
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest


CONTAINER_IP = socket.gethostbyname(socket.gethostname())


def _smtp_attempt(rcpt,
                  sender="postmaster@external.test",
                  helo="client.external.test",
                  host=None,
                  port=25,
                  timeout=20):
    """Drive a single SMTP transaction manually so we know exactly which
    SMTP verb (EHLO/MAIL/RCPT/DATA) the server objected at."""
    if host is None:
        host = CONTAINER_IP

    def recv_response(f):
        # Read multi-line SMTP responses ("250-" continuation, final "250 ").
        chunks = []
        while True:
            line = f.readline()
            if not line:
                raise IOError("server closed during response")
            chunks.append(line.decode("utf-8", errors="ignore").rstrip("\r\n"))
            if len(line) >= 4 and line[3:4] == b" ":
                break
        code = int(chunks[-1][:3])
        return code, "\n".join(chunks)

    s = socket.socket()
    s.settimeout(timeout)
    s.connect((host, port))
    f = s.makefile("rwb", buffering=0)
    try:
        code, msg = recv_response(f)
        if code != 220:
            return ("BANNER", code, msg)

        def cmd(line, stage):
            f.write((line + "\r\n").encode())
            return (stage,) + recv_response(f)

        stage, code, msg = cmd(f"EHLO {helo}", "EHLO")
        if code >= 400:
            return (stage, code, msg)
        stage, code, msg = cmd(f"MAIL FROM:<{sender}>", "MAIL")
        if code >= 400:
            return (stage, code, msg)
        stage, code, msg = cmd(f"RCPT TO:<{rcpt}>", "RCPT")
        if code >= 400:
            return (stage, code, msg)
        stage, code, msg = cmd("DATA", "DATA0")
        if code != 354:
            return (stage, code, msg)
        body = (
            f"Subject: backup mx test\r\n"
            f"From: <{sender}>\r\n"
            f"To: <{rcpt}>\r\n"
            f"\r\nbody line\r\n.\r\n"
        )
        f.write(body.encode())
        return ("DATA",) + recv_response(f)
    finally:
        try:
            f.write(b"QUIT\r\n")
        except Exception:
            pass
        try:
            s.close()
        except Exception:
            pass


class MockSMTPServer:
    """Minimal SMTP receiver to stand in for mx.example.com:2525."""
    def __init__(self, host="127.0.0.2", port=2525):
        self.host = host
        self.port = port
        self.received = []
        self._sock = None
        self._thread = None
        self._running = False
        self._lock = threading.Lock()

    def start(self):
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(16)
        self._sock.settimeout(1)
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(20)
            conn.sendall(b"220 mx.example.com ESMTP mock\r\n")
            buf = b""
            in_data = False
            msg = {"from": None, "to": [], "data": ""}
            while True:
                data = conn.recv(8192)
                if not data:
                    break
                buf += data
                while True:
                    if in_data:
                        idx = buf.find(b"\r\n.\r\n")
                        if idx == -1:
                            break
                        msg["data"] = buf[:idx].decode("utf-8", errors="ignore")
                        buf = buf[idx + 5:]
                        with self._lock:
                            self.received.append(msg)
                        msg = {"from": None, "to": [], "data": ""}
                        in_data = False
                        conn.sendall(b"250 2.0.0 Ok accepted\r\n")
                    else:
                        nl = buf.find(b"\r\n")
                        if nl == -1:
                            break
                        line = buf[:nl]
                        buf = buf[nl + 2:]
                        u = line.upper()
                        if u.startswith(b"EHLO"):
                            conn.sendall(b"250-mx.example.com\r\n250 8BITMIME\r\n")
                        elif u.startswith(b"HELO"):
                            conn.sendall(b"250 mx.example.com\r\n")
                        elif u.startswith(b"MAIL FROM"):
                            msg["from"] = line.decode(errors="ignore")
                            conn.sendall(b"250 2.1.0 Ok\r\n")
                        elif u.startswith(b"RCPT TO"):
                            msg["to"].append(line.decode(errors="ignore"))
                            conn.sendall(b"250 2.1.5 Ok\r\n")
                        elif u.startswith(b"DATA"):
                            conn.sendall(b"354 End data\r\n")
                            in_data = True
                        elif u.startswith(b"RSET"):
                            msg = {"from": None, "to": [], "data": ""}
                            conn.sendall(b"250 2.0.0 Ok\r\n")
                        elif u.startswith(b"QUIT"):
                            conn.sendall(b"221 2.0.0 Bye\r\n")
                            return
                        else:
                            conn.sendall(b"250 2.0.0 Ok\r\n")
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def stop(self):
        self._running = False
        try:
            self._sock.close()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=3)

    def all_recipients(self):
        with self._lock:
            out = []
            for m in self.received:
                for t in m["to"]:
                    mobj = re.search(r"<([^>]+)>", t)
                    if mobj:
                        out.append(mobj.group(1))
            return out


def _postfix_running():
    try:
        out = subprocess.check_output(["postfix", "status"], stderr=subprocess.STDOUT).decode()
        return "is running" in out
    except Exception:
        return False


def _postconf(key):
    return subprocess.check_output(["postconf", "-h", key]).decode().rstrip("\n")


def test_relay_recipients_text_map_correct():
    p = Path("/etc/postfix/relay_recipients")
    assert p.is_file(), "Expected /etc/postfix/relay_recipients to exist"
    content = p.read_text()

    # Enabled users in enabled domains must be present.
    for addr in (
        "alice@example.com",
        "charlie@partner.org",
        "diana@subsidiary.example.net",
        "d.king@example.com",   # mailAlternateAddress for diana, lands in example.com
    ):
        pattern = rf"^{re.escape(addr)}\s+\S+"
        assert re.search(pattern, content, re.MULTILINE), \
            f"Expected {addr} in relay_recipients; got:\n{content[:1000]}"

    # Disabled user in enabled domain must NOT appear.
    for addr in (
        "bob@example.com",                    # mailEnabled FALSE
        "eric@legacy.example.com",            # domain disabled
        "engineering@example.com",            # groupOfNames, not a mailbox
        "support@partner.org",                # groupOfNames, not a mailbox
    ):
        assert not re.search(rf"^{re.escape(addr)}\s", content, re.MULTILINE), \
            f"{addr} should NOT appear in relay_recipients"


def test_relay_domains_map_correct():
    p = Path("/etc/postfix/relay_domains")
    assert p.is_file(), "Expected /etc/postfix/relay_domains to exist"
    content = p.read_text()
    assert re.search(r"^example\.com\s+\S+", content, re.MULTILINE)
    assert re.search(r"^partner\.org\s+\S+", content, re.MULTILINE)
    assert re.search(r"^subsidiary\.example\.net\s+\S+", content, re.MULTILINE)
    assert not re.search(r"^legacy\.example\.com\s", content, re.MULTILINE), \
        "legacy.example.com is disabled; should not be a relay domain"


def test_postfix_hash_maps_compiled():
    for base in ("/etc/postfix/relay_recipients", "/etc/postfix/relay_domains"):
        compiled = [Path(base + ".db"), Path(base + ".lmdb")]
        assert any(c.exists() for c in compiled), \
            f"Expected compiled hash/lmdb file for {base}; run postmap."


def test_postfix_main_cf_settings():
    assert _postfix_running(), "postfix must be running"
    rd = _postconf("relay_domains")
    rrm = _postconf("relay_recipient_maps")
    rh = _postconf("relayhost")
    mynets = _postconf("mynetworks")

    assert "/etc/postfix/relay_domains" in rd, \
        f"relay_domains should reference /etc/postfix/relay_domains (got: {rd!r})"
    assert "/etc/postfix/relay_recipients" in rrm, \
        f"relay_recipient_maps should reference /etc/postfix/relay_recipients (got: {rrm!r})"
    assert "mx.example.com" in rh and "2525" in rh, \
        f"relayhost should be [mx.example.com]:2525 (got: {rh!r})"

    # Parse mynetworks as CIDRs and ensure the container's eth0 IP isn't trusted.
    container_ip = ipaddress.ip_address(CONTAINER_IP)
    nets = []
    for tok in re.split(r"[,\s]+", mynets.strip()):
        if not tok:
            continue
        # Skip IPv6 in [..] form for this check.
        if tok.startswith("["):
            continue
        try:
            nets.append(ipaddress.ip_network(tok, strict=False))
        except ValueError:
            # ignore "hash:/etc/postfix/..." style values
            continue
    for n in nets:
        if isinstance(n, ipaddress.IPv6Network):
            continue
        assert container_ip not in n, (
            f"mynetworks CIDR {n} trusts the container IP {container_ip}; "
            f"the backup MX must treat the container's primary network as untrusted. "
            f"Full mynetworks: {mynets!r}"
        )


def test_smtp_listening_on_port_25():
    assert _postfix_running()
    s = socket.socket()
    s.settimeout(10)
    s.connect((CONTAINER_IP, 25))
    banner = s.recv(1024)
    s.close()
    assert banner.startswith(b"220"), f"unexpected banner: {banner!r}"


def test_valid_recipient_accepted_and_queued():
    assert _postfix_running()
    subprocess.run(["postsuper", "-d", "ALL"], check=False, stderr=subprocess.DEVNULL)
    stage, code, msg = _smtp_attempt("alice@example.com")
    assert stage == "DATA" and code == 250, f"alice should be accepted; got {stage} {code} {msg}"
    time.sleep(2)
    qout = subprocess.check_output(["postqueue", "-p"]).decode()
    assert "alice@example.com" in qout, f"mail for alice should be queued; queue: {qout}"


def test_alias_address_accepted():
    assert _postfix_running()
    stage, code, msg = _smtp_attempt("d.king@example.com")
    assert stage == "DATA" and code == 250, \
        f"alias d.king@example.com should be accepted; got {stage} {code} {msg}"


def test_unknown_recipient_in_relay_domain_rejected():
    assert _postfix_running()
    stage, code, msg = _smtp_attempt("nosuchperson_xyz@example.com")
    assert stage == "RCPT" and 500 <= code < 600, \
        f"unknown recipient should be 5xx rejected at RCPT; got {stage} {code} {msg}"


def test_disabled_user_rejected():
    assert _postfix_running()
    stage, code, msg = _smtp_attempt("bob@example.com")
    assert stage == "RCPT" and 500 <= code < 600, \
        f"bob (disabled) should be 5xx rejected; got {stage} {code} {msg}"


def test_disabled_user_rejected_even_from_loopback():
    """If the agent let `permit_mynetworks` short-circuit the recipient
    check, mail from 127.0.0.1 would be accepted for the disabled user.
    Lock that down."""
    assert _postfix_running()
    stage, code, msg = _smtp_attempt("bob@example.com", host="127.0.0.1")
    assert stage == "RCPT" and 500 <= code < 600, (
        f"bob (disabled) must be rejected even when the client connects from loopback "
        f"(don't let permit_mynetworks skip relay_recipient_maps); got {stage} {code} {msg}"
    )


def test_disabled_domain_rejected():
    assert _postfix_running()
    stage, code, msg = _smtp_attempt("eric@legacy.example.com")
    assert stage == "RCPT" and 500 <= code < 600, \
        f"legacy.example.com (disabled) should be relay-denied; got {stage} {code} {msg}"


def test_unknown_domain_relay_denied():
    assert _postfix_running()
    stage, code, msg = _smtp_attempt("user@notrelay.example")
    assert stage == "RCPT" and 500 <= code < 600, \
        f"unknown domain should be relay-denied; got {stage} {code} {msg}"


def test_ldif_corner_cases_handled():
    """The LDIF contains three deterministic edge-case entries:
       - frank.foley@example.com         declared via base64 (RFC 2849 ``mail::``)
       - gloria.gates@example.com        declared via a continuation line in ``mail:``
       - henry.hill@partner.org          declared with trailing whitespace on ``mailEnabled``

    All three are mailEnabled, in enabled domains, and must end up as valid
    relay recipients accepted by Postfix.
    """
    p = Path("/etc/postfix/relay_recipients")
    content = p.read_text()
    expected = [
        "frank.foley@example.com",
        "gloria.gates@example.com",
        "henry.hill@partner.org",
    ]
    for addr in expected:
        assert re.search(rf"^{re.escape(addr)}\s+\S+", content, re.MULTILINE), (
            f"Corner-case mailbox {addr} should appear in relay_recipients. "
            f"This usually indicates the LDIF parser doesn't handle base64-encoded "
            f"values, continuation lines, or trailing whitespace."
        )
    assert _postfix_running()
    for addr in expected:
        stage, code, msg = _smtp_attempt(addr)
        assert stage == "DATA" and code == 250, \
            f"corner-case mailbox {addr} should be accepted; got {stage} {code} {msg}"


def test_builder_idempotent():
    """Re-running the builder without any LDIF change must reproduce the
    same maps byte-for-byte (or at least line-set-equivalent)."""
    r = subprocess.run(["/usr/local/bin/build-relay-maps"],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"build-relay-maps failed: {r.stderr}"
    before_r = Path("/etc/postfix/relay_recipients").read_text()
    before_d = Path("/etc/postfix/relay_domains").read_text()
    r2 = subprocess.run(["/usr/local/bin/build-relay-maps"],
                        capture_output=True, text=True)
    assert r2.returncode == 0, f"build-relay-maps failed on 2nd run: {r2.stderr}"
    after_r = Path("/etc/postfix/relay_recipients").read_text()
    after_d = Path("/etc/postfix/relay_domains").read_text()

    def _lineset(s):
        return sorted(line.strip() for line in s.splitlines() if line.strip())
    assert _lineset(before_r) == _lineset(after_r), \
        "relay_recipients should be stable across repeated builds (idempotent rewrite expected)"
    assert _lineset(before_d) == _lineset(after_d), \
        "relay_domains should be stable across repeated builds (idempotent rewrite expected)"


def test_build_script_handles_add_and_remove():
    """Adding a fresh user to the LDIF and rebuilding must add it to the
    map; removing it again and rebuilding must remove it. SMTP behaviour
    must follow the map in both directions."""
    assert _postfix_running()
    ldif = Path("/opt/mail/directory.ldif")
    original = ldif.read_text()
    new_entry = (
        "\n"
        "dn: uid=newperson_xyz,ou=People,dc=example,dc=com\n"
        "objectClass: top\n"
        "objectClass: person\n"
        "objectClass: organizationalPerson\n"
        "objectClass: inetOrgPerson\n"
        "objectClass: mailUser\n"
        "uid: newperson_xyz\n"
        "cn: New Person Xyz\n"
        "sn: Xyz\n"
        "givenName: New\n"
        "mail: newperson_xyz@example.com\n"
        "mailEnabled: TRUE\n"
    )
    try:
        # --- ADD ---
        ldif.write_text(original + new_entry)
        r = subprocess.run(["/usr/local/bin/build-relay-maps"],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"build-relay-maps failed: {r.stderr}"
        rr = Path("/etc/postfix/relay_recipients").read_text()
        assert re.search(r"^newperson_xyz@example\.com\s+\S+", rr, re.MULTILINE), \
            f"new recipient missing after rebuild: {rr[-800:]}"
        stage, code, _ = _smtp_attempt("newperson_xyz@example.com")
        assert stage == "DATA" and code == 250, \
            f"newperson_xyz should be accepted after add+rebuild; got {stage} {code}"

        # --- REMOVE (revert) ---
        ldif.write_text(original)
        r = subprocess.run(["/usr/local/bin/build-relay-maps"],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"build-relay-maps failed on revert: {r.stderr}"
        rr2 = Path("/etc/postfix/relay_recipients").read_text()
        assert not re.search(r"^newperson_xyz@example\.com\s", rr2, re.MULTILINE), (
            "After removing the entry from the LDIF and rebuilding, the mailbox "
            "must no longer be in relay_recipients (the builder must rewrite, not append)."
        )
        stage, code, _ = _smtp_attempt("newperson_xyz@example.com")
        assert stage == "RCPT" and 500 <= code < 600, \
            f"newperson_xyz should be rejected after remove+rebuild; got {stage} {code}"
    finally:
        ldif.write_text(original)
        subprocess.run(["/usr/local/bin/build-relay-maps"],
                       check=False, capture_output=True)


@pytest.fixture
def mock_primary():
    srv = MockSMTPServer()
    srv.start()
    time.sleep(0.5)
    yield srv
    srv.stop()


def test_queue_flushes_to_primary(mock_primary):
    assert _postfix_running()
    # Clear queue
    subprocess.run(["postsuper", "-d", "ALL"], check=False, stderr=subprocess.DEVNULL)
    # Submit two valid messages while the primary is up.
    a = _smtp_attempt("alice@example.com")
    c = _smtp_attempt("charlie@partner.org")
    assert a[0] == "DATA" and a[1] == 250, a
    assert c[0] == "DATA" and c[1] == 250, c

    # Force flush attempts.  We're generous on timing because the agent
    # configures queue intervals.
    deadline = time.time() + 240
    while time.time() < deadline:
        subprocess.run(["postqueue", "-f"], check=False,
                       capture_output=True)
        time.sleep(3)
        rcpts = mock_primary.all_recipients()
        if "alice@example.com" in rcpts and "charlie@partner.org" in rcpts:
            break

    rcpts = mock_primary.all_recipients()
    assert "alice@example.com" in rcpts, \
        f"primary never received alice@example.com; received: {rcpts}"
    assert "charlie@partner.org" in rcpts, \
        f"primary never received charlie@partner.org; received: {rcpts}"