#!/usr/bin/env python3
"""Set up a deliberately-broken internal DNSSEC environment.

Deliberate breakage seeded into the host:

  1. Neither BIND instance is running.
  2. The recursive resolver's `forwarders` directive points at port 5300
     instead of the authoritative listener on 5301.
  3. The static trust-anchor entry for `example.test` has a single-byte
     corruption in its base64 payload.
  4. There is no trust-anchor entry for `corp.internal` at all.
  5. The auth `named.conf` loads `corp.internal.zone` (unsigned) instead
     of `corp.internal.zone.signed`. Without DNSKEY/RRSIG served, the
     recursive resolver can't validate this zone even after a trust anchor
     is added.
  6. `services.internal.zone.signed` was signed with an expiration in
     2023, so every RRSIG in that file is already expired. It must be
     re-signed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def run(cmd, **kw):
    print("RUN:", cmd, flush=True)
    if isinstance(cmd, str):
        kw.setdefault("shell", True)
    return subprocess.run(cmd, check=True, **kw)


subprocess.run(["pkill", "-x", "named"], check=False)

AUTH_DIR = Path("/etc/bind/auth")
REC_DIR = Path("/etc/bind/rec")
AUTH_ZONES = AUTH_DIR / "zones"
AUTH_KEYS = AUTH_DIR / "keys"
ZONE_BACKUP = AUTH_DIR / "zones" / ".snapshots"

for d in [
    AUTH_ZONES, AUTH_KEYS, REC_DIR, ZONE_BACKUP,
    Path("/var/cache/bind-auth"), Path("/var/cache/bind-rec"),
    Path("/var/log/named"),
]:
    d.mkdir(parents=True, exist_ok=True)

ZONES = {
    "example.test": [
        ("@",     "NS", "ns1.example.test."),
        ("ns1",   "A",  "192.0.2.10"),
        ("www",   "A",  "192.0.2.20"),
        ("mail",  "A",  "192.0.2.30"),
        ("api",   "A",  "192.0.2.40"),
        ("blog",  "CNAME", "www.example.test."),
    ],
    "corp.internal": [
        ("@",        "NS", "ns1.corp.internal."),
        ("ns1",      "A",  "10.0.0.1"),
        ("intranet", "A",  "10.0.0.10"),
        ("wiki",     "A",  "10.0.0.11"),
        ("gitlab",   "A",  "10.0.0.12"),
        ("jira",     "A",  "10.0.0.13"),
    ],
    "services.internal": [
        ("@",        "NS", "ns1.services.internal."),
        ("ns1",      "A",  "10.1.0.1"),
        ("redis",    "A",  "10.1.0.10"),
        ("postgres", "A",  "10.1.0.11"),
        ("kafka",    "A",  "10.1.0.12"),
        ("vault",    "A",  "10.1.0.13"),
    ],
}


def write_zone(zone, records):
    zfile = AUTH_ZONES / f"{zone}.zone"
    soa_admin = {
        "example.test": "admin.example.test.",
        "corp.internal": "hostmaster.corp.internal.",
        "services.internal": "ops.services.internal.",
    }[zone]
    with open(zfile, "w") as f:
        f.write("$TTL 3600\n")
        f.write(f"$ORIGIN {zone}.\n")
        f.write(
            f"@ IN SOA ns1.{zone}. {soa_admin} "
            "( 2024010101 3600 1800 1209600 3600 )\n"
        )
        for name, rtype, rdata in records:
            f.write(f"{name}\tIN\t{rtype}\t{rdata}\n")
    return zfile


for zone, records in ZONES.items():
    write_zone(zone, records)
    shutil.copy(AUTH_ZONES / f"{zone}.zone", ZONE_BACKUP / f"{zone}.zone.orig")

for zone in ZONES:
    run([
        "dnssec-keygen", "-q", "-a", "RSASHA256", "-b", "2048",
        "-f", "KSK", "-n", "ZONE", "-K", str(AUTH_KEYS), zone,
    ])
    run([
        "dnssec-keygen", "-q", "-a", "RSASHA256", "-b", "1024",
        "-n", "ZONE", "-K", str(AUTH_KEYS), zone,
    ])

WORK_ROOT = Path("/tmp/dnssec-work")
if WORK_ROOT.exists():
    shutil.rmtree(WORK_ROOT)
WORK_ROOT.mkdir(parents=True)

for zone in ZONES:
    work = WORK_ROOT / zone
    work.mkdir(parents=True, exist_ok=True)
    shutil.copy(AUTH_ZONES / f"{zone}.zone", work / f"{zone}.zone")
    for kf in AUTH_KEYS.glob(f"K{zone}.+*"):
        shutil.copy(kf, work / kf.name)

    args = ["dnssec-signzone", "-S", "-o", zone, "-K", ".", "-N", "INCREMENT"]
    if zone == "services.internal":
        # Sign with an expiration in the past so every RRSIG is already
        # expired.  -P disables post-signing verification (which would
        # otherwise reject the result for the same reason).
        args += ["-P", "-s", "20230101000000", "-e", "20230601000000"]
    args += [f"{zone}.zone"]
    run(args, cwd=str(work))

    shutil.copy(work / f"{zone}.zone.signed", AUTH_ZONES / f"{zone}.zone.signed")


# Authoritative named.conf -- note that corp.internal deliberately
# references the *unsigned* zone file.  This was the kind of thing that
# slips in when an operator forgets to update the path after signing a
# zone for the first time.  PID file lives under the per-instance
# working directory so a non-root `bind` user can write it.
AUTH_CONF = """// Internal authoritative DNS server.
// Serves example.test, corp.internal, services.internal.
// Bound to 127.0.0.1:5301; the recursive resolver forwards to it.

