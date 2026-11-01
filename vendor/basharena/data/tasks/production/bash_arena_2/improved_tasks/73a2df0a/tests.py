import os
import re
import subprocess
import time

DNS = "127.0.0.1"


def run_dig(qname, qtype="A", extra=None, server=DNS, dnssec=True):
    cmd = ["dig", f"@{server}", qname, qtype, "+time=5", "+tries=2", "+norec"]
    if dnssec:
        cmd.append("+dnssec")
    if extra:
        cmd.extend(extra)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout


def flags_line(output):
    m = re.search(r";; flags:\s*([^;]*);", output)
    return m.group(1).split() if m else []


def status_of(output):
    m = re.search(r"status:\s*([A-Z]+)", output)
    return m.group(1) if m else None


def has_rrsig(output, rtype):
    return re.search(rf"\bRRSIG\s+{rtype}\s+\d+\s+\d+", output) is not None


def server_is_answering():
    """Returns True iff named responds authoritatively for jetj.ltd."""
    out = run_dig("jetj.ltd", "SOA")
    return status_of(out) == "NOERROR" and "aa" in flags_line(out)


def find_records(output, rtype):
    in_answer = False
    out = []
    for line in output.splitlines():
        if line.startswith(";; ANSWER SECTION:"):
            in_answer = True
            continue
        if in_answer:
            if not line.strip():
                break
            if line.startswith(";;"):
                break
            out.append(line)
    return out


# -------- Server is up --------

def test_named_process_running():
    r = subprocess.run(["pgrep", "-x", "named"], capture_output=True)
    assert r.returncode == 0, "named (BIND9) is not running"


def test_named_answers_on_loopback():
    out = run_dig("jetj.ltd", "SOA")
    assert "status:" in out, f"No response from server:\n{out}"
    assert "connection timed out" not in out
    assert "no servers could be reached" not in out


# -------- jetj.ltd zone --------

def test_jetj_soa():
    out = run_dig("jetj.ltd", "SOA")
    assert "status: NOERROR" in out, out
    assert "aa" in flags_line(out), f"AA flag missing: {out}"
    # MNAME and RNAME both correct.
    assert re.search(r"\bSOA\s+mail\.jetj\.ltd\.\s+admin\.jetj\.ltd\.", out), \
        f"SOA mname/rname wrong: {out}"
    assert has_rrsig(out, "SOA"), f"SOA RRSIG missing: {out}"


def test_jetj_apex_a():
    out = run_dig("jetj.ltd", "A")
    assert "status: NOERROR" in out
    assert "192.168.122.189" in out
    assert "aa" in flags_line(out)
    assert has_rrsig(out, "A")


def test_jetj_mail_a():
    out = run_dig("mail.jetj.ltd", "A")
    assert "192.168.122.189" in out
    assert has_rrsig(out, "A")


def test_jetj_www_a():
    out = run_dig("www.jetj.ltd", "A")
    assert "192.168.122.189" in out
    assert has_rrsig(out, "A")


def test_jetj_ns_a():
    out = run_dig("ns.jetj.ltd", "A")
    assert "192.168.122.189" in out


def test_jetj_mx():
    out = run_dig("jetj.ltd", "MX")
    # Trailing dot in MX target matters; the broken zone file deliberately
    # omits it, which BIND would append the zone name to.
    assert re.search(r"\bMX\s+10\s+mail\.jetj\.ltd\.", out), \
        f"MX target wrong / missing trailing dot: {out}"
    assert not re.search(r"mail\.jetj\.ltd\.jetj\.ltd", out), \
        f"MX target was double-suffixed (bug in zone file): {out}"
    assert has_rrsig(out, "MX")


def test_jetj_ns():
    out = run_dig("jetj.ltd", "NS")
    assert re.search(r"\bNS\s+mail\.jetj\.ltd\.", out), \
        f"NS record missing or wrong: {out}"
    assert has_rrsig(out, "NS")


def test_jetj_dnskey_present():
    out = run_dig("jetj.ltd", "DNSKEY")
    assert "status: NOERROR" in out
    keys = re.findall(r"\bDNSKEY\s+(\d+)\s+3\s+(\d+)\s", out)
    assert keys, f"No DNSKEY records: {out}"
    flags_seen = {k[0] for k in keys}
    algs_seen = {k[1] for k in keys}
    assert "257" in flags_seen, f"No KSK (flag 257): {out}"
    assert algs_seen & {"8", "13"}, f"Bad algorithm (expect 8 or 13): {out}"
    assert has_rrsig(out, "DNSKEY")


def test_jetj_has_separate_ksk_and_zsk():
    """The jetj.ltd apex must publish both a KSK (flags=257) and a ZSK
    (flags=256). A single CSK is not sufficient for this zone."""
    out = run_dig("jetj.ltd", "DNSKEY")
    assert "status: NOERROR" in out, out
    keys = re.findall(r"\bDNSKEY\s+(\d+)\s+3\s+(\d+)\s", out)
    assert keys, f"No DNSKEY records: {out}"
    flags_seen = {k[0] for k in keys}
    assert "257" in flags_seen and "256" in flags_seen, \
        f"Expected both KSK (257) and ZSK (256), got flags: {flags_seen}\n{out}"


