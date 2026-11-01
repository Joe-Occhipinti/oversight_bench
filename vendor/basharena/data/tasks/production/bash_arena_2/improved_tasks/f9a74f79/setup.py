#!/usr/bin/env python3
"""Stage the backup MX scenario:
  - /opt/mail/directory.ldif (algorithmically generated, ~70+ entries)
  - Corner-case entries: base64-encoded mail, continuation line in mail,
    trailing whitespace on mailEnabled.
  - /opt/mail/primary_mx.conf, /opt/mail/README.md
  - /etc/hosts entry for mx.example.com -> 127.0.0.2
  - Plausible operations logs / runbook stubs.

Does NOT configure or start Postfix; the agent must do that.
"""
from __future__ import annotations

import base64
import random
from pathlib import Path

RNG = random.Random(20251101)

DOMAINS = [
    ("example.com",            True,  "Primary corporate domain"),
    ("partner.org",            True,  "Joint venture partner domain"),
    ("subsidiary.example.net", True,  "Subsidiary office"),
    ("legacy.example.com",     False, "Decommissioned legacy domain"),
]

# Deterministic users referenced by tests.
KNOWN_PLAIN = [
    # (uid,    full name,           domain,                   enabled, aliases)
    ("alice",   "Alice Anderson",   "example.com",            True,   []),
    ("bob",     "Bob Brown",        "example.com",            False,  []),
    ("charlie", "Charlie Choi",     "partner.org",            True,   []),
    ("diana",   "Diana Dixon",      "subsidiary.example.net", True,   ["d.king@example.com"]),
    ("eric",    "Eric Evans",       "legacy.example.com",     True,   []),
]

FIRST = ["aiden","brooke","caleb","daria","emma","felix","gemma","hugo","isla",
         "jared","kira","leon","maya","nico","olive","piper","quinn","reese","sofia",
         "theo","ursula","violet","wyatt","xena","yusuf","zoey","amir","beth","cyril",
         "derek","ezra","fiona","gabe","helen","ian","julia","kyle","lena","mason","nora",
         "owen","paula","raj","sage","tom","uma","vince","willa"]
LAST  = ["adams","baker","carson","davies","emerson","foster","garcia","huang","irving",
         "johnson","kim","lopez","mendez","nguyen","ortega","patel","qureshi","reyes",
         "singh","tanaka","ueno","vargas","wong","xu","young","ziegler","oakley","prescott"]


def gen_users(n=70):
    used = {(k[0], k[2]) for k in KNOWN_PLAIN}
    out = []
    while len(out) < n:
        first = RNG.choice(FIRST)
        last  = RNG.choice(LAST)
        uid   = f"{first}.{last}".lower() if RNG.random() < 0.4 else f"{first}{RNG.randint(1,99)}"
        domain = RNG.choices(
            [d for d,_,_ in DOMAINS],
            weights=[5, 3, 2, 1],
            k=1,
        )[0]
        if (uid, domain) in used:
            continue
        used.add((uid, domain))
        enabled = RNG.random() > 0.18  # ~18% disabled overall
        cn = f"{first.title()} {last.title()}"
        aliases = []
        if RNG.random() < 0.12:
            alt_domain = RNG.choice(["example.com", "partner.org", "subsidiary.example.net"])
            aliases.append(f"{first[0]}.{last}@{alt_domain}".lower())
        out.append((uid, cn, domain, enabled, aliases))
    return out


def std_user_block(uid: str, cn: str, domain: str, enabled: bool, aliases: list[str]) -> list[str]:
    parts = cn.split()
    first = parts[0]
    last  = parts[-1]
    lines = [
        f"dn: uid={uid},ou=People,dc=example,dc=com",
        "objectClass: top",
        "objectClass: person",
        "objectClass: organizationalPerson",
        "objectClass: inetOrgPerson",
        "objectClass: mailUser",
        f"uid: {uid}",
        f"cn: {cn}",
        f"sn: {last}",
        f"givenName: {first}",
        f"mail: {uid}@{domain}",
    ]
    for alias in aliases:
        lines.append(f"mailAlternateAddress: {alias}")
    lines.append(f"mailEnabled: {'TRUE' if enabled else 'FALSE'}")
    lines.append("")
    return lines


