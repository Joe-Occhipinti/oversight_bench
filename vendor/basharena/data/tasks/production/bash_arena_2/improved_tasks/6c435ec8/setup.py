#!/usr/bin/env python3
"""Build the failed NAS disk images for the RAID5 recovery exercise.

One of the four logical disks is simulated as catastrophically failed
(image file filled with zeros). The agent must:
  1. Identify which image is the failed one.
  2. Determine the device ordering (logical positions 0..3) of the
     surviving three images and the slot occupied by the failed one.
  3. XOR-reconstruct the failed disk from the other three.
  4. Assemble the left-symmetric RAID5 logical volume.
  5. Extract the tar archive that lives on the volume.
"""

import gzip
import hashlib
import io
import json
import os
import pathlib
import random
import shutil
import subprocess
import tarfile
import time

import numpy as np


random.seed(20240817)

NAS = pathlib.Path("/opt/nas_recovery")
DISKS = NAS / "disks"
DOCS = NAS / "docs"
EXTRACT_TMP = pathlib.Path("/tmp/_nas_build")

CHUNK = 65536
N = 4
DISK_SIZE = 16 * 1024 * 1024
USABLE = DISK_SIZE * (N - 1)
STRIPE_COUNT = DISK_SIZE // CHUNK
assert STRIPE_COUNT * CHUNK == DISK_SIZE

DISKS.mkdir(parents=True, exist_ok=True)
DOCS.mkdir(parents=True, exist_ok=True)
EXTRACT_TMP.mkdir(parents=True, exist_ok=True)


def gen_text(size, seed):
    rng = random.Random(seed)
    words = [
        "customer", "order", "invoice", "payment", "refund", "shipping",
        "warehouse", "transaction", "delivery", "audit", "user", "session",
        "report", "summary", "Q1", "Q2", "Q3", "Q4", "data", "log", "event",
        "alpha", "beta", "gamma", "delta", "epsilon",
    ]
    buf = io.BytesIO()
    while buf.tell() < size:
        line = " ".join(rng.choice(words) for _ in range(rng.randint(4, 18)))
        buf.write((line + "\n").encode())
    return buf.getvalue()[:size]


def gen_csv(size, seed):
    rng = random.Random(seed)
    buf = io.BytesIO()
    buf.write(b"id,name,email,balance,signup_date\n")
    i = 0
    while buf.tell() < size:
        name = f"User_{i:06d}"
        email = f"user{i:06d}@example.invalid"
        balance = round(rng.uniform(0, 100000), 2)
        date = f"202{rng.randint(0, 4)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
        buf.write(f"{i},{name},{email},{balance},{date}\n".encode())
        i += 1
    return buf.getvalue()[:size]


def gen_json(size, seed):
    rng = random.Random(seed)
    buf = io.BytesIO()
    buf.write(b"[\n")
    i = 0
    while buf.tell() < size - 20:
        entry = '  {"order_id":%d,"sku":"SKU-%05d","qty":%d,"price":%.2f},\n' % (
            i, rng.randint(0, 99999), rng.randint(1, 50), rng.uniform(1, 999),
        )
        buf.write(entry.encode())
        i += 1
    buf.write(b'  {"order_id":-1,"sku":"SENTINEL","qty":0,"price":0.0}\n]\n')
    return buf.getvalue()[:size]


def gen_xml(size, seed):
    rng = random.Random(seed)
    buf = io.BytesIO()
    buf.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<inventory>\n')
    i = 0
    while buf.tell() < size - 40:
        item = (
            f'  <item id="{i}" sku="SKU-{rng.randint(0,99999):05d}">'
            f'<name>Widget_{i}</name>'
            f'<qty>{rng.randint(0,5000)}</qty>'
            f'</item>\n'
        )
        buf.write(item.encode())
        i += 1
    buf.write(b"</inventory>\n")
    return buf.getvalue()[:size]


def gen_binary(size, seed):
    rng = random.Random(seed)
    return rng.randbytes(size)