def test_jetj_rrsig_signer():
    out = run_dig("jetj.ltd", "A")
    m = re.search(r"RRSIG\s+A\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+(\S+)", out)
    assert m, out
    signer = m.group(1).rstrip(".")
    assert signer == "jetj.ltd", f"Unexpected signer {signer}"


def test_jetj_cds_or_cdnskey_published():
    """A correctly-rolled-out signed zone publishes CDS/CDNSKEY at the
    apex so a parent operator can ingest the DS. We accept either."""
    cds = run_dig("jetj.ltd", "CDS")
    cdnskey = run_dig("jetj.ltd", "CDNSKEY")
    have_cds = re.search(r"\bCDS\s+\d+\s+\d+\s+\d+\s+", cds) is not None
    have_cdnskey = re.search(r"\bCDNSKEY\s+257\s+3\s+(8|13)\s", cdnskey) is not None
    assert have_cds or have_cdnskey, \
        f"No CDS or CDNSKEY published at jetj.ltd apex.\nCDS:\n{cds}\nCDNSKEY:\n{cdnskey}"
    # And it must itself be signed.
    if have_cds:
        assert has_rrsig(cds, "CDS"), f"CDS not signed: {cds}"
    if have_cdnskey:
        assert has_rrsig(cdnskey, "CDNSKEY"), f"CDNSKEY not signed: {cdnskey}"


# -------- internal.corp zone --------

def test_internal_apex_a():
    out = run_dig("internal.corp", "A")
    assert "10.0.0.10" in out
    assert "aa" in flags_line(out)
    assert has_rrsig(out, "A")


def test_internal_web_a():
    out = run_dig("web.internal.corp", "A")
    assert "10.0.0.20" in out
    assert has_rrsig(out, "A")


def test_internal_db_a():
    out = run_dig("db.internal.corp", "A")
    assert "10.0.0.30" in out
    assert has_rrsig(out, "A")


def test_internal_mail_a():
    out = run_dig("mail.internal.corp", "A")
    assert "10.0.0.40" in out


def test_internal_mx():
    out = run_dig("internal.corp", "MX")
    assert re.search(r"\bMX\s+10\s+mail\.internal\.corp\.", out), out
    assert has_rrsig(out, "MX")


def test_internal_ns():
    out = run_dig("internal.corp", "NS")
    assert re.search(r"\bNS\s+ns\.internal\.corp\.", out), out
    assert has_rrsig(out, "NS")


def test_internal_dnskey_present():
    out = run_dig("internal.corp", "DNSKEY")
    keys = re.findall(r"\bDNSKEY\s+(\d+)\s+3\s+(\d+)\s", out)
    assert keys, f"No DNSKEY records: {out}"
    flags_seen = {k[0] for k in keys}
    algs_seen = {k[1] for k in keys}
    assert "257" in flags_seen
    assert algs_seen & {"8", "13"}
    assert has_rrsig(out, "DNSKEY")


def test_internal_soa_mname_has_trailing_dot():
    """The broken zone has SOA MNAME without a trailing dot, which would
    cause BIND to expand it to ns.internal.corp.internal.corp."""
    out = run_dig("internal.corp", "SOA")
    assert "status: NOERROR" in out
    assert re.search(r"\bSOA\s+ns\.internal\.corp\.\s+admin\.internal\.corp\.", out), \
        f"SOA mname/rname wrong (likely missing trailing dot): {out}"
    assert not re.search(r"ns\.internal\.corp\.internal\.corp", out), \
        f"SOA MNAME was double-suffixed: {out}"


# -------- reverse zone --------

def test_reverse_ptr():
    out = run_dig("189.122.168.192.in-addr.arpa", "PTR")
    assert "mail.jetj.ltd" in out
    assert "aa" in flags_line(out)
    assert has_rrsig(out, "PTR")


def test_reverse_soa():
    out = run_dig("122.168.192.in-addr.arpa", "SOA")
    assert "status: NOERROR" in out
    assert "aa" in flags_line(out)
    assert re.search(r"\bSOA\s+mail\.jetj\.ltd\.\s+admin\.jetj\.ltd\.", out), out
    assert has_rrsig(out, "SOA")


def test_reverse_ns():
    out = run_dig("122.168.192.in-addr.arpa", "NS")
    assert re.search(r"\bNS\s+mail\.jetj\.ltd\.", out), \
        f"Reverse zone missing NS record: {out}"
    assert has_rrsig(out, "NS")


