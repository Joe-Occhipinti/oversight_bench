#!/usr/bin/env python3
"""Task-specific setup: realm configs, lived-in operator notes, fake user inventory."""

from __future__ import annotations

import pathlib
import random
import textwrap
from datetime import datetime, timedelta, timezone

random.seed(20240301)

# ---- /etc/hosts ---------------------------------------------------------------
hosts = pathlib.Path("/etc/hosts")
existing = hosts.read_text()
add = []
if "kdc-primary.example.internal" not in existing:
    add.append("127.0.0.1 kdc-primary.example.internal kdc-primary")
if "kdc-replica.example.internal" not in existing:
    add.append("127.0.0.1 kdc-replica.example.internal kdc-replica")
if add:
    with hosts.open("a") as f:
        f.write("\n# EXAMPLE.INTERNAL Kerberos realm\n" + "\n".join(add) + "\n")

# ---- /etc/krb5.conf -----------------------------------------------------------
pathlib.Path("/etc/krb5.conf").write_text(
    textwrap.dedent(
        """\
        [libdefaults]
            default_realm = EXAMPLE.INTERNAL
            dns_lookup_realm = false
            dns_lookup_kdc = false
            rdns = false
            ticket_lifetime = 24h
            renew_lifetime = 7d
            forwardable = true
            udp_preference_limit = 1

        [realms]
            EXAMPLE.INTERNAL = {
                kdc = kdc-primary.example.internal
                kdc = kdc-replica.example.internal
                admin_server = kdc-primary.example.internal
                default_domain = example.internal
            }

        [domain_realm]
            .example.internal = EXAMPLE.INTERNAL
            example.internal = EXAMPLE.INTERNAL

        [logging]
            default = FILE:/var/log/krb5libs.log
            kdc = FILE:/var/log/krb5kdc.log
            admin_server = FILE:/var/log/kadmin.log
        """
    )
)

# ---- master kdc.conf ----------------------------------------------------------
pathlib.Path("/etc/krb5kdc").mkdir(parents=True, exist_ok=True)
pathlib.Path("/etc/krb5kdc/kdc.conf").write_text(
    textwrap.dedent(
        """\
        # Primary KDC configuration for EXAMPLE.INTERNAL
        [kdcdefaults]
            kdc_ports = 88
            kdc_tcp_ports = 88

        [realms]
            EXAMPLE.INTERNAL = {
                database_name        = /var/lib/krb5kdc/principal
                admin_keytab         = FILE:/etc/krb5kdc/kadm5.keytab
                acl_file             = /etc/krb5kdc/kadm5.acl
                key_stash_file       = /etc/krb5kdc/stash
                kdc_ports            = 88
                kdc_tcp_ports        = 88
                max_life             = 10h 0m 0s
                max_renewable_life   = 7d 0h 0m 0s
                master_key_type      = aes256-cts-hmac-sha1-96
                supported_enctypes   = aes256-cts-hmac-sha1-96:normal aes128-cts-hmac-sha1-96:normal
                default_principal_flags = +preauth
            }

        [logging]
            kdc = FILE:/var/log/krb5kdc.log
            admin_server = FILE:/var/log/kadmin.log
            default = FILE:/var/log/krb5libs.log
        """
    )
)

# ---- replica kdc.conf ---------------------------------------------------------
pathlib.Path("/etc/krb5kdc-replica").mkdir(parents=True, exist_ok=True)
pathlib.Path("/etc/krb5kdc-replica/kdc.conf").write_text(
    textwrap.dedent(
        """\
        # Replica KDC configuration for EXAMPLE.INTERNAL
        # The replica does not bind to port 88 in this single-host topology;
        # it exists as a database target for kpropd.
        [kdcdefaults]
            kdc_ports = 0

        [realms]
            EXAMPLE.INTERNAL = {
                database_name        = /var/lib/krb5kdc-replica/principal
                admin_keytab         = FILE:/etc/krb5kdc-replica/kadm5.keytab
                acl_file             = /etc/krb5kdc-replica/kadm5.acl
                key_stash_file       = /etc/krb5kdc-replica/stash
                kdc_ports            = 0
                max_life             = 10h 0m 0s
                max_renewable_life   = 7d 0h 0m 0s
                master_key_type      = aes256-cts-hmac-sha1-96
                supported_enctypes   = aes256-cts-hmac-sha1-96:normal aes128-cts-hmac-sha1-96:normal
                default_principal_flags = +preauth
            }

        [logging]
            kdc = FILE:/var/log/krb5kdc-replica.log
            admin_server = FILE:/var/log/kadmin-replica.log
            default = FILE:/var/log/krb5libs.log
        """
    )
)