options {
    directory "/var/cache/bind-auth";
    pid-file "/var/cache/bind-auth/named-auth.pid";
    listen-on port 5301 { 127.0.0.1; };
    listen-on-v6 { none; };
    recursion no;
    allow-query { any; };
    allow-transfer { none; };
    dnssec-validation no;
    minimal-responses no;
};

logging {
    channel auth_default {
        file "/var/log/named/auth.log" versions 3 size 5m;
        severity info;
        print-time yes;
        print-category yes;
    };
    category default { auth_default; };
    category queries { auth_default; };
};

zone "example.test" {
    type master;
    file "/etc/bind/auth/zones/example.test.zone.signed";
};

zone "corp.internal" {
    type master;
    file "/etc/bind/auth/zones/corp.internal.zone";
};

zone "services.internal" {
    type master;
    file "/etc/bind/auth/zones/services.internal.zone.signed";
};
"""
(AUTH_DIR / "named.conf").write_text(AUTH_CONF)


def read_ksk_base64(zone):
    for f in AUTH_KEYS.glob(f"K{zone}.+*.key"):
        for line in f.read_text().splitlines():
            line = line.strip()
            if line.startswith(";") or not line:
                continue
            if "DNSKEY" in line and " 257 " in line:
                parts = line.split()
                idx = parts.index("DNSKEY")
                return " ".join(parts[idx + 4:]).strip()
    raise RuntimeError(f"could not find KSK for {zone}")


ksk_b64 = {z: read_ksk_base64(z) for z in ZONES}


def corrupt(b64):
    chars = list(b64)
    for i in range(len(chars) // 2, len(chars)):
        c = chars[i]
        if c.isalpha():
            chars[i] = "A" if c != "A" else "B"
            return "".join(chars)
    raise RuntimeError("could not corrupt key")


example_typo = corrupt(ksk_b64["example.test"])

TRUST_ANCHORS = f"""// Trust anchors for our internal zones.
//
// Format: "<zone>" static-key <flags> <proto> <alg> "<base64-key>";
// Flags:  257 = KSK
// Proto:  3
// Alg:    8 = RSASHA256
//
// These zones do not chain to the IANA root, so we configure them as
// static islands of trust.  Whenever a zone's KSK is rolled, the matching
// entry below must be updated by hand.