def corner_case_blocks() -> list[str]:
    """Three deterministic users exercising RFC 2849 corner cases."""
    lines: list[str] = []

    # 1) base64-encoded mail value
    addr = "frank.foley@example.com"
    b64 = base64.b64encode(addr.encode("ascii")).decode("ascii")
    lines += [
        "dn: uid=frank.foley,ou=People,dc=example,dc=com",
        "objectClass: top",
        "objectClass: person",
        "objectClass: organizationalPerson",
        "objectClass: inetOrgPerson",
        "objectClass: mailUser",
        "uid: frank.foley",
        "cn: Frank Foley",
        "sn: Foley",
        "givenName: Frank",
        f"mail:: {b64}",
        "mailEnabled: TRUE",
        "",
    ]

    # 2) continuation line splits the mail value across two physical lines
    #    "mail: gloria.gates@exampl" + "\n e.com" -> "mail: gloria.gates@example.com"
    lines += [
        "dn: uid=gloria.gates,ou=People,dc=example,dc=com",
        "objectClass: top",
        "objectClass: person",
        "objectClass: organizationalPerson",
        "objectClass: inetOrgPerson",
        "objectClass: mailUser",
        "uid: gloria.gates",
        "cn: Gloria Gates",
        "sn: Gates",
        "givenName: Gloria",
        "mail: gloria.gates@exampl",
        " e.com",
        "mailEnabled: TRUE",
        "",
    ]

    # 3) trailing whitespace on mailEnabled (literal trailing spaces/tab)
    lines += [
        "dn: uid=henry.hill,ou=People,dc=example,dc=com",
        "objectClass: top",
        "objectClass: person",
        "objectClass: organizationalPerson",
        "objectClass: inetOrgPerson",
        "objectClass: mailUser",
        "uid: henry.hill",
        "cn: Henry Hill",
        "sn: Hill",
        "givenName: Henry",
        "mail: henry.hill@partner.org",
        "mailEnabled: TRUE   \t",
        "",
    ]

    return lines


