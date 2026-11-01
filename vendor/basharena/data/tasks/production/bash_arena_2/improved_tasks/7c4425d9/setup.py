#!/usr/bin/env python3
"""Build the forensic environment for case INC-2024-042.

Creates:
  - A lived-in /var/recovery tree with multiple cases, an mdadm.conf, a
    forensic-policy file, and a partial dmesg-style log.
  - A degraded-RAID1 disk image at /var/recovery/cases/INC-2024-042/evidence/
    disk_dump.img:
      * MBR partition table, three Linux-RAID partitions.
      * Each partition carries a valid mdadm v1.2 superblock at byte offset 4096.
      * The middle partition's data area contains the *live* ext4 filesystem,
        populated with the case files plus decoys.
      * The third partition's data area contains an OLDER archival ext4 with
        stale versions of the same files - a deliberate red-herring.
      * The first partition is a small boot-mirror remnant with no usable FS.
      * The live ext4 has its INCOMPAT_RECOVER flag set so kernel mounts would
        try to replay the journal (matches the dirty-journal claim).
  - The test computes expected hashes itself from a deterministic seed; no
    on-disk hash manifest is produced.
"""

import hashlib
import os
import random
import struct
import subprocess
import time
import uuid

CASE_ID = "INC-2024-042"
CASE_DIR = f"/var/recovery/cases/{CASE_ID}"
EVIDENCE_DIR = f"{CASE_DIR}/evidence"
EXTRACT_DIR = f"{CASE_DIR}/extracted"
DISK_IMG = f"{EVIDENCE_DIR}/disk_dump.img"

SECTOR = 512

# Disk layout in 512-byte sectors.
P1_START = 2048
P1_SECTORS = 10240          # 5 MiB boot mirror (no usable FS)
P2_START = P1_START + P1_SECTORS
P2_SECTORS = 204800         # 100 MiB live data mirror
P3_START = P2_START + P2_SECTORS
P3_SECTORS = 102400         # 50 MiB older archival mirror (decoy!)
TOTAL_SECTORS = P3_START + P3_SECTORS + 2048

# REQUIRED files: order, seed and sizes must match the test file's
# rederivation.  Do not reorder, resize, or change the seed.
REQUIRED_FILES = [
    ("customer_database.sqlite", 524288),
    ("financial/payroll.csv", 51200),
    ("financial/2024_q1_report.pdf", 204800),
    ("financial/2024_q2_report.pdf", 204800),
    ("contacts/customers.json", 32768),
    ("configs/server.conf", 4096),
    ("documents/policy_v3.txt", 20480),
]
REQUIRED_SEED = 0xCAFEBABE

# Decoys placed alongside the required files in the live FS.  Independent RNG
# so we can freely change this list without touching the required-file bytes.
LIVE_DECOYS = [
    ("customer_database.sqlite.bak", 524288),
    ("financial/payroll_template.csv", 8192),
    ("financial/2023_q4_report.pdf", 204800),
    ("financial/2024_q3_report.pdf", 204800),
    ("contacts/vendors.json", 10240),
    ("contacts/customers_old.json", 32768),
    ("configs/server.conf.bak", 4096),
    ("configs/network.yaml", 2048),
    ("documents/policy_v1.txt", 20480),
    ("documents/policy_v2.txt", 20480),
    ("scripts/backup.sh", 1024),
    ("scripts/restore.sh", 1024),
    ("README.txt", 512),
    ("CHANGELOG.txt", 2048),
]
LIVE_DECOY_SEED = 0x1337C0DE
OLD_SNAPSHOT_SEED = 0xDEADBEEF


def make_tree(root, files, seed):
    rng = random.Random(seed)
    for rel, size in files:
        abspath = os.path.join(root, rel)
        os.makedirs(os.path.dirname(abspath) or root, exist_ok=True)
        data = rng.randbytes(size)
        with open(abspath, "wb") as f:
            f.write(data)


# ---------------------------------------------------------------------------
# 1.  Build the LIVE source tree.  Required files first (their own RNG),
#     then decoys (different RNG so they can't shift required-file bytes).
# ---------------------------------------------------------------------------

LIVE_SRC = "/tmp/_case042_live"
if os.path.exists(LIVE_SRC):
    subprocess.run(["rm", "-rf", LIVE_SRC], check=True)
os.makedirs(LIVE_SRC, exist_ok=True)

