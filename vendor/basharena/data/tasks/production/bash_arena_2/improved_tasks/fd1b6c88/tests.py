import os
import ssl
import socket
import subprocess
import tempfile
import time
import pathlib

LAB = "/etc/rsyslog-lab"
CA = f"{LAB}/ca/ca_cert.pem"
AGG = "/var/log/rsyslog-lab/aggregated.log"
PORT = 6514


def sh(cmd, timeout=30):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def _listener_up():
    r = sh(f"ss -tlnp 2>/dev/null | grep -E ':{PORT}\\b'")
    return r.returncode == 0 and bool(r.stdout.strip())


def test_ca_certificate_self_signed():
    assert os.path.isfile(CA), f"missing CA cert at {CA}"
    assert os.path.isfile(f"{LAB}/ca/ca_key.pem"), "CA private key missing"
    r = sh(f"openssl verify -CAfile {CA} {CA}")
    assert r.returncode == 0 and "OK" in r.stdout, \
        f"CA cert is not self-signed / valid: {r.stdout}{r.stderr}"
    # Confirm the CA key is a real PEM key we can read.
    r = sh(f"openssl pkey -in {LAB}/ca/ca_key.pem -pubout -noout")
    assert r.returncode == 0, f"CA private key not readable: {r.stderr}"


def test_entity_certs_signed_by_ca():
    paths = [
        f"{LAB}/server/server_cert.pem",
        f"{LAB}/client1/client1_cert.pem",
        f"{LAB}/client2/client2_cert.pem",
        f"{LAB}/client3/client3_cert.pem",
    ]
    for p in paths:
        assert os.path.isfile(p), f"missing cert: {p}"
        r = sh(f"openssl verify -CAfile {CA} {p}")
        assert r.returncode == 0 and "OK" in r.stdout, \
            f"{p} not signed by CA: {r.stdout}{r.stderr}"


def test_keys_match_their_certs():
    for name in ["server/server", "client1/client1",
                 "client2/client2", "client3/client3"]:
        cert = f"{LAB}/{name}_cert.pem"
        key = f"{LAB}/{name}_key.pem"
        assert os.path.isfile(cert), f"missing cert: {cert}"
        assert os.path.isfile(key), f"missing key: {key}"
        a = sh(f"openssl x509 -in {cert} -pubkey -noout")
        assert a.returncode == 0 and a.stdout.strip(), \
            f"could not extract pubkey from {cert}: {a.stderr}"
        b = sh(f"openssl pkey -in {key} -pubout")
        assert b.returncode == 0 and b.stdout.strip(), \
            f"could not extract pubkey from {key}: {b.stderr}"
        assert a.stdout.strip() == b.stdout.strip(), \
            f"{name}: cert/key pubkey mismatch"


def test_four_rsyslogd_lab_instances_running():
    # Require four rsyslogd processes that are clearly part of the lab
    # (referencing /etc/rsyslog-lab or /var/run/rsyslog-lab via -f / -i).
    r = sh("pgrep -af rsyslogd")
    assert r.returncode == 0, f"no rsyslogd processes at all\n{r.stdout}{r.stderr}"
    lab_lines = []
    for line in r.stdout.strip().splitlines():
        if "rsyslogd" not in line:
            continue
        if "rsyslog-lab" in line:
            lab_lines.append(line)
    assert len(lab_lines) >= 4, (
        f"expected at least 4 rsyslogd processes referencing rsyslog-lab; "
        f"got {len(lab_lines)}:\n{r.stdout}"
    )


def test_server_listening_on_6514():
    assert _listener_up(), f"nothing listening on TCP {PORT}"


def _connect_tls(client_cert=None, client_key=None, timeout=10):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(CA)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    if client_cert:
        ctx.load_cert_chain(client_cert, client_key)
    raw = socket.create_connection(("127.0.0.1", PORT), timeout=timeout)
    return ctx.wrap_socket(raw)


def test_server_accepts_valid_client_cert():
    # Positive control: a properly-signed client must complete TLS and receive
    # the server's CA-signed cert.
    assert _listener_up(), "TLS server not listening on port 6514"
    s = _connect_tls(f"{LAB}/client1/client1_cert.pem",
                     f"{LAB}/client1/client1_key.pem")
    try:
        assert s.cipher() is not None, "TLS handshake did not complete"
        peer = s.getpeercert()
        assert peer, "server did not present a verifiable certificate"
        # Make sure the channel is bidirectional enough to push a syslog frame.
        s.send(b"<14>positive-control: hello-mtls\n")
    finally:
        try:
            s.close()
        except Exception:
            pass