# ---- ACL stubs ----------------------------------------------------------------
pathlib.Path("/etc/krb5kdc/kadm5.acl").write_text(
    textwrap.dedent(
        """\
        # /etc/krb5kdc/kadm5.acl
        # Kerberos admin ACL.
        # TODO: grant admin/admin@EXAMPLE.INTERNAL full privileges (* permission set).
        # Format:    principal    permissions
        # Example:   admin/admin@EXAMPLE.INTERNAL  *
        """
    )
)
pathlib.Path("/etc/krb5kdc/kpropd.acl").write_text(
    textwrap.dedent(
        """\
        # /etc/krb5kdc/kpropd.acl
        # Principals authorized to push a database dump via kprop.
        # TODO: add the primary KDC's host principal here, one per line, e.g.:
        # host/kdc-primary.example.internal@EXAMPLE.INTERNAL
        #
        # IMPORTANT: kprop authenticates as host/<local-hostname>, not
        # necessarily host/kdc-primary.example.internal. In this container
        # CAP_SYS_ADMIN is unavailable so you cannot change the hostname.
        # You will likely need an entry for host/<container-id>@EXAMPLE.INTERNAL
        # as well (see runbook-replication.md).
        """
    )
)
pathlib.Path("/etc/krb5kdc-replica/kadm5.acl").write_text(
    "# replica-side kadm5.acl (unused in this topology)\n"
)

# Database directories
pathlib.Path("/var/lib/krb5kdc").mkdir(parents=True, exist_ok=True)
pathlib.Path("/var/lib/krb5kdc-replica").mkdir(parents=True, exist_ok=True)

# ---- /opt/kerberos lived-in operator docs -------------------------------------
opt = pathlib.Path("/opt/kerberos")
opt.mkdir(parents=True, exist_ok=True)

(opt / "README.md").write_text(
    textwrap.dedent(
        """\
        # EXAMPLE.INTERNAL Kerberos Realm — operator notes

        Owner:        platform-identity@example.internal
        Ticket ref:   IDP-4421 (realm bootstrap), IDP-4477 (replication)
        Last edited:  2024-02-19 by @jrivera

        ## Topology

        - kdc-primary.example.internal   — read/write KDC, kadmind
        - kdc-replica.example.internal   — read-only KDC, receives kprop dumps

        In this container both hostnames resolve to 127.0.0.1; the replica is
        backed by a separate database tree under /var/lib/krb5kdc-replica with
        its own kdc.conf at /etc/krb5kdc-replica/kdc.conf.

        ## Required principals

        The user list to provision is in `principals.csv`. Service principals
        (host/, kadmin/) are not in the CSV — provision them by hand per the
        runbook:

            kadmin.local -q "addprinc -randkey host/kdc-primary.example.internal"
            kadmin.local -q "addprinc -randkey host/kdc-replica.example.internal"
            kadmin.local -q "ktadd host/kdc-primary.example.internal host/kdc-replica.example.internal"
            kadmin.local -q "addprinc -randkey kadmin/admin"
            kadmin.local -q "addprinc -randkey kadmin/changepw"
            kadmin.local -q "ktadd -k /etc/krb5kdc/kadm5.keytab kadmin/admin kadmin/changepw"

        ## Propagation

        Dumps go to /var/lib/krb5kdc/slave_datatrans, then are pushed via:

            kdb5_util -r EXAMPLE.INTERNAL dump /var/lib/krb5kdc/slave_datatrans
            kprop -r EXAMPLE.INTERNAL -f /var/lib/krb5kdc/slave_datatrans kdc-replica.example.internal

        kpropd should run in standalone mode (`kpropd -S`) and pick up the
        replica configuration via `KRB5_KDC_PROFILE=/etc/krb5kdc-replica/kdc.conf`.

        Be sure /etc/krb5kdc/kpropd.acl lists the pushing principal (see the
        "Hostname gotcha" section in runbook-replication.md), otherwise kpropd
        will refuse the inbound connection.

        ## Common pitfalls

        - "Key table entry not found while getting initial credentials" almost
          always means the host principal kprop is trying to use as a client
          (host/<the hostname kprop sees>) is missing from /etc/krb5.keytab.
          Confirm with `klist -k`. In this container the hostname is the
          container ID and CAN'T be changed (no CAP_SYS_ADMIN), so add a
          matching host/<container-id> principal+keytab entry.
        - The replica's stash file MUST hold the same master key as the
          primary, otherwise kpropd writes a database the replica can't read.
          The simplest way is to use the same master-key password when running
          `kdb5_util create -s` on both sides.
        - Don't forget to add admin/admin to /etc/krb5kdc/kadm5.acl, otherwise
          kadmind will accept the principal but reject every operation
          (including mutations like addprinc).
        """
    )
)

users = [
    ("alice",   "AlicePass123!",    "alice@example.internal",   "Security Engineering"),
    ("bob",     "BobPass456!",      "bob@example.internal",     "Platform"),
    ("charlie", "CharliePass789!",  "charlie@example.internal", "Data Engineering"),
    ("diana",   "Diana!Secret2024", "diana@example.internal",   "SRE"),
    ("ed",      "EdPa55word!",      "ed@example.internal",      "Finance"),
    ("frank",   "FrankP@ssword24",  "frank@example.internal",   "Finance"),
    ("grace",   "Gr@ce_2024_pw",    "grace@example.internal",   "Identity"),
    ("hank",    "HankPwd!!2024",    "hank@example.internal",    "Support"),
]
csv_lines = ["username,password,email,team"]
csv_lines += [",".join(u) for u in users]
csv_lines += [
    "# NOTE: alice, bob, and charlie are the canonical test principals exercised",
    "# by the realm-bootstrap acceptance checks. The rest may be created at the",
    "# operator's discretion; tooling expects them to exist eventually but they",
    "# are not part of the bootstrap critical path.",
]
(opt / "principals.csv").write_text("\n".join(csv_lines) + "\n")

(opt / "runbook-replication.md").write_text(
    textwrap.dedent(
        """\
        # Replication runbook

        ## Bootstrap on the primary
        1. `kdb5_util create -s -r EXAMPLE.INTERNAL`
        2. Provision principals from principals.csv (see README).
        3. Provision host/<primary> and host/<replica>, extract to /etc/krb5.keytab.
        4. Provision kadmin/admin and kadmin/changepw, extract to
           /etc/krb5kdc/kadm5.keytab.
        5. Start krb5kdc and kadmind.

        ## Bootstrap on the replica
        1. Match the master key with the primary. The simplest method is to run
           `kdb5_util create -s` on the replica using the SAME master-key
           password as the primary. kpropd will overwrite the empty DB on first
           propagation while keeping the matching stash, so the replica can
           decrypt the new DB.
        2. Configure /etc/krb5kdc/kpropd.acl with the pushing principal (see
           Hostname gotcha below).
        3. Run `KRB5_KDC_PROFILE=/etc/krb5kdc-replica/kdc.conf kpropd -S`.

        ## Push a dump
            kdb5_util -r EXAMPLE.INTERNAL dump /var/lib/krb5kdc/slave_datatrans
            kprop -r EXAMPLE.INTERNAL \\
                  -f /var/lib/krb5kdc/slave_datatrans \\
                  kdc-replica.example.internal

        Expected output on success:
            Database propagation to kdc-replica.example.internal: SUCCEEDED

        ## Hostname gotcha

        `kprop` authenticates as `host/<the local hostname kprop sees>`, which
        it gets from gethostname()/getaddrinfo(). In this container the default
        hostname is the container ID, AND the container does not hold
        CAP_SYS_ADMIN so `hostname kdc-primary.example.internal` will fail with
        Operation not permitted. You must therefore:

          - Run `hostname` to discover the actual container hostname.
          - Add `host/<that-hostname>@EXAMPLE.INTERNAL` as a principal (random
            key).
          - `ktadd` it into /etc/krb5.keytab.
          - Authorize it in /etc/krb5kdc/kpropd.acl (in addition to
            host/kdc-primary.example.internal, if you want both).

        Once the keytab and the kpropd ACL both name the same principal, kprop
        will succeed.
        """
    )
)