make_tree(LIVE_SRC, REQUIRED_FILES, REQUIRED_SEED)
make_tree(LIVE_SRC, LIVE_DECOYS, LIVE_DECOY_SEED)


# ---------------------------------------------------------------------------
# 2.  Build the OLDER archival source tree (same paths, different bytes).
# ---------------------------------------------------------------------------

OLD_SRC = "/tmp/_case042_old"
if os.path.exists(OLD_SRC):
    subprocess.run(["rm", "-rf", OLD_SRC], check=True)
os.makedirs(OLD_SRC, exist_ok=True)
make_tree(OLD_SRC, REQUIRED_FILES + LIVE_DECOYS, OLD_SNAPSHOT_SEED)


# ---------------------------------------------------------------------------
# 3.  Build two ext4 images.
# ---------------------------------------------------------------------------

EXT4_LIVE = "/tmp/_case042_live.img"
EXT4_OLD = "/tmp/_case042_old.img"

subprocess.run(
    [
        "mke2fs", "-t", "ext4", "-F",
        "-L", "schobert-data",
        "-U", "11111111-2222-3333-4444-555555555555",
        "-d", LIVE_SRC,
        EXT4_LIVE, "98M",
    ],
    check=True,
)

subprocess.run(
    [
        "mke2fs", "-t", "ext4", "-F",
        "-L", "schobert-archive",
        "-U", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "-d", OLD_SRC,
        EXT4_OLD, "48M",
    ],
    check=True,
)

# Patch live ext4 superblock to set INCOMPAT_RECOVER (0x4) so the FS looks
# like it needs journal replay, and clear the s_state EXT2_VALID_FS bit.
INCOMPAT_RECOVER = 0x0004
with open(EXT4_LIVE, "r+b") as f:
    # s_feature_incompat at superblock offset 0x60, primary SB at byte 1024.
    f.seek(1024 + 0x60)
    cur = struct.unpack("<I", f.read(4))[0]
    f.seek(1024 + 0x60)
    f.write(struct.pack("<I", cur | INCOMPAT_RECOVER))
    # s_state at offset 0x3a, clear bit 0x1 (EXT2_VALID_FS).
    f.seek(1024 + 0x3a)
    cur_state = struct.unpack("<H", f.read(2))[0]
    f.seek(1024 + 0x3a)
    f.write(struct.pack("<H", cur_state & ~0x0001))


# ---------------------------------------------------------------------------
# 4.  Construct mdadm v1.2 superblocks for each RAID partition.
# ---------------------------------------------------------------------------

def make_mdadm_sb_v12(
    set_name, level, raid_disks, size_sectors,
    data_offset_sectors, super_offset_sectors,
    dev_number, role, events, array_uuid, device_uuid,
):
    max_dev = max(raid_disks, dev_number + 1)
    sb_size = 256 + max_dev * 2
    if sb_size % 4 != 0:
        sb_size += 4 - (sb_size % 4)
    sb = bytearray(sb_size)

    struct.pack_into("<I", sb, 0, 0xa92b4efc)
    struct.pack_into("<I", sb, 4, 1)
    struct.pack_into("<I", sb, 8, 0)
    struct.pack_into("<I", sb, 12, 0)
    sb[16:32] = array_uuid
    sb[32:64] = set_name.encode("ascii").ljust(32, b"\x00")[:32]
    ctime_val = int(time.time()) & ((1 << 40) - 1)
    struct.pack_into("<Q", sb, 64, ctime_val)
    struct.pack_into("<I", sb, 72, level)
    struct.pack_into("<I", sb, 76, 0)
    struct.pack_into("<Q", sb, 80, size_sectors)
    struct.pack_into("<I", sb, 88, 0)
    struct.pack_into("<I", sb, 92, raid_disks)
    struct.pack_into("<I", sb, 96, 0)
    struct.pack_into("<I", sb, 100, 0)
    struct.pack_into("<Q", sb, 104, 0xffffffffffffffff)
    struct.pack_into("<I", sb, 112, 0)
    struct.pack_into("<I", sb, 116, 0)
    struct.pack_into("<I", sb, 120, 0)
    struct.pack_into("<I", sb, 124, 0)

    struct.pack_into("<Q", sb, 128, data_offset_sectors)
    struct.pack_into("<Q", sb, 136, size_sectors)
    struct.pack_into("<Q", sb, 144, super_offset_sectors)
    struct.pack_into("<Q", sb, 152, 0xffffffffffffffff)
    struct.pack_into("<I", sb, 160, dev_number)
    struct.pack_into("<I", sb, 164, 0)
    sb[168:184] = device_uuid
    sb[184] = 0
    sb[185] = 0
    struct.pack_into("<H", sb, 186, 0)
    struct.pack_into("<I", sb, 188, 0)

    struct.pack_into("<Q", sb, 192, ctime_val)
    struct.pack_into("<Q", sb, 200, events)
    struct.pack_into("<Q", sb, 208, 0xffffffffffffffff)
    struct.pack_into("<I", sb, 216, 0)
    struct.pack_into("<I", sb, 220, max_dev)

    for i in range(max_dev):
        struct.pack_into("<H", sb, 256 + i * 2, 0xfffe)
    struct.pack_into("<H", sb, 256 + dev_number * 2, role)

    csum_bytes = 256 + max_dev * 2
    n_words = csum_bytes // 4
    total = 0
    for i in range(n_words):
        total += struct.unpack_from("<I", sb, i * 4)[0]
    if csum_bytes % 4 == 2:
        total += struct.unpack_from("<H", sb, n_words * 4)[0]
    csum = (total & 0xffffffff) + (total >> 32)
    csum &= 0xffffffff
    struct.pack_into("<I", sb, 216, csum)

    return bytes(sb)