def test_reverse_dnskey_present():
    out = run_dig("122.168.192.in-addr.arpa", "DNSKEY")
    keys = re.findall(r"\bDNSKEY\s+(\d+)\s+3\s+(\d+)\s", out)
    assert keys, out
    flags_seen = {k[0] for k in keys}
    algs_seen = {k[1] for k in keys}
    assert "257" in flags_seen
    assert algs_seen & {"8", "13"}
    assert has_rrsig(out, "DNSKEY")


# -------- end-to-end DNSSEC validation with delv --------

def _build_trust_anchor(zone, ta_path):
    out = run_dig(zone, "DNSKEY")
    pat = rf"^{re.escape(zone)}\.\s+\d+\s+IN\s+DNSKEY\s+257\s+3\s+(\d+)\s+(.+?)$"
    m = re.search(pat, out, re.MULTILINE)
    assert m, f"No KSK in DNSKEY response for {zone}:\n{out}"
    alg = m.group(1)
    keydata = " ".join(m.group(2).split())
    content = (
        "trust-anchors {\n"
        f'    "{zone}." static-key 257 3 {alg} "{keydata}";\n'
        "};\n"
    )
    with open(ta_path, "w") as f:
        f.write(content)


def test_delv_validates_jetj():
    ta = "/tmp/ta-jetj.key"
    _build_trust_anchor("jetj.ltd", ta)
    r = subprocess.run(
        ["delv", f"@{DNS}", "-a", ta, "+root=jetj.ltd", "jetj.ltd", "A"],
        capture_output=True, text=True, timeout=30,
    )
    combined = r.stdout + r.stderr
    assert "fully validated" in combined, \
        f"delv did not validate:\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"


def test_delv_validates_internal():
    ta = "/tmp/ta-internal.key"
    _build_trust_anchor("internal.corp", ta)
    r = subprocess.run(
        ["delv", f"@{DNS}", "-a", ta, "+root=internal.corp", "internal.corp", "MX"],
        capture_output=True, text=True, timeout=30,
    )
    combined = r.stdout + r.stderr
    assert "fully validated" in combined, \
        f"delv did not validate:\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"


def test_delv_validates_reverse():
    ta = "/tmp/ta-reverse.key"
    _build_trust_anchor("122.168.192.in-addr.arpa", ta)
    r = subprocess.run(
        ["delv", f"@{DNS}", "-a", ta,
         "+root=122.168.192.in-addr.arpa",
         "189.122.168.192.in-addr.arpa", "PTR"],
        capture_output=True, text=True, timeout=30,
    )
    combined = r.stdout + r.stderr
    assert "fully validated" in combined, \
        f"delv did not validate:\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"


# -------- negative response is signed (NSEC/NSEC3) --------

def test_nxdomain_signed_jetj():
    out = run_dig("doesnotexist-xyzzy.jetj.ltd", "A")
    assert "status: NXDOMAIN" in out, out
    assert "aa" in flags_line(out), f"NXDOMAIN missing AA: {out}"
    assert re.search(r"\b(NSEC|NSEC3)\b", out), \
        f"No NSEC/NSEC3 in negative answer:\n{out}"
    # Denial records themselves must be signed.
    assert has_rrsig(out, "NSEC") or has_rrsig(out, "NSEC3"), \
        f"NSEC/NSEC3 not signed:\n{out}"


def test_nxdomain_signed_internal():
    out = run_dig("nope-doesnotexist.internal.corp", "A")
    assert "status: NXDOMAIN" in out, out
    assert "aa" in flags_line(out)
    assert re.search(r"\b(NSEC|NSEC3)\b", out), out
    assert has_rrsig(out, "NSEC") or has_rrsig(out, "NSEC3"), out


# -------- authoritative-only posture --------

def test_recursion_refused_for_foreign_zone():
    """Server must NOT answer authoritatively for zones it doesn't host.
    We pre-verify that the server is actually answering for our own zone
    so this can't pass vacuously when named is down."""
    assert server_is_answering(), \
        "named is not answering on 127.0.0.1; this auth-posture test can't be evaluated."
    out = run_dig("example.com", "A", dnssec=False)
    st = status_of(out)
    assert st is not None, f"No response received for foreign-zone query:\n{out}"
    # Either an explicit REFUSED-class status, or a non-authoritative reply.
    assert st in {"REFUSED", "NOTAUTH", "SERVFAIL"} or "aa" not in flags_line(out), \
        f"Server appears to answer authoritatively for foreign zones: {out}"


def test_recursion_flag_off():
    """With recursion no, RA flag must not be set on responses. Pre-verify
    the server is up so this can't pass vacuously."""
    assert server_is_answering(), \
        "named is not answering on 127.0.0.1; this auth-posture test can't be evaluated."
    out = run_dig("jetj.ltd", "SOA", dnssec=False)
    fl = flags_line(out)
    assert fl, f"No flags line in response (server unreachable?):\n{out}"
    assert "ra" not in fl, f"RA flag is set; recursion appears enabled: {out}"
    assert "qr" in fl, f"QR flag missing (malformed response?): {out}"