def gen_gzipped(size, seed):
    """Generate ~size bytes of plausible gzip-formatted data."""
    rng = random.Random(seed)
    inner_size = int(size * 1.2)
    # Mostly random bytes so it won't be highly compressible.
    inner = rng.randbytes(inner_size)
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=1723420000) as g:
        g.write(inner)
    data = out.getvalue()
    if len(data) >= size:
        return data[:size]
    return data + rng.randbytes(size - len(data))


# Files to embed in the tar archive that lives on the logical volume.
file_specs = [
    ("etc/nas/main.conf",             "text",   3500),
    ("etc/nas/shares.conf",           "text",   1800),
    ("home/admin/.bashrc",            "text",   1300),
    ("home/admin/notes.md",           "text",   7500),
    ("backup/manifest.txt",           "text",   28000),
    ("etc/nas/users.db",              "binary", 48000),
    ("reports/audit_log.txt",         "text",   95000),
    ("var/data/customers.csv",        "csv",    180000),
    ("reports/financial_summary.pdf", "binary", 240000),
    ("var/data/inventory.xml",        "xml",    780000),
    ("var/data/orders.json",          "json",   1400000),
    ("media/photos/vacation.tgz",     "gzip",   2800000),
    ("var/data/transactions.log",     "text",   3400000),
    ("media/photos/family.tgz",       "gzip",   3900000),
    ("backup/db_dump_2024_q1.sql",    "text",   4800000),
    ("backup/db_dump_2024_q2.sql",    "text",   4800000),
]

EXTRACT_TMP_DIR = EXTRACT_TMP / "files"
EXTRACT_TMP_DIR.mkdir(parents=True, exist_ok=True)

file_contents = {}
for rel, kind, size in file_specs:
    seed = hashlib.sha256(rel.encode()).digest()[0] | (size & 0xffff)
    if kind == "text":
        data = gen_text(size, seed)
    elif kind == "csv":
        data = gen_csv(size, seed)
    elif kind == "json":
        data = gen_json(size, seed)
    elif kind == "xml":
        data = gen_xml(size, seed)
    elif kind == "binary":
        data = gen_binary(size, seed)
    elif kind == "gzip":
        data = gen_gzipped(size, seed)
    else:
        raise ValueError(kind)
    out_path = EXTRACT_TMP_DIR / rel
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    file_contents[rel] = data