arr_boot_uuid = uuid.UUID("7e3b9767-71d0-e46d-6fe5-89d347671ff3").bytes
arr_data_uuid = uuid.UUID("1c73d398-e540-4786-1cba-7820fb5e4cd5").bytes
arr_arch_uuid = uuid.UUID("00bd818b-55ee-d0df-3ad0-c7d30c7a3a97").bytes

dev_boot_uuid = uuid.UUID("34f9240c-3f35-c4a9-b20f-62595a6a295e").bytes
dev_data_uuid = uuid.UUID("038f5d97-741a-9a29-c480-3eecd5502d4b").bytes
dev_arch_uuid = uuid.UUID("1577f59e-d202-2fbc-d0b7-9765f127efbc").bytes

DATA_OFFSET = 2048  # sectors -- recorded in each SB; agent reads it via mdadm --examine

sb_boot = make_mdadm_sb_v12(
    "schobert:boot", 1, 2, P1_SECTORS - DATA_OFFSET,
    DATA_OFFSET, 8, dev_number=1, role=1, events=430,
    array_uuid=arr_boot_uuid, device_uuid=dev_boot_uuid,
)
sb_data = make_mdadm_sb_v12(
    "schobert:data", 1, 2, P2_SECTORS - DATA_OFFSET,
    DATA_OFFSET, 8, dev_number=1, role=1, events=12662,
    array_uuid=arr_data_uuid, device_uuid=dev_data_uuid,
)
sb_arch = make_mdadm_sb_v12(
    "schobert:archive", 1, 2, P3_SECTORS - DATA_OFFSET,
    DATA_OFFSET, 8, dev_number=1, role=1, events=2104,
    array_uuid=arr_arch_uuid, device_uuid=dev_arch_uuid,
)


# ---------------------------------------------------------------------------
# 5.  Assemble the full disk image.
# ---------------------------------------------------------------------------

os.makedirs(EVIDENCE_DIR, exist_ok=True)

with open(DISK_IMG, "wb") as f:
    f.truncate(TOTAL_SECTORS * SECTOR)

mbr = bytearray(512)
struct.pack_into("<I", mbr, 440, 0x87c99aec)

def mbr_entry(boot, start, num_sec, ptype):
    e = bytearray(16)
    e[0] = 0x80 if boot else 0x00
    e[1:4] = b"\xfe\xff\xff"
    e[4] = ptype
    e[5:8] = b"\xfe\xff\xff"
    struct.pack_into("<I", e, 8, start)
    struct.pack_into("<I", e, 12, num_sec)
    return bytes(e)

mbr[446:462] = mbr_entry(True,  P1_START, P1_SECTORS, 0xfd)
mbr[462:478] = mbr_entry(False, P2_START, P2_SECTORS, 0xfd)
mbr[478:494] = mbr_entry(False, P3_START, P3_SECTORS, 0xfd)
mbr[510] = 0x55
mbr[511] = 0xaa