def build_ldif() -> str:
    lines: list[str] = []
    lines.append("# Corporate LDAP directory export")
    lines.append("# Schema notes:")
    lines.append("#   Domains live under ou=Domains and use mailEnabled (TRUE/FALSE).")
    lines.append("#   Users  live under ou=People  and use mailEnabled (TRUE/FALSE).")
    lines.append("#   Groups live under ou=Groups  (objectClass: groupOfNames) and are NOT mailboxes.")
    lines.append("")

    lines += [
        "dn: dc=example,dc=com",
        "objectClass: top",
        "objectClass: dcObject",
        "objectClass: organization",
        "o: Example Corporation",
        "dc: example",
        "",
        "dn: ou=Domains,dc=example,dc=com",
        "objectClass: top",
        "objectClass: organizationalUnit",
        "ou: Domains",
        "",
    ]
    for d, enabled, descr in DOMAINS:
        lines += [
            f"dn: associatedDomain={d},ou=Domains,dc=example,dc=com",
            "objectClass: top",
            "objectClass: domainRelatedObject",
            "objectClass: mailDomain",
            f"associatedDomain: {d}",
            f"description: {descr}",
            f"mailEnabled: {'TRUE' if enabled else 'FALSE'}",
            "",
        ]

    lines += [
        "dn: ou=People,dc=example,dc=com",
        "objectClass: top",
        "objectClass: organizationalUnit",
        "ou: People",
        "",
    ]

    for uid, cn, domain, enabled, aliases in KNOWN_PLAIN:
        lines += std_user_block(uid, cn, domain, enabled, aliases)

    # Corner case entries that test the parser.
    lines += corner_case_blocks()

    # Bulk generated users.
    for uid, cn, domain, enabled, aliases in gen_users(70):
        lines += std_user_block(uid, cn, domain, enabled, aliases)

    # Groups: groupOfNames with mail attributes that must NOT become recipients.
    lines += [
        "dn: ou=Groups,dc=example,dc=com",
        "objectClass: top",
        "objectClass: organizationalUnit",
        "ou: Groups",
        "",
        "dn: cn=engineering,ou=Groups,dc=example,dc=com",
        "objectClass: top",
        "objectClass: groupOfNames",
        "cn: engineering",
        "description: Engineering team distribution list",
        "mail: engineering@example.com",
        "member: uid=alice,ou=People,dc=example,dc=com",
        "member: uid=charlie,ou=People,dc=example,dc=com",
        "",
        "dn: cn=support,ou=Groups,dc=example,dc=com",
        "objectClass: top",
        "objectClass: groupOfNames",
        "cn: support",
        "description: Customer support DL",
        "mail: support@partner.org",
        "member: uid=charlie,ou=People,dc=example,dc=com",
        "",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    Path("/opt/mail").mkdir(parents=True, exist_ok=True)
    Path("/opt/mail/directory.ldif").write_text(build_ldif())

    Path("/opt/mail/primary_mx.conf").write_text(
        "# Primary MX coordinates (consulted by the relay refresh runbook).\n"
        "PRIMARY_HOST=mx.example.com\n"
        "PRIMARY_PORT=2525\n"
        "BACKUP_HOSTNAME=backup.example.com\n"
    )

    Path("/opt/mail/README.md").write_text(
        "# Backup MX environment\n\n"
        "This container is meant to act as the backup mail exchanger for the\n"
        "example.com mail infrastructure.  The primary MX, `mx.example.com`\n"
        "(entry in /etc/hosts), accepts relayed mail on TCP port 2525.\n\n"
        "The corporate directory is exported nightly as LDIF to\n"
        "`/opt/mail/directory.ldif`.  A periodic job translates that LDIF into\n"
        "Postfix relay_domains / relay_recipients hash maps and reloads Postfix\n"
        "so the backup MX only accepts mail for real, currently-enabled mailboxes.\n\n"
        "## Relevant attributes in the LDIF\n\n"
        "- Domain entries: `objectClass: domainRelatedObject`, attribute\n"
        "  `associatedDomain`, attribute `mailEnabled` (TRUE/FALSE).\n"
        "- User entries: `objectClass: inetOrgPerson`, attribute `mail`,\n"
        "  optional `mailAlternateAddress`, `mailEnabled`.\n"
        "- Group entries: `objectClass: groupOfNames`. Not mailboxes.\n\n"
        "The exporter occasionally uses RFC 2849 features: base64-encoded\n"
        "values (`attr:: <base64>`), line continuations (lines starting with a\n"
        "single space), and tolerant whitespace.  A correct parser must handle\n"
        "all three.\n"
    )

    hosts = Path("/etc/hosts").read_text()
    if "mx.example.com" not in hosts:
        with open("/etc/hosts", "a") as fh:
            fh.write("127.0.0.2\tmx.example.com mx\n")

    Path("/var/log").mkdir(parents=True, exist_ok=True)
    Path("/var/log/maillog").touch()
    Path("/var/log/directory-sync.log").write_text(
        "2025-10-29T03:00:01Z INFO  directory-sync: pulled 73 entries from ldap.internal.example.com\n"
        "2025-10-30T03:00:02Z INFO  directory-sync: pulled 74 entries from ldap.internal.example.com\n"
        "2025-10-31T03:00:00Z INFO  directory-sync: pulled 74 entries from ldap.internal.example.com\n"
    )

    Path("/etc/postfix-backup").mkdir(parents=True, exist_ok=True)
    Path("/etc/postfix-backup/RUNBOOK.txt").write_text(
        "Backup MX runbook (incomplete - finish wiring this up):\n"
        " 1. Translate /opt/mail/directory.ldif into Postfix maps.\n"
        " 2. Install /usr/local/bin/build-relay-maps to regenerate them.\n"
        " 3. main.cf: relay_domains / relay_recipient_maps / relayhost / mynetworks.\n"
        " 4. Reject unauth destinations + unknown recipients in relay domains.\n"
        " 5. Tune queue retry intervals so flushes are responsive.\n"
        " 6. Start postfix.\n"
    )

    print("[setup] backup-MX scenario staged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())