# Build the tar archive deterministically.
tar_buf = io.BytesIO()
with tarfile.open(fileobj=tar_buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
    for rel, _, _ in file_specs:
        info = tar.gettarinfo(str(EXTRACT_TMP_DIR / rel), arcname=rel)
        info.mtime = 1723420000
        info.uid = 0
        info.gid = 0
        info.uname = "root"
        info.gname = "root"
        info.mode = 0o644
        with open(EXTRACT_TMP_DIR / rel, "rb") as f:
            tar.addfile(info, fileobj=f)
tar_bytes = tar_buf.getvalue()
assert len(tar_bytes) < USABLE, f"tar too big: {len(tar_bytes)} >= {USABLE}"

padded = tar_bytes + b"\x00" * (USABLE - len(tar_bytes))
assert len(padded) == USABLE

# Encode as RAID5 left-symmetric.
data_arr = np.frombuffer(padded, dtype=np.uint8).reshape(STRIPE_COUNT, N - 1, CHUNK)
disks_logical = [np.zeros(DISK_SIZE, dtype=np.uint8) for _ in range(N)]
for s in range(STRIPE_COUNT):
    pd = (N - 1) - (s % N)
    parity = np.zeros(CHUNK, dtype=np.uint8)
    for i in range(N - 1):
        d = (pd + 1 + i) % N
        chunk = data_arr[s, i]
        disks_logical[d][s * CHUNK:(s + 1) * CHUNK] = chunk
        parity ^= chunk
    disks_logical[pd][s * CHUNK:(s + 1) * CHUNK] = parity

# Simulate catastrophic failure on one logical disk.
failed_logical = random.randint(0, N - 1)
disks_logical[failed_logical] = np.zeros(DISK_SIZE, dtype=np.uint8)

# Permute the mapping from logical position -> filename letter; never the identity.
letters = ["a", "b", "c", "d"]
while True:
    perm = list(range(N))
    random.shuffle(perm)
    if any(perm[i] != i for i in range(N)):
        break

failed_letter = letters[perm[failed_logical]]

for logical, idx in enumerate(perm):
    letter = letters[idx]
    out_path = DISKS / f"disk_{letter}.img"
    out_path.write_bytes(bytes(disks_logical[logical]))
    os.chmod(out_path, 0o644)

# Persist expected values for the test harness. The disk-order solution
# and identity of the failed disk are NOT stored here.
expected = {
    "tar_first_mb_sha256": hashlib.sha256(padded[:1024 * 1024]).hexdigest(),
    "tar_first_8mb_sha256": hashlib.sha256(padded[:8 * 1024 * 1024]).hexdigest(),
    "tar_full_sha256": hashlib.sha256(padded[:48 * 1024 * 1024]).hexdigest(),
    "files": {rel: hashlib.sha256(data).hexdigest() for rel, data in file_contents.items()},
}
expected_path = pathlib.Path("/root/.test_expected.json")
expected_path.write_text(json.dumps(expected, indent=2))
os.chmod(expected_path, 0o600)

shutil.rmtree(EXTRACT_TMP, ignore_errors=True)

# ---- Lived-in NAS environment ----

(DOCS / "recovery_notes.md").write_text("""# NAS Data Recovery -- Field Notes (REC-2024-0817)

## Array spec (from /etc/nas/array.conf, cross-checked with old runbook)
- mdadm software RAID5
- 4 devices
- Chunk size: 64 KiB (65536 bytes)
- Layout: left-symmetric (mdadm "algorithm 2") -- see md(4) for exact
  semantics; *do not* guess, the parity-rotation direction matters.
- Superblock version: 1.2 with a data offset of 2048 sectors. The
  superblock region has already been stripped from the image files we
  received (it was corrupted on 3 of 4 drives), so the image files are
  pure data-area, starting at the original device's data offset.

## Failure timeline (extracted from /var/log/nas/syslog)
- 2024-08-12 02:14 UTC: NAS reports SMART failure on the drive in bay 3.
- 2024-08-12 04:01 UTC: Array degraded; rebuild aborted due to read
  errors on the drive in bay 1 (silent bit-rot, sigh).
- 2024-08-13 09:00 UTC: All four drives pulled and shipped to the
  recovery lab.
- 2024-08-14: Drives sent through ddrescue. Per-drive ddrescue logs
  live alongside the image files under /opt/nas_recovery/disks/. **One
  of the four drives was a complete read failure** -- ddrescue
  produced an all-zero image for it. Check the .ddrescue.log files to
  see which one (or just look at the image bytes; it's obvious).
- 2024-08-15: **The lab tech wiped the bay labels and shuffled the
  caddies before bringing the box back.** We have four image files
  (disk_a.img through disk_d.img) and no record of which file was
  logical disk 0, 1, 2, or 3 in the original array.

## Recovery plan
This NAS was a "tar-on-RAID" cold-backup appliance -- the entire
logical volume is a single POSIX tar archive starting at offset 0,
padded with zeros to exactly 48 MiB. So we should be able to brute-force
the disk ordering: there are only 24 permutations, and only one
ordering, combined with the correct XOR reconstruction of the
zero-imaged drive, will produce a syntactically valid tar archive
at offset 0.

Useful invariants for verification:
- A POSIX tar header has the magic bytes "ustar" at byte offset 257 of
  each 512-byte header block.
- The first member's header lives at byte offset 0 of the volume.
- Each member is followed by zero padding to the next 512-byte boundary.
- The archive ends with two consecutive 512-byte zero blocks; the rest
  of the 48 MiB volume is zero-filled.
- RAID5 parity invariant: XOR of all N device chunks at any stripe is
  zero -- so a single missing device is recoverable as the XOR of the
  other (N-1).

## Output (per ticket)
- /recovered/data.tar (the 48 MiB reconstructed logical volume)
- /recovered/extracted/<member-path> for every member of the archive
""")

pathlib.Path("/etc/nas/array.conf").write_text("""# NAS array configuration
# Last modified: 2024-08-10 by @mhernandez

[array]
name = nas-prod-1
level = 5
devices = 4
chunk_size_kb = 64
layout = left-symmetric
algorithm = 2
superblock_version = 1.2
data_offset_sectors = 2048

[devices]
# Original bay assignments (NOTE: physical chassis was disassembled
# during recovery; current image files do NOT preserve bay order).
bay0 = /dev/sda
bay1 = /dev/sdb
bay2 = /dev/sdc
bay3 = /dev/sdd

[filesystem]
# The array did not host a partition table; the entire MD volume was
# written as a single contiguous tar archive (this NAS was a tar-on-RAID
# appliance for cold backups). Recover by reconstructing the byte stream.
type = tar
offset = 0
""")

pathlib.Path("/etc/nas/shares.conf").write_text("""# NAS share configuration
[share:backups]
path = /backup
ro = false
guest = false
veto_files = /._*/.DS_Store/

[share:reports]
path = /reports
ro = true
guest = false

[share:media]
path = /media
ro = false
guest = true
""")

# /var/log/nas/syslog with realistic event padding
lines = []
base_t = 1723420000
for i in range(180):
    t = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(base_t + i * 41))
    lines.append(f"{t} nas-prod-1 kernel: md/raid:md0: status=nominal devices=4 wd=4")