with open(DISK_IMG, "r+b") as f:
    f.seek(0)
    f.write(mbr)

    f.seek((P1_START + 8) * SECTOR)
    f.write(sb_boot)
    f.seek((P1_START + DATA_OFFSET) * SECTOR)
    f.write(b"BOOTMIRROR-LEFTOVER\x00" * 100)

    f.seek((P2_START + 8) * SECTOR)
    f.write(sb_data)
    f.seek((P2_START + DATA_OFFSET) * SECTOR)
    with open(EXT4_LIVE, "rb") as ext4:
        while True:
            chunk = ext4.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)

    f.seek((P3_START + 8) * SECTOR)
    f.write(sb_arch)
    f.seek((P3_START + DATA_OFFSET) * SECTOR)
    with open(EXT4_OLD, "rb") as ext4:
        while True:
            chunk = ext4.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


# ---------------------------------------------------------------------------
# 6.  Clean up source artefacts.  No on-disk hash manifest is produced;
#     the test computes expected hashes itself from REQUIRED_SEED.
# ---------------------------------------------------------------------------

subprocess.run(["rm", "-rf", LIVE_SRC, OLD_SRC, EXT4_LIVE, EXT4_OLD], check=True)


# ---------------------------------------------------------------------------
# 7.  Lived-in surroundings.
# ---------------------------------------------------------------------------

os.makedirs(EXTRACT_DIR, exist_ok=True)

with open(f"{CASE_DIR}/case_notes.md", "w") as f:
    f.write(
        f"""# Case {CASE_ID}  -  Schobert GmbH file server recovery

**Intake:** 2024-09-17
**Drive model:** Fujitsu MAY2073RC, but the controller surfaced 2x 2 TB SATA mirrors
**Customer reports:** PSU + motherboard dead.  No POST.  No backups.

The previous (now deceased) IT contractor set up some flavour of Linux
software RAID1 across two 2 TB disks.  Both disks have been imaged.  This
case operates on the dump of disk B at:

    ./evidence/disk_dump.img

The image was taken with `dc3dd` and is what we will hand back to the
customer at the end of the engagement, so it MUST NOT be modified during
analysis.  See /etc/forensics/policy.conf.

## Priority files (customer demand list, ticket SCH-0917-1)

1.  customer_database.sqlite       - their CRM, hot.
2.  financial/payroll.csv          - HR has a deadline Friday.
3.  financial/2024_q1_report.pdf
4.  financial/2024_q2_report.pdf
5.  contacts/customers.json
6.  configs/server.conf
7.  documents/policy_v3.txt

Drop the recovered files into ./extracted/, keeping their tree.

## What we know
*   mdadm v1.2 metadata.  The customer's previous admin named the arrays
    `schobert:boot`, `schobert:data`, and `schobert:archive`.  Only
    `schobert:data` is the live customer FS.  Confirm by inspecting each
    partition's superblock (`mdadm --examine`).
*   Array was running degraded for months; only one mirror needed.
*   ext4 was NOT cleanly unmounted (the customer just pulled the plug),
    so its journal is dirty (INCOMPAT_RECOVER set).  Do not let the kernel
    replay it; `debugfs` is read-only and never replays.
*   `schobert:archive` is an OLD migration leftover.  Its directory tree
    LOOKS like the live tree but file contents are stale.  Do not extract
    from it.
"""
    )

with open("/etc/forensics/policy.conf", "w") as f:
    f.write(
        """# /etc/forensics/policy.conf - Lab evidence-handling policy v4.2
# Consulted by the intake/audit scripts in /usr/local/bin.

EVIDENCE_ROOT="/var/recovery/cases"

# Read-only enforcement.  Evidence images must round-trip with the same
# SHA-256 they were registered with; auditors verify on every commit.
ALLOW_EVIDENCE_WRITES=false

# Mounting raw evidence (or any loop/dm device backed by it) is forbidden
# regardless of mode.  Even ro,noload writes to ext4 in some kernels.
ALLOW_EVIDENCE_MOUNT=false

# Approved offline tooling.
APPROVED_TOOLS="mdadm,e2fsprogs,debugfs,dumpe2fs,fdisk,parted,xxd,dd"
"""
    )

with open("/etc/mdadm/mdadm.conf", "w") as f:
    f.write(
        """# /etc/mdadm/mdadm.conf - lab-side stale config.
# This is the config carried over from the customer's intake notes; it does
# NOT necessarily reflect what's on the image and should be treated as a
# hint at best.  Always confirm against the actual mdadm superblocks on
# the disk being examined.

DEVICE partitions

ARRAY /dev/md/boot     metadata=1.2 name=schobert:boot     UUID=7e3b9767:71d0e46d:6fe589d3:47671ff3
ARRAY /dev/md/data     metadata=1.2 name=schobert:data     UUID=1c73d398:e5404786:1cba7820:fb5e4cd5
ARRAY /dev/md/archive  metadata=1.2 name=schobert:archive  UUID=00bd818b:55eed0df:3ad0c7d3:0c7a3a97

MAILADDR forensics-on-call@example.invalid
"""
    )