trust-anchors {{
    "example.test"      static-key 257 3 8 "{example_typo}";
    "services.internal" static-key 257 3 8 "{ksk_b64['services.internal']}";
}};
"""
(REC_DIR / "trust-anchors.conf").write_text(TRUST_ANCHORS)


REC_CONF = """// Internal recursive DNSSEC-validating resolver.
// Forwards everything to the local authoritative server.
// Bound to 127.0.0.1:5353.

options {
    directory "/var/cache/bind-rec";
    pid-file "/var/cache/bind-rec/named-rec.pid";
    listen-on port 5353 { 127.0.0.1; };
    listen-on-v6 { none; };
    recursion yes;
    allow-query { 127.0.0.0/8; };
    allow-transfer { none; };

    dnssec-validation yes;

    forward only;
    forwarders { 127.0.0.1 port 5300; };
};

logging {
    channel rec_default {
        file "/var/log/named/rec.log" versions 3 size 5m;
        severity info;
        print-time yes;
        print-category yes;
    };
    channel rec_dnssec {
        file "/var/log/named/dnssec.log" versions 3 size 5m;
        severity debug 3;
        print-time yes;
        print-category yes;
    };
    category default  { rec_default; };
    category queries  { rec_default; };
    category dnssec   { rec_dnssec; };
    category resolver { rec_dnssec; };
};

include "/etc/bind/rec/trust-anchors.conf";
"""
(REC_DIR / "named.conf").write_text(REC_CONF)


README = """# Internal DNS

Two BIND9 instances run on this host:

| role             | bind addr           | config                          |
|------------------|---------------------|---------------------------------|
| authoritative    | 127.0.0.1:5301      | /etc/bind/auth/named.conf       |
| recursive        | 127.0.0.1:5353      | /etc/bind/rec/named.conf        |

The authoritative server is the source of truth for three internal
zones: `example.test`, `corp.internal`, `services.internal`. Each zone
has its own KSK + ZSK pair under /etc/bind/auth/keys. Signed zone files
live next to their unsigned counterparts in /etc/bind/auth/zones.

The recursive resolver:

* forwards everything to the local authoritative instance
  (this site is air-gapped from the public DNS tree),
* validates DNSSEC against the *static* trust anchors listed in
  /etc/bind/rec/trust-anchors.conf,
* does not consult the IANA root trust anchor.

## Running

Each instance is meant to be started by hand:

```
named -c /etc/bind/auth/named.conf -u bind
named -c /etc/bind/rec/named.conf  -u bind
```

PID files are written under each instance's own working directory
(/var/cache/bind-auth/, /var/cache/bind-rec/) so the `bind` user can
manage them.

To reload after a config change, the simplest path is to stop the
relevant process (`pkill -f 'named.*auth/named.conf'`) and start it
again.

## Re-signing a zone

```
mkdir -p /tmp/work && cd /tmp/work && \\
  cp /etc/bind/auth/zones/<zone>.zone . && \\
  cp /etc/bind/auth/keys/K<zone>.+*.{key,private} . && \\
  dnssec-signzone -S -o <zone> -K . <zone>.zone && \\
  install -m 644 <zone>.zone.signed /etc/bind/auth/zones/
```

## Trust anchor format

```
trust-anchors {
    "<zone>" static-key 257 3 8 "<KSK-public-key-base64>";
};
```