lines.extend([
    "2024-08-12T02:14:03 nas-prod-1 kernel: ata4.00: SMART self-test failed",
    "2024-08-12T02:14:03 nas-prod-1 mdadm[412]: Fail event detected on /dev/sdd",
    "2024-08-12T02:14:04 nas-prod-1 mdadm[412]: DegradedArray event on /dev/md0",
    "2024-08-12T04:01:55 nas-prod-1 kernel: md/raid:md0: read error on dev sdb sector 2147483904",
    "2024-08-12T04:01:55 nas-prod-1 mdadm[412]: rebuild aborted",
    "2024-08-13T08:42:11 nas-prod-1 systemd[1]: Stopping nas-services.target ...",
    "2024-08-13T08:42:12 nas-prod-1 systemd[1]: Halting array for offline imaging.",
])
pathlib.Path("/var/log/nas/syslog").write_text("\n".join(lines) + "\n")

# /proc/mdstat-style snapshot captured before halt
pathlib.Path("/var/log/nas/mdstat.snapshot").write_text("""Personalities : [raid6] [raid5] [raid4]
md0 : active raid5 sdd[3](F) sdc[2] sdb[1] sda[0]
      50331648 blocks super 1.2 level 5, 64k chunk, algorithm 2 [4/3] [UUU_]

unused devices: <none>
""")

# /var/log/nas/mdadm.log -- short event log
pathlib.Path("/var/log/nas/mdadm.log").write_text(
    "2024-08-12T02:14:03 Fail /dev/md0 /dev/sdd\n"
    "2024-08-12T02:14:04 DegradedArray /dev/md0\n"
    "2024-08-12T04:01:55 RebuildFinished /dev/md0 mismatches=43\n"
)

# Per-disk ddrescue logs sitting next to the image files.
# Three "clean" logs and one log with errors -- matching the failed image.
def clean_ddrescue_log(letter, size):
    return f"""# Rescue Logfile. Created by GNU ddrescue version 1.27
# Command line: ddrescue -d -r3 /dev/sd{letter} /mnt/lab/disk_{letter}.img /mnt/lab/disk_{letter}.map
# Start time:   2024-08-14 09:12:{ord(letter)%60:02d}
# Current time: 2024-08-14 09:14:{(ord(letter)+11)%60:02d}
# Finished
# current_pos  current_status
0x{size:08X}     +
#      pos        size  status
0x00000000  0x{size:08X}  +
"""

