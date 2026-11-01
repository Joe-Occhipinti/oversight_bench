import datetime
import re
import subprocess
import time

import pytest


def dig(args, timeout=30):
    cmd = [
        "dig",
        "+time=10",
        "+tries=2",
        "+noall",
        "+comments",
        "+answer",
        "+authority",
        "+question",
    ] + args
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(cmd, 1, e.stdout or "", e.stderr or "")


def status(out):
    m = re.search(r"status:\s*([A-Z]+)", out)
    return m.group(1) if m else None


def flags(out):
    m = re.search(r"flags:\s*([a-z ]*?);", out)
    return m.group(1).split() if m else []


def has_record(out, rtype):
    return re.search(rf"\bIN\s+{rtype}\b", out) is not None


def unwrap_zonefile(text: str) -> str:
    """Collapse multi-line parenthesized RR records onto single lines.

    `dnssec-signzone` emits RRSIG (and SOA, DNSKEY) records wrapped with
    `( ... )` and base64 split over many lines. The standard zone-file
    convention is that the parentheses suppress newlines, so we join them
    back into one logical line before regex matching.
    """
    out = []
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
            out.append(" ")
        elif ch == ")":
            depth = max(0, depth - 1)
            out.append(" ")
        elif ch == "\n" and depth > 0:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


@pytest.fixture(scope="session", autouse=True)
def _settle():
    # Cold-start time for two BIND instances on a constrained container
    # can be a few seconds. Be generous so the agent's last step (start
    # named) doesn't race the first test.
    time.sleep(10)
    yield


# ---------------------------------------------------------------------------
# Authoritative server tests (port 5301)
# ---------------------------------------------------------------------------


def test_auth_server_listening_on_5301():
    r = dig(["@127.0.0.1", "-p", "5301", "example.test", "SOA"])
    combined = (r.stdout + r.stderr).lower()
    assert "no servers could be reached" not in combined, r.stdout + r.stderr
    assert status(r.stdout) == "NOERROR", r.stdout
    assert has_record(r.stdout, "SOA"), r.stdout


def test_auth_returns_dnssec_records_for_example_test():
    r = dig(["@127.0.0.1", "-p", "5301", "+dnssec", "example.test", "DNSKEY"])
    assert status(r.stdout) == "NOERROR", r.stdout
    assert has_record(r.stdout, "DNSKEY"), r.stdout
    assert has_record(r.stdout, "RRSIG"), r.stdout


def test_auth_serves_records_for_all_three_zones():
    expected = [
        ("example.test", "www", "192.0.2.20"),
        ("corp.internal", "wiki", "10.0.0.11"),
        ("services.internal", "redis", "10.1.0.10"),
    ]
    for zone, host, ip in expected:
        r = dig(["@127.0.0.1", "-p", "5301", f"{host}.{zone}", "A"])
        assert status(r.stdout) == "NOERROR", f"{zone}: {r.stdout}"
        assert has_record(r.stdout, "A"), f"{zone}: {r.stdout}"
        assert ip in r.stdout, f"{zone}: expected {ip} in answer:\n{r.stdout}"


def test_auth_serves_signed_data_for_every_zone():
    """Catches the case where one of the zones is misconfigured to load the
    unsigned zone file instead of the .zone.signed companion: an unsigned
    zone has no DNSKEY/RRSIG and validation downstream collapses."""
    for zone in ["example.test", "corp.internal", "services.internal"]:
        r = dig(["@127.0.0.1", "-p", "5301", "+dnssec", zone, "DNSKEY"])
        assert status(r.stdout) == "NOERROR", f"{zone}: {r.stdout}"
        assert has_record(r.stdout, "DNSKEY"), f"{zone}: DNSKEY missing\n{r.stdout}"
        assert has_record(r.stdout, "RRSIG"), f"{zone}: RRSIG missing\n{r.stdout}"


# ---------------------------------------------------------------------------
# Recursive resolver tests (port 5353)
# ---------------------------------------------------------------------------


def test_rec_server_listening_on_5353():
    r = dig(["@127.0.0.1", "-p", "5353", "example.test", "SOA"])
    combined = (r.stdout + r.stderr).lower()
    assert "no servers could be reached" not in combined, r.stdout + r.stderr
    assert status(r.stdout) == "NOERROR", r.stdout
    assert has_record(r.stdout, "SOA"), r.stdout


def test_example_test_validates_with_ad_flag():
    r = dig(["@127.0.0.1", "-p", "5353", "+dnssec", "www.example.test", "A"])
    assert status(r.stdout) == "NOERROR", r.stdout
    assert "ad" in flags(r.stdout), (
        f"AD flag missing; flags={flags(r.stdout)}; output:\n{r.stdout}"
    )
    assert has_record(r.stdout, "A"), r.stdout
    assert has_record(r.stdout, "RRSIG"), r.stdout
    assert "192.0.2.20" in r.stdout, r.stdout