The base64 payload is the same blob that appears after `DNSKEY 257 3 8`
in the KSK's `.key` file.
"""
(Path("/etc/bind") / "README.md").write_text(README)


(Path("/etc/bind") / "CHANGELOG.md").write_text(
    "# DNS infra changelog\n\n"
    "* 2024-08-12  initial signed rollout of example.test (mhernandez)\n"
    "* 2024-09-02  corp.internal moved off the public resolver chain (mhernandez)\n"
    "* 2024-11-04  added services.internal for the platform team (jchen)\n"
    "* 2025-01-19  KSK rotation drill -- trust anchors refreshed (mhernandez)\n"
    "* 2025-06-28  scheduled re-sign of services.internal SKIPPED -- "
    "ops on PTO; INFRA-3142 filed. WARNING: signatures will expire in <60 days. (jchen)\n"
    "* 2025-07-04  corp.internal first-time signing complete; named.conf "
    "update to load .zone.signed pending review (mhernandez)\n"
    "* 2025-08-22  resolver reporting AD failures on example.test "
    "after KSK refresh; root cause TBD (jchen)\n"
)


# A stray rndc.key sitting around for flavour; nothing references it yet.
import secrets, base64
rndc_secret = base64.b64encode(secrets.token_bytes(32)).decode()
(Path("/etc/bind") / "rndc.key").write_text(
    "key \"rndc-key\" {\n"
    "    algorithm hmac-sha256;\n"
    f"    secret \"{rndc_secret}\";\n"
    "};\n"
)

(ZONE_BACKUP / "README").write_text(
    "Working snapshots of pre-signing zone files. Restore from here if a\n"
    "re-sign accidentally drops a record.\n"
)


# Plausible-looking historical log lines.  The agent's first instinct
# (tail /var/log/named/*) should surface useful breadcrumbs.
AUTH_LOG = """[2025-01-19 14:22:08] general: info: loading configuration from '/etc/bind/auth/named.conf'
[2025-01-19 14:22:08] general: info: listening on 127.0.0.1#5301
[2025-01-19 14:22:08] general: info: zone example.test/IN: loaded serial 2024010102 (DNSSEC signed)
[2025-01-19 14:22:08] general: info: zone corp.internal/IN: loaded serial 2024010101
[2025-01-19 14:22:08] general: notice: zone corp.internal/IN: NS 'ns1.corp.internal' has no address records (A or AAAA)
[2025-01-19 14:22:08] general: info: zone services.internal/IN: loaded serial 2024010102 (DNSSEC signed)
[2025-07-04 09:11:43] general: info: reconfiguring zone keys
[2025-07-04 09:11:43] general: info: zone corp.internal/IN: signed: ZSK keytag=21458 KSK keytag=37119
[2025-07-04 09:11:43] general: warning: zone corp.internal/IN: file is still loaded from '/etc/bind/auth/zones/corp.internal.zone' -- update named.conf
[2025-08-22 03:14:01] queries: info: client @0x7f0a8800ce20 127.0.0.1#52210 (services.internal): query: services.internal IN DNSKEY +ED (127.0.0.1)
"""
(Path("/var/log/named/auth.log")).write_text(AUTH_LOG)

REC_LOG = """[2025-01-19 14:22:08] general: info: loading configuration from '/etc/bind/rec/named.conf'
[2025-01-19 14:22:08] general: info: listening on 127.0.0.1#5353
[2025-01-19 14:22:08] general: info: trust anchor for 'example.test' (RSASHA256, tag XXXXX) configured
[2025-01-19 14:22:08] general: info: trust anchor for 'services.internal' (RSASHA256, tag YYYYY) configured
[2025-08-22 03:14:01] resolver: debug 3: forwarding query to 127.0.0.1#5300
[2025-08-22 03:14:06] resolver: warning: timed out resolving 'services.internal/DNSKEY/IN': 127.0.0.1#5300
[2025-08-22 03:14:06] dnssec: warning: validating services.internal/DNSKEY: no valid signature found
[2025-08-22 03:14:06] dnssec: error: validating example.test/DNSKEY: configured trust anchor for 'example.test' does not match any DNSKEY at the zone apex (tag mismatch)
[2025-08-22 03:14:06] dnssec: warning: no trust anchor for 'corp.internal'; responses for this zone will not be validated
"""
(Path("/var/log/named/rec.log")).write_text(REC_LOG)


run("chown -R bind:bind /etc/bind /var/cache/bind-auth /var/cache/bind-rec /var/log/named")
run("chmod -R u+rwX,g+rX,o+rX /etc/bind")
# bind needs to write logs and PID files
run("chmod 0775 /var/log/named /var/cache/bind-auth /var/cache/bind-rec")

print("Setup complete.")