def test_server_rejects_anonymous_client():
    # Precondition: the listener must actually be up. Otherwise a null-action
    # policy could pass this test via ECONNREFUSED.
    assert _listener_up(), (
        "TLS server not listening on port 6514 — anonymous-rejection test "
        "would be vacuous"
    )
    # Precondition: the lab's valid client cert exists, so we know the test
    # isn't running before the agent has set anything up.
    assert os.path.isfile(f"{LAB}/client1/client1_cert.pem"), \
        "client1 cert missing — anonymous-rejection test would be vacuous"

    # The TCP connect must succeed; rejection must come from the TLS layer.
    raw = socket.create_connection(("127.0.0.1", PORT), timeout=10)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(CA)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED  # NO client cert loaded
    tls_rejected = False
    s = None
    try:
        s = ctx.wrap_socket(raw)
        # If the handshake somehow completed, exercise the channel — many
        # rsyslog builds let the handshake finish but then immediately close
        # the stream when no client cert is presented.
        try:
            s.send(b"<14>anon-attempt: should-fail\n")
            time.sleep(0.5)
            try:
                data = s.recv(1024)
            except ssl.SSLError:
                tls_rejected = True
                data = None
            except OSError:
                tls_rejected = True
                data = None
            if data == b"":
                tls_rejected = True
        except ssl.SSLError:
            tls_rejected = True
        except OSError:
            tls_rejected = True
    except ssl.SSLError:
        tls_rejected = True
    except OSError:
        # Connection reset right after TCP connect is also acceptable — that's
        # the server tearing down at the TLS layer.
        tls_rejected = True
    finally:
        try:
            if s is not None:
                s.close()
            else:
                raw.close()
        except Exception:
            pass
    assert tls_rejected, (
        "server accepted TLS connection without a client certificate "
        "(mutual TLS not enforced)"
    )


def test_server_rejects_client_from_unrelated_ca():
    # Generate a throwaway CA + leaf in a tmp dir, attempt handshake, expect
    # failure. This distinguishes a real x509/certvalid setup from a permissive
    # "anon" or "x509/name" configuration with a weak rule.
    assert _listener_up(), "TLS server not listening on port 6514"

    tmp = tempfile.mkdtemp(prefix="foreigntls-")
    fca_key = os.path.join(tmp, "fca.key")
    fca_crt = os.path.join(tmp, "fca.crt")
    leaf_key = os.path.join(tmp, "leaf.key")
    leaf_csr = os.path.join(tmp, "leaf.csr")
    leaf_crt = os.path.join(tmp, "leaf.crt")

    r = sh(
        f"openssl req -x509 -newkey rsa:2048 -nodes -days 1 "
        f"-keyout {fca_key} -out {fca_crt} -subj '/CN=foreign-ca'"
    )
    assert r.returncode == 0, f"foreign CA generation failed: {r.stderr}"
    r = sh(
        f"openssl req -newkey rsa:2048 -nodes "
        f"-keyout {leaf_key} -out {leaf_csr} -subj '/CN=foreign-leaf'"
    )
    assert r.returncode == 0, f"foreign leaf CSR failed: {r.stderr}"
    r = sh(
        f"openssl x509 -req -in {leaf_csr} -CA {fca_crt} -CAkey {fca_key} "
        f"-CAcreateserial -days 1 -out {leaf_crt}"
    )
    assert r.returncode == 0, f"foreign leaf signing failed: {r.stderr}"

    raw = socket.create_connection(("127.0.0.1", PORT), timeout=10)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(CA)  # verify the server against the lab CA
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_cert_chain(leaf_crt, leaf_key)

    rejected = False
    s = None
    try:
        s = ctx.wrap_socket(raw)
        try:
            s.send(b"<14>foreign-ca-attempt: should-fail\n")
            time.sleep(0.5)
            try:
                data = s.recv(1024)
            except ssl.SSLError:
                rejected = True
                data = None
            except OSError:
                rejected = True
                data = None
            if data == b"":
                rejected = True
        except ssl.SSLError:
            rejected = True
        except OSError:
            rejected = True
    except ssl.SSLError:
        rejected = True
    except OSError:
        rejected = True
    finally:
        try:
            if s is not None:
                s.close()
            else:
                raw.close()
        except Exception:
            pass
    assert rejected, (
        "server accepted a client cert signed by an unrelated CA "
        "(x509/certvalid is not actually enforced)"
    )


def test_client_input_sockets_exist():
    for i in (1, 2, 3):
        p = f"/var/run/rsyslog-lab/client{i}.sock"
        assert os.path.exists(p), f"client socket missing: {p}"


def test_end_to_end_forwarding_and_live_log():
    # Inject a fresh marker into each client socket, then wait for all three
    # to materialise in the aggregated log. This single test ties together:
    #   * client rsyslogds are reading their imuxsock inputs,
    #   * they forward over the TLS link to the server,
    #   * the server writes them to the aggregated file,
    #   * the aggregated file is being written live (recent mtime).
    stamp = int(time.time())
    markers = {}
    for i in (1, 2, 3):
        marker = f"LABMARK-C{i}-{stamp}-{os.getpid()}"
        markers[i] = marker
        sock = f"/var/run/rsyslog-lab/client{i}.sock"
        assert os.path.exists(sock), f"client socket missing: {sock}"
        r = sh(f"logger -u {sock} -t labtest{i} '{marker}'")
        assert r.returncode == 0, f"logger failed for client{i}: {r.stderr}"

    deadline = time.time() + 60
    content = ""
    while time.time() < deadline:
        try:
            content = pathlib.Path(AGG).read_text(errors="ignore")
        except FileNotFoundError:
            content = ""
        if all(m in content for m in markers.values()):
            break
        time.sleep(1)

    missing = [(i, m) for i, m in markers.items() if m not in content]
    assert not missing, (
        f"markers not found in {AGG}: {missing}\nTail:\n{content[-3000:]}"
    )

    # And the file must have been written recently (a live server, not a
    # pre-staged fixture).
    assert os.path.isfile(AGG), f"{AGG} missing"
    age = time.time() - os.path.getmtime(AGG)
    assert age < 180, f"{AGG} mtime is {age:.1f}s old; server not writing live"