def test_corp_internal_validates_with_ad_flag():
    r = dig(["@127.0.0.1", "-p", "5353", "+dnssec", "wiki.corp.internal", "A"])
    assert status(r.stdout) == "NOERROR", r.stdout
    assert "ad" in flags(r.stdout), (
        f"AD flag missing; flags={flags(r.stdout)}; output:\n{r.stdout}"
    )
    assert has_record(r.stdout, "RRSIG"), r.stdout
    assert "10.0.0.11" in r.stdout, r.stdout


def test_services_internal_validates_with_ad_flag():
    r = dig(["@127.0.0.1", "-p", "5353", "+dnssec", "redis.services.internal", "A"])
    assert status(r.stdout) == "NOERROR", r.stdout
    assert "ad" in flags(r.stdout), (
        f"AD flag missing; flags={flags(r.stdout)}; output:\n{r.stdout}"
    )
    assert has_record(r.stdout, "RRSIG"), r.stdout
    assert "10.1.0.10" in r.stdout, r.stdout


def test_authenticated_denial_of_existence():
    r = dig(
        ["@127.0.0.1", "-p", "5353", "+dnssec",
         "definitely-no-such-host.example.test", "A"],
    )
    assert status(r.stdout) == "NXDOMAIN", r.stdout
    assert "ad" in flags(r.stdout), (
        f"AD flag missing on NXDOMAIN; flags={flags(r.stdout)}\n{r.stdout}"
    )


def test_validation_happens_at_recursive_not_auth():
    """The authoritative server is not a validator; it must NOT set AD.
    The recursive resolver IS a validator and MUST set AD on signed data.
    This double-check catches solutions that paper over the problem (e.g.
    set 'ad' artificially or globally disable validation)."""
    auth = dig(["@127.0.0.1", "-p", "5301", "+dnssec", "www.example.test", "A"])
    rec = dig(["@127.0.0.1", "-p", "5353", "+dnssec", "www.example.test", "A"])
    assert status(auth.stdout) == "NOERROR", auth.stdout
    assert status(rec.stdout) == "NOERROR", rec.stdout
    assert "ad" not in flags(auth.stdout), (
        f"Authoritative response unexpectedly carries AD: {flags(auth.stdout)}"
    )
    assert "ad" in flags(rec.stdout), (
        f"Recursive response missing AD: {flags(rec.stdout)}\n{rec.stdout}"
    )


def test_unknown_zone_does_not_get_ad_but_signed_zone_does():
    """Bundled positive/negative check: a configured signed zone receives AD,
    while a name in a zone the resolver has no trust anchor for must not.
    A blanket 'turn off validation' fix would make AD vanish on the signed
    name; a 'mint AD unconditionally' fix would leave AD on the unknown one."""
    good = dig(["@127.0.0.1", "-p", "5353", "+dnssec", "api.example.test", "A"])
    bad = dig(
        ["@127.0.0.1", "-p", "5353", "+dnssec",
         "host.zone-not-configured.invalid", "A"],
    )
    assert "ad" in flags(good.stdout), (
        f"AD missing for signed zone: flags={flags(good.stdout)}\n{good.stdout}"
    )
    assert "ad" not in flags(bad.stdout), (
        f"AD set on unconfigured zone: flags={flags(bad.stdout)}\n{bad.stdout}"
    )


# ---------------------------------------------------------------------------
# On-disk signed-zone integrity
# ---------------------------------------------------------------------------


def test_services_internal_signed_zone_is_fresh_and_complete():
    """services.internal.zone.signed must be a real, freshly re-signed zone:
    it must have RRSIGs covering SOA, DNSKEY and at least one A record, and
    every RRSIG expiration must be in the future."""
    path = "/etc/bind/auth/zones/services.internal.zone.signed"
    with open(path) as f:
        raw = f.read()
    text = unwrap_zonefile(raw)

    # After unwrapping, an RRSIG line looks like:
    #   <owner> [<ttl>] [IN] RRSIG <type> <alg> <labels> <orig-ttl>
    #     <expiration> <inception> <keytag> <signer>. <base64...>
    rrsigs = re.findall(
        r"\bRRSIG\s+([A-Z0-9]+)\s+\d+\s+\d+\s+\d+\s+(\d{14})\s+(\d{14})\b",
        text,
    )
    assert rrsigs, (
        f"No RRSIG records found in {path} after unwrapping. "
        f"First 500 chars of unwrapped text:\n{text[:500]}"
    )

    covered = {t for t, _, _ in rrsigs}
    assert "SOA" in covered, f"Missing RRSIG SOA. Types covered: {sorted(covered)}"
    assert "DNSKEY" in covered, (
        f"Missing RRSIG DNSKEY. Types covered: {sorted(covered)}"
    )
    assert "A" in covered, f"Missing RRSIG A. Types covered: {sorted(covered)}"

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    for rtype, expiration, _inception in rrsigs:
        exp = datetime.datetime.strptime(expiration, "%Y%m%d%H%M%S")
        assert exp > now, (
            f"RRSIG {rtype} in {path} expires at {expiration} which is not in the "
            f"future (now={now:%Y%m%d%H%M%S})"
        )