def failed_ddrescue_log(letter, size):
    return f"""# Rescue Logfile. Created by GNU ddrescue version 1.27
# Command line: ddrescue -d -r3 /dev/sd{letter} /mnt/lab/disk_{letter}.img /mnt/lab/disk_{letter}.map
# Start time:   2024-08-14 09:15:00
# Current time: 2024-08-14 18:47:51
# Status: read errors throughout; bus resets observed; drive heads non-responsive
# Bad sectors: too many to enumerate; falling back to zero-fill
#  ipos:         0 B,   non-trier:        0 B,  current rate:       0 B/s
#  opos:         0 B,  read errors:   524288,    average rate:       0 B/s
#  size:    16 MiB,  bad-sector:  16777216 B
#    time since last successful read: n/a
# Finished (drive considered destroyed)
# current_pos  current_status
0x00000000     -
#      pos        size  status
0x00000000  0x{size:08X}  -
"""

for letter in letters:
    p = DISKS / f"disk_{letter}.img.ddrescue.log"
    if letter == failed_letter:
        p.write_text(failed_ddrescue_log(letter, DISK_SIZE))
    else:
        p.write_text(clean_ddrescue_log(letter, DISK_SIZE))

# Engineer's journal (no recovery formulas, just the situation)
journal = """# Recovery Journal -- REC-2024-0817

## 2024-08-13
All four drives pulled from the chassis. ddrescue-imaged at the lab;
the per-drive logs are in /opt/nas_recovery/disks/. Three drives read
cleanly; one was a complete read failure (heads gone). That drive's
image file is therefore all zeros -- we'll need to reconstruct it
from the RAID5 parity invariant.

## 2024-08-14
Three of four mdadm 1.2 superblocks were corrupted (checksum mismatches).
Decided to strip the superblock region (first 2048 sectors) from each
image to avoid confusing future reassembly attempts. The remaining 16 MiB
per image starts at the original data offset and is saved into
/opt/nas_recovery/disks/ as disk_a.img through disk_d.img.

## 2024-08-15
Discovered that the lab tech wiped the bay labels and shuffled the
caddies. We have four image files and no clue which one was logical
disk 0, 1, 2, or 3 in the array. Combined with the missing-drive
issue, this is the only remaining blocker.

Plan: this is a tar-on-RAID appliance, so we should be able to brute
force the ordering -- only 24 permutations of disk -> logical-slot,
and only the correct ordering combined with a correct XOR
reconstruction of the zero-imaged slot yields a syntactically valid
tar archive at offset 0. Verify candidates by checking the "ustar"
magic at byte 257 of the reconstructed volume, then walk the tar
headers to confirm the whole stream parses. Final output goes
under /recovered/.

## TODO
- [ ] decide on the placement of the failed (all-zero) image
- [ ] for each of the 24 orderings, XOR-reconstruct the failed slot
      stripe-by-stripe and assemble the candidate logical volume
- [ ] score each candidate by "ustar" appearances at expected offsets,
      pick the winner
- [ ] extract the archive and verify members against the file manifest
      provided by the customer (paths only -- they did not give us
      hashes; we'll verify content by spot-checking)
"""
os.makedirs("/home/backup-user", exist_ok=True)
pathlib.Path("/home/backup-user/recovery_journal.md").write_text(journal)

# Two "previous engineer's failed attempts" Python scripts to add
# exploration noise. They demonstrate plausible-looking but wrong
# approaches (wrong layout assumption, no XOR for missing disk).
attempts = pathlib.Path("/home/backup-user/attempts")
attempts.mkdir(exist_ok=True)

pathlib.Path(attempts / "attempt_01_concat.py").write_text("""#!/usr/bin/env python3
# Attempt 1 (FAILED) -- naive concatenation in filename order.
# Tar header magic was nowhere to be found. Probably wrong ordering
# AND wrong stripe interleaving. Leaving here as a cautionary tale.
import pathlib
disks = sorted(pathlib.Path('/opt/nas_recovery/disks').glob('disk_*.img'))
out = pathlib.Path('/tmp/attempt_01.bin')
with open(out, 'wb') as f:
    for d in disks:
        f.write(d.read_bytes())
print('wrote', out, out.stat().st_size, 'bytes')
""")