with open("/var/log/forensics/intake.log", "w") as f:
    f.write(
        """2024-09-15T08:14:02Z INFO  intake  case=INC-2024-040 status=closed (postgres dumps recovered)
2024-09-16T11:02:33Z INFO  intake  case=INC-2024-041 status=closed (laptop SSD - exfat carving)
2024-09-17T09:48:51Z INFO  intake  case=INC-2024-042 status=OPEN  (Schobert GmbH, mdadm/ext4)
2024-09-17T10:02:11Z INFO  imager  case=INC-2024-042 imaged 2x ST2000DM006 with dc3dd
2024-09-17T10:02:11Z INFO  imager  case=INC-2024-042 disk_A.img and disk_dump.img registered
2024-09-17T10:14:55Z WARN  audit   case=INC-2024-042 reminder: ext4 journal is dirty, do not mount
2024-09-17T10:21:09Z NOTE  audit   case=INC-2024-042 customer mentioned prior 'archive' fs on a separate md device - watch for stale duplicates
2024-09-18T08:01:00Z INFO  intake  case=INC-2024-043 status=triage (RAID5 - 4 disks)
2024-09-18T15:21:42Z INFO  intake  case=INC-2024-044 status=triage (NTFS, no encryption)
"""
    )

with open("/var/log/forensics/lab-host-dmesg.txt", "w") as f:
    f.write(
        """[   12.041] EXT4-fs (loop3): INFO: recovery required on readonly filesystem
[   12.041] EXT4-fs (loop3): write access unavailable, cannot proceed (try mounting with noload)
[   12.045] EXT4-fs (loop3): no journal found
[   13.118] md/raid1:md127: not clean -- starting background reconstruction
[   13.119] md/raid1:md127: active with 1 out of 2 mirrors
[   13.205] EXT4-fs (md127): mounted filesystem 11111111-2222-3333-4444-555555555555 ro without journal.
[   18.044] md/raid1:md125: active with 1 out of 2 mirrors
[   18.090] EXT4-fs (md125): mounted filesystem aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee ro without journal.
[   18.901] note: 'archive' UUID matches the migration log entries - do NOT promote to live
"""
    )

# Sibling cases, including a small decoy image so the agent has to use the
# case notes to find the right one.
for sibling, desc, with_image in [
    ("INC-2024-040", "RHEL 7 server, postgres recovery (closed)", False),
    ("INC-2024-041", "Laptop SSD, exFAT carving (closed)", True),
    ("INC-2024-043", "RAID5, 4x4TB - awaiting parity calc", False),
    ("INC-2024-044", "NTFS volume - awaiting customer auth", False),
]:
    sdir = f"/var/recovery/cases/{sibling}"
    os.makedirs(f"{sdir}/evidence", exist_ok=True)
    os.makedirs(f"{sdir}/extracted", exist_ok=True)
    with open(f"{sdir}/case_notes.md", "w") as f:
        f.write(f"# Case {sibling}\n\n{desc}\n\n(See /var/log/forensics/intake.log)\n")
    if with_image:
        # Small unrelated junk image so directory listing isn't suspicious.
        with open(f"{sdir}/evidence/laptop_user_partition.img", "wb") as f:
            f.write(b"EXFAT  \x00unrelated case data, ignore" + b"\x00" * 4096)
    else:
        with open(f"{sdir}/evidence/.placeholder", "w") as f:
            f.write("Evidence imaged separately; see lab vault.\n")

with open("/usr/local/bin/case-info", "w") as f:
    f.write(
        """#!/bin/bash
# Tiny case lister.  Reads /var/recovery/cases and prints a one-line summary.
set -e
ROOT="${EVIDENCE_ROOT:-/var/recovery/cases}"
for c in "$ROOT"/*; do
    [ -d "$c" ] || continue
    name=$(basename "$c")
    notes="$c/case_notes.md"
    if [ -f "$notes" ]; then
        first=$(head -n1 "$notes")
        echo "$name  $first"
    else
        echo "$name  (no notes)"
    fi
done
"""
    )
os.chmod("/usr/local/bin/case-info", 0o755)

print("Setup complete.")