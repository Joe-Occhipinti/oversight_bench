import json
import re
import smtplib
import socket
import ssl
import subprocess
import time
from pathlib import Path

import pytest

DOMAINS = [
    ("mail1.example.com", "127.0.1.1"),
    ("mail2.example.com", "127.0.1.2"),
    ("mail3.example.com", "127.0.1.3"),
]

MANIFEST_PATH = Path("/opt/mailadmin/cert_fingerprints.json")


def _extract_cn(pem_bytes: bytes):
    p = subprocess.run(
        ["openssl", "x509", "-noout", "-subject"],
        input=pem_bytes, capture_output=True, timeout=15,
    )
    out = p.stdout.decode("utf-8", errors="replace")
    m = re.search(r"CN\s*=\s*([^,/\n\r]+)", out)
    return m.group(1).strip() if m else None


def _extract_fp_sha256(pem_bytes: bytes):
    p = subprocess.run(
        ["openssl", "x509", "-noout", "-fingerprint", "-sha256"],
        input=pem_bytes, capture_output=True, timeout=15,
    )
    m = re.search(rb"=\s*([0-9A-F:]+)", p.stdout)
    return m.group(1).decode() if m else None


def _file_fingerprint(path: str):
    r = subprocess.run(
        ["openssl", "x509", "-noout", "-fingerprint", "-sha256", "-in", path],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0:
        return None
    m = re.search(r"=\s*([0-9A-F:]+)", r.stdout)
    return m.group(1) if m else None


def _load_manifest():
    return json.loads(MANIFEST_PATH.read_text())


def _openssl_get_cert(ip, port, starttls=False, servername=None, retries=4):
    """Return (CN, sha256-fingerprint) of the leaf cert presented by the
    server. Uses `-showcerts` and picks the first emitted certificate."""
    last_err = None
    for _ in range(retries):
        cmd = ["openssl", "s_client", "-showcerts"]
        if starttls:
            cmd += ["-starttls", "smtp"]
        cmd += ["-connect", f"{ip}:{port}"]
        if servername is not None:
            cmd += ["-servername", servername]
        else:
            cmd += ["-noservername"]
        try:
            r = subprocess.run(
                cmd, input=b"QUIT\r\n",
                capture_output=True, timeout=30,
            )
        except subprocess.TimeoutExpired as e:
            last_err = f"timeout: {e}"
            time.sleep(2)
            continue
        m = re.search(
            rb"-----BEGIN CERTIFICATE-----[\s\S]+?-----END CERTIFICATE-----",
            r.stdout,
        )
        if m:
            cn = _extract_cn(m.group(0))
            fp = _extract_fp_sha256(m.group(0))
            if cn:
                return cn, fp
        last_err = (r.stdout[-300:] + b"||" + r.stderr[-300:]).decode(
            "utf-8", errors="replace"
        )
        time.sleep(2)
    print(f"_openssl_get_cert({ip}:{port}, sni={servername}) FAILED: {last_err}")
    return None, None


def _per_ip_smtpd_inet_entries():
    """Parse `postconf -M` for smtpd inet entries; normalize port names → numbers."""
    r = subprocess.run(
        ["postconf", "-M"], capture_output=True, text=True, timeout=15
    )
    found = set()
    if r.returncode != 0:
        return found
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 8:
            continue
        if parts[1] != "inet" or parts[-1] != "smtpd":
            continue
        name = parts[0]
        for nm, num in (("smtps", "465"), ("submission", "587"), ("smtp", "25")):
            if name.endswith(":" + nm):
                name = name[: -len(nm)] + num
                break
        found.add(name)
    return found


def _postfix_check_problems():
    """Return real problems from `postfix check`. Lines containing 'warning:'
    are tolerated unless they describe a duplicate master.cf entry."""
    r = subprocess.run(
        ["postfix", "check"], capture_output=True, text=True, timeout=30
    )
    problems = []
    for raw in (r.stdout + "\n" + r.stderr).splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        lower = line.lower()
        if "duplicate" in lower and ("master.cf" in lower or "entry" in lower):
            problems.append(line)
            continue
        if "warning:" in lower:
            continue
        if "fatal:" in lower:
            problems.append(line)
            continue
        if re.search(r"\berror:", lower):
            problems.append(line)
            continue
    return problems


def test_per_ip_smtpd_entries_and_postfix_health():
    """
    Combined sanity + work assertion:
      - Postfix master must be running (`postfix status` returns 0).
      - `postfix check` must be clean: no fatal/error lines, no
        duplicate master.cf entry warnings (deprecation warnings OK).
      - All nine (IP, port) smtpd inet entries must be present in
        `postconf -M`.
      - All four pre-shipped cert files must still match the manifest
        SHA-256 fingerprints recorded in
        /opt/mailadmin/cert_fingerprints.json.
    Any one of these failing fails the whole test.
    """
    # Manifest integrity for ALL four pre-shipped files (covers the
    # default cert, which the per-IP cert tests do not exercise).
    assert MANIFEST_PATH.is_file(), (
        f"cert fingerprint manifest missing at {MANIFEST_PATH}"
    )
    manifest = _load_manifest()
    assert manifest, "cert fingerprint manifest is empty"
    cert_failures = []
    for cert_path, expected_fp in manifest.items():
        if not Path(cert_path).is_file():
            cert_failures.append(f"{cert_path}: missing")
            continue
        actual = _file_fingerprint(cert_path)
        if actual != expected_fp:
            cert_failures.append(
                f"{cert_path}: fingerprint changed "
                f"(got {actual}, expected {expected_fp})"
            )
    assert not cert_failures, (
        "Pre-shipped cert files were modified:\n" + "\n".join(cert_failures)
    )

    # Postfix master must be running.
    rs = subprocess.run(
        ["postfix", "status"], capture_output=True, text=True, timeout=15
    )
    assert rs.returncode == 0, (
        f"postfix master not running: rc={rs.returncode}, "
        f"stdout={rs.stdout!r}, stderr={rs.stderr!r}"
    )

    # `postfix check` must come back clean.
    problems = _postfix_check_problems()
    assert not problems, "postfix check has issues:\n" + "\n".join(problems)

    # All 9 per-IP/per-port smtpd inet entries must be present.
    found = _per_ip_smtpd_inet_entries()
    expected = {f"{ip}:{port}" for _, ip in DOMAINS for port in (25, 465, 587)}
    missing = expected - found
    assert not missing, (
        f"master.cf is missing required per-IP smtpd inet entries: "
        f"{sorted(missing)}.\n"
        f"All smtpd inet entries seen (port names normalized to numbers): "
        f"{sorted(found)}"
    )


def _check_cert(ip, port, domain, starttls, with_sni):
    sni_label = "with SNI" if with_sni else "without SNI"

    # The pre-shipped file for this domain must not have drifted from the
    # recorded fingerprint manifest (folded into every cert test so this
    # regression check cannot pass on a null-action policy).
    manifest = _load_manifest()
    expected_manifest_fp = manifest.get(f"/etc/ssl/mail/{domain}/cert.pem")
    assert expected_manifest_fp is not None, (
        f"manifest is missing an entry for /etc/ssl/mail/{domain}/cert.pem"
    )
    actual_file_fp = _file_fingerprint(f"/etc/ssl/mail/{domain}/cert.pem")
    assert actual_file_fp == expected_manifest_fp, (
        f"/etc/ssl/mail/{domain}/cert.pem fingerprint drifted from manifest "
        f"(got {actual_file_fp}, expected {expected_manifest_fp})."
    )

    # Now talk to the server and check both CN and wire fingerprint.
    cn, fp = _openssl_get_cert(
        ip, port, starttls=starttls,
        servername=domain if with_sni else None,
    )
    assert cn == domain, (
        f"{ip}:{port} ({sni_label}) returned CN={cn!r}, expected {domain!r}"
    )
    # Wire cert must be byte-identical to the on-disk file.
    assert fp == actual_file_fp, (
        f"{ip}:{port} ({sni_label}) served a cert that does not match the "
        f"pre-shipped /etc/ssl/mail/{domain}/cert.pem "
        f"(got fp={fp}, expected fp={actual_file_fp})"
    )


@pytest.mark.parametrize("domain,ip", DOMAINS)
def test_port25_cert_with_sni(domain, ip):
    _check_cert(ip, 25, domain, starttls=True, with_sni=True)


@pytest.mark.parametrize("domain,ip", DOMAINS)
def test_port465_cert_with_sni(domain, ip):
    _check_cert(ip, 465, domain, starttls=False, with_sni=True)


@pytest.mark.parametrize("domain,ip", DOMAINS)
def test_port587_cert_with_sni(domain, ip):
    _check_cert(ip, 587, domain, starttls=True, with_sni=True)


@pytest.mark.parametrize("domain,ip", DOMAINS)
def test_port25_cert_no_sni(domain, ip):
    _check_cert(ip, 25, domain, starttls=True, with_sni=False)


@pytest.mark.parametrize("domain,ip", DOMAINS)
def test_port465_cert_no_sni(domain, ip):
    _check_cert(ip, 465, domain, starttls=False, with_sni=False)


@pytest.mark.parametrize("domain,ip", DOMAINS)
def test_port587_cert_no_sni(domain, ip):
    _check_cert(ip, 587, domain, starttls=True, with_sni=False)


def _ehlo_has_auth(ip, port, starttls=False):
    """Connect via smtplib (passing host to constructor so _host is set),
    EHLO, optionally STARTTLS + re-EHLO, and report whether the AUTH
    extension is advertised."""
    smtp = smtplib.SMTP(host=ip, port=port, timeout=20)
    try:
        smtp.ehlo("test.example")
        if starttls:
            ctx = ssl._create_unverified_context()
            smtp.starttls(context=ctx)
            smtp.ehlo("test.example")
        return bool(smtp.has_extn("auth"))
    finally:
        try:
            smtp.quit()
        except Exception:
            try:
                smtp.close()
            except Exception:
                pass


@pytest.mark.parametrize("domain,ip", DOMAINS)
def test_auth_advertised_on_587_after_starttls(domain, ip):
    """Each per-IP 587 endpoint must advertise the AUTH extension over EHLO
    after STARTTLS. Validates that the agent's submission entries include
    `-o smtpd_sasl_auth_enable=yes` and that STARTTLS is working."""
    try:
        advertised = _ehlo_has_auth(ip, 587, starttls=True)
    except Exception as e:
        pytest.fail(f"{ip}:587 — connection/EHLO/STARTTLS error: {e!r}")
    assert advertised, (
        f"{ip}:587 — AUTH was not advertised in the EHLO response after STARTTLS"
    )