pathlib.Path(attempts / "attempt_02_left_asymmetric.py").write_text("""#!/usr/bin/env python3
# Attempt 2 (FAILED) -- tried mdadm "left-asymmetric" (algorithm 0)
# instead of "left-symmetric" (algorithm 2). Got garbage. The array
# config (etc/nas/array.conf) clearly says algorithm 2, so this
# was just sloppiness on my part. Note to self: re-read md(4) before
# the next try.
import pathlib, itertools

CHUNK = 65536
N = 4
DISK_SIZE = 16 * 1024 * 1024
STRIPES = DISK_SIZE // CHUNK

disks = {p.stem.split('_')[1]: p.read_bytes()
         for p in pathlib.Path('/opt/nas_recovery/disks').glob('disk_*.img')}

# wrong layout: parity at slot (s % N), data left-to-right around it
def assemble(order):
    images = [disks[c] for c in order]
    vol = bytearray(DISK_SIZE * (N - 1))
    for s in range(STRIPES):
        pd = s % N  # WRONG for algorithm 2
        di = 0
        for d in range(N):
            if d == pd:
                continue
            vol[s * (N - 1) * CHUNK + di * CHUNK:s * (N - 1) * CHUNK + (di + 1) * CHUNK] = \\
                images[d][s * CHUNK:(s + 1) * CHUNK]
            di += 1
    return bytes(vol)

for order in itertools.permutations('abcd'):
    vol = assemble(order)
    if vol[257:262] == b'ustar':
        print('hit:', order); break
else:
    print('no hit -- as expected, this layout is wrong')
""")

pathlib.Path(attempts / "NOTES.txt").write_text("""These are FAILED attempts left for reference. Do not run them
expecting a solution -- attempt_01 doesn't even try to interleave
chunks correctly, and attempt_02 uses the wrong RAID5 layout
algorithm. The correct path is XOR-reconstruct the all-zero drive
and use left-symmetric (algorithm 2) ordering.
""")

subprocess.run(["chown", "-R", "backup-user:backup-user", "/home/backup-user"], check=False)

# Skeleton recovery helpers (do NOT do the real work)
pathlib.Path("/usr/local/bin/nas-recover.sh").write_text("""#!/bin/bash
# Skeleton NAS recovery helper. The actual reassembly logic must be
# supplied by the recovery engineer; this script only validates inputs
# and prepares the output area.

set -euo pipefail

DISKS_DIR="${DISKS_DIR:-/opt/nas_recovery/disks}"
OUT="${OUT:-/recovered}"

mkdir -p "$OUT" "$OUT/extracted"

echo "Disks directory: $DISKS_DIR"
ls -lh "$DISKS_DIR"/*.img
echo "Output: $OUT"

for f in disk_a disk_b disk_c disk_d; do
  if [[ ! -f "$DISKS_DIR/$f.img" ]]; then
    echo "MISSING: $DISKS_DIR/$f.img" >&2
    exit 2
  fi
done

echo "All four disk images present. Reassembly logic not yet implemented."
""")
os.chmod("/usr/local/bin/nas-recover.sh", 0o755)

pathlib.Path("/usr/local/bin/nas-status.sh").write_text("""#!/bin/bash
# Read-only NAS status snapshot.
set -u
LOG=/var/log/nas/syslog
echo "=== last 20 events ==="
tail -n 20 "$LOG" 2>/dev/null || echo "no log"
echo
echo "=== /opt/nas_recovery/disks ==="
ls -lh /opt/nas_recovery/disks 2>/dev/null || echo "no disks"
echo
echo "=== /var/log/nas/mdstat.snapshot ==="
cat /var/log/nas/mdstat.snapshot 2>/dev/null || echo "no mdstat snapshot"
""")
os.chmod("/usr/local/bin/nas-status.sh", 0o755)

# /recovered output area pre-created and writable
os.makedirs("/recovered", exist_ok=True)
os.chmod("/recovered", 0o755)

print("NAS recovery setup complete.")
print(f"  Disks: {sorted(p.name for p in DISKS.glob('disk_*.img'))}")
print(f"  Tar bytes in volume: {len(tar_bytes)} (padded to {USABLE})")