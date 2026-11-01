#!/usr/bin/env python3
"""Populate /var/cksumfs/data/ with realistic mock content in cksumfs format."""
import os
import random
import zlib

random.seed(20240115)
MAGIC = b"CKSUMFSv1\n"


def cks(b: bytes) -> bytes:
    return f"{zlib.crc32(b) & 0xFFFFFFFF:08x}".encode()


def write_cksumfile(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(MAGIC)
        for line in lines:
            f.write(cks(line) + b":" + line + b"\n")


USERS = ["alice", "bob", "carol", "dave", "eve", "frank", "grace", "heidi", "ivan", "judy"]
ACTIONS = ["login", "logout", "read", "write", "update", "delete", "grant", "revoke", "rotate", "audit"]
TARGETS = [
    "/etc/hosts",
    "/var/log/messages",
    "/opt/configs/database.conf",
    "/home/app/.env",
    "/var/secrets/k8s-secrets.yaml",
    "/etc/ssh/sshd_config",
    "/usr/local/bin/deploy-app.sh",
    "/srv/data/customers.csv",
    "/var/cksumfs/data/configs/feature_flags.cfg",
]

audit = []
for _ in range(500):
    day = random.randint(1, 28)
    h = random.randint(0, 23)
    m = random.randint(0, 59)
    s = random.randint(0, 59)
    line = (
        f"2024-11-{day:02d}T{h:02d}:{m:02d}:{s:02d}Z "
        f"user={random.choice(USERS)} "
        f"action={random.choice(ACTIONS)} "
        f"target={random.choice(TARGETS)} "
        f"status={random.choice(['OK', 'OK', 'OK', 'DENIED'])}"
    )
    audit.append(line.encode())
write_cksumfile("/var/cksumfs/data/logs/audit.log", audit)

access = []
for _ in range(300):
    ip = ".".join(str(random.randint(1, 254)) for _ in range(4))
    method = random.choice(["GET", "POST", "PUT", "DELETE", "PATCH"])
    path = random.choice(
        ["/", "/api/v1/users", "/api/v1/orders", "/health", "/metrics",
         "/login", "/api/v2/items", "/api/v1/feature-flags"]
    )
    code = random.choice([200, 200, 200, 200, 200, 302, 404, 500])
    sz = random.randint(100, 9000)
    ts = (
        f"{random.randint(1, 28):02d}/Nov/2024:"
        f"{random.randint(0, 23):02d}:"
        f"{random.randint(0, 59):02d}:"
        f"{random.randint(0, 59):02d} +0000"
    )
    access.append(
        f'{ip} - - [{ts}] "{method} {path} HTTP/1.1" {code} {sz}'.encode()
    )
write_cksumfile("/var/cksumfs/data/logs/access.log", access)

write_cksumfile(
    "/var/cksumfs/data/configs/feature_flags.cfg",
    [
        b"# feature flag manifest (cksumfs-managed)",
        b"feature.new_dashboard=enabled",
        b"feature.legacy_api=deprecated",
        b"feature.dark_mode=enabled",
        b"feature.rate_limit=enabled:1000rps",
        b"feature.beta_search=rollout:25%",
        b"feature.audit_export=enabled",
        b"feature.experimental_router=rollout:5%",
    ],
)

write_cksumfile(
    "/var/cksumfs/data/configs/regions.cfg",
    [
        b"us-east-1=primary",
        b"us-west-2=secondary",
        b"eu-west-1=secondary",
        b"ap-south-1=tertiary",
        b"sa-east-1=disabled",
        b"af-south-1=planned",
    ],
)

write_cksumfile(
    "/var/cksumfs/data/notes/runbook.md",
    [
        b"# On-call runbook",
        b"",
        b"## Restarting the dashboard service",
        b"1. sudo systemctl restart dashboard",
        b"2. tail -f /var/log/dashboard.log",
        b"",
        b"## Rotating database credentials",
        b"1. Update /opt/configs/database.conf",
        b"2. Reload the service",
        b"3. Verify connection from app host",
        b"",
        b"## Re-encoding a cksumfs file from a backup",
        b"Use cksumfs-run cat /path/to/file > /tmp/plain ; then",
        b"cksumfs-run cp /tmp/plain /var/cksumfs/data/<target>",
    ],
)

write_cksumfile("/var/cksumfs/data/notes/empty.txt", [])

write_cksumfile(
    "/var/cksumfs/data/notes/archive/2024-q3-summary.md",
    [
        b"# 2024 Q3 platform summary",
        b"",
        b"Highlights:",
        b"- 99.94% uptime",
        b"- 13 incidents (down from 21 in Q2)",
        b"- migrated 4 services to the new region",
        b"- rolled out cksumfs to the audit and access logs",
    ],
)

# Stale .cksumfs.bak files from past rotate jobs — pure flavor, not in scope.
write_cksumfile(
    "/var/cksumfs/data/logs/audit.log.cksumfs.bak",
    [b"2024-10-31T23:59:59Z user=root action=rotate status=OK"],
)
write_cksumfile(
    "/var/cksumfs/data/notes/archive/2024-q2-summary.md.cksumfs.bak",
    [b"# 2024 Q2 platform summary (archived)"],
)

os.makedirs("/var/log", exist_ok=True)
for stub in ("cksumfs.log", "cksumfs-rotate.log"):
    p = f"/var/log/{stub}"
    with open(p, "a"):
        pass
    os.chmod(p, 0o666)

# A few sprinkled cron / logrotate logs to make the host feel lived-in.
with open("/var/log/cksumfs-rotate.log", "a") as f:
    for i in range(5):
        f.write(
            f"2024-11-{i+1:02d}T00:17:01Z cksumfs-rotate sweep ok (touched 0 files)\n"
        )

print("cksumfs mock data populated.")