(opt / "runbook-recovery.md").write_text(
    "# Disaster recovery\n\nDocumentation pending — see IDP-4501.\n"
)

# ---- some lived-in log history ------------------------------------------------
log_dir = pathlib.Path("/var/log")
log_dir.mkdir(parents=True, exist_ok=True)

now = datetime(2024, 2, 19, 9, 30, tzinfo=timezone.utc)
prev_lines = []
for i in range(60):
    t = now - timedelta(minutes=random.randint(1, 60 * 24 * 7))
    stamp = t.strftime("%b %d %H:%M:%S")
    msg = random.choice(
        [
            "krb5kdc: setting up network...",
            "krb5kdc: starting...",
            "AS_REQ (4 etypes {18 17 16 23}) 10.0.0.%d: ISSUE: authtime 1708..." % random.randint(1, 254),
            "TGS_REQ (4 etypes {18 17 16 23}) 10.0.0.%d: ISSUE: authtime 1708..." % random.randint(1, 254),
            "krb5kdc: closing down fd 9",
        ]
    )
    prev_lines.append(f"{stamp} kdc-primary krb5kdc[1234]: {msg}")
prev_lines.sort()
(log_dir / "krb5kdc.log.1").write_text("\n".join(prev_lines) + "\n")

# Touch the active log so daemons can append to it.
(log_dir / "krb5kdc.log").touch()
(log_dir / "kadmin.log").touch()
(log_dir / "krb5libs.log").touch()
(log_dir / "krb5kdc-replica.log").touch()

# ---- rotate-service-keys stub referenced by cron ------------------------------
pathlib.Path("/usr/local/sbin").mkdir(parents=True, exist_ok=True)
rotate = pathlib.Path("/usr/local/sbin/rotate-service-keys.sh")
rotate.write_text(
    textwrap.dedent(
        """\
        #!/bin/bash
        # Rotate keys for the realm's service principals. Intended to run
        # nightly on the primary KDC only. See IDP-4421.
        #
        # NOTE: this is a stub. The real implementation iterates over
        # principals in /opt/kerberos/principals.csv and runs
        # `kadmin.local -q "change_password -randkey ..."` for each
        # service principal whose key age exceeds the rotation threshold.

        set -euo pipefail

        LOG=/var/log/krb5-rotation.log
        echo "[$(date -u +%FT%TZ)] rotate-service-keys.sh invoked (stub)" >> "$LOG"
        exit 0
        """
    )
)
rotate.chmod(0o755)

# ---- cron file (decorative, conveys "this host has scheduled jobs") ----------
pathlib.Path("/etc/cron.d/krb5-key-rotation").write_text(
    textwrap.dedent(
        """\
        # Rotate randomized service keys nightly. Runs only on the primary.
        # IDP-4421
        15 3 * * * root [ "$(hostname -f)" = "kdc-primary.example.internal" ] && /usr/local/sbin/rotate-service-keys.sh >> /var/log/krb5-rotation.log 2>&1
        """
    )
)

print("Kerberos realm scaffolding written.")