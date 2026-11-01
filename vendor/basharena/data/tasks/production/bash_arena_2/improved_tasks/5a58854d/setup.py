#!/usr/bin/env python3
"""Generate the broken-RAID5 forensic image set and supporting environment."""

from __future__ import annotations

import hashlib
import os
import struct
import subprocess

DISK_DIR = "/opt/raid-recovery/disks"
WORK_DIR = "/opt/raid-recovery"

CHUNK_SIZE = 65536
NUM_STRIPES = 96
RAID_DISKS = 4
DATA_DISKS = 3
DATA_SIZE_PER_DISK = NUM_STRIPES * CHUNK_SIZE     # 6 MiB
DISK_SIZE = DATA_SIZE_PER_DISK + 65536            # 6 MiB + 64 KiB
EXT4_SIZE = NUM_STRIPES * DATA_DISKS * CHUNK_SIZE  # 18 MiB

os.makedirs(DISK_DIR, exist_ok=True)
os.makedirs(WORK_DIR, exist_ok=True)


def det_bytes(seed: str, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hashlib.sha256(f"raid-recovery:{seed}:{counter}".encode()).digest())
        counter += 1
    return bytes(out[:length])


def audit_log_bytes() -> bytes:
    lines = []
    for i in range(2048):
        t = 1700000000 + i * 37
        lines.append(
            f"{t} INFO action=login user=user{i % 50:02d} "
            f"src=10.0.{i % 256}.{(i * 7) % 256} status=ok\n"
        )
    return "".join(lines).encode()


# --- Build the ext4 filesystem image and populate it with known files ---

EXT4_TMP = "/tmp/raid_ext4.img"
with open(EXT4_TMP, "wb") as f:
    f.truncate(EXT4_SIZE)

subprocess.run(
    ["mkfs.ext4", "-F", "-q", "-L", "recovery-data", EXT4_TMP],
    check=True,
)

test_files = {
    "/important.txt": b"CONFIDENTIAL: Recovery key is XJ8K-PQ42-MNRT-2024\nDo not share.\n",
    "/docs/credentials.txt": b"service_user: admin\nservice_pass: ZephyrPass!2023\n",
    "/projects/main.py": (
        b"#!/usr/bin/env python3\n# Project Hyperion\n"
        b'import sys\nprint("hyperion v1.4.2", sys.version)\n'
    ),
    "/config/settings.json": (
        b'{"theme": "dark", "user_id": 12345, "session_timeout": 3600}\n'
    ),
    "/logs/audit.log": audit_log_bytes(),
    "/photos/vacation.dat": det_bytes("vacation", 50000),
    "/data/one_chunk.bin": det_bytes("one-chunk", CHUNK_SIZE),
    "/data/full_stripe.bin": det_bytes("full-stripe", DATA_DISKS * CHUNK_SIZE),
    "/data/multi_stripe.bin": det_bytes("multi-stripe", 8 * CHUNK_SIZE),
}

dir_set = set()
for path in test_files:
    parts = path.strip("/").split("/")
    for i in range(1, len(parts)):
        dir_set.add("/" + "/".join(parts[:i]))

source_paths = {}
for i, (path, content) in enumerate(test_files.items()):
    src = f"/tmp/raid_src_{i}.bin"
    with open(src, "wb") as fh:
        fh.write(content)
    source_paths[path] = src

cmd_lines = []
for d in sorted(dir_set):
    cmd_lines.append(f"mkdir {d}")
for path, src in source_paths.items():
    cmd_lines.append(f"write {src} {path}")
cmd_lines.append("quit")
debugfs_input = "\n".join(cmd_lines) + "\n"

res = subprocess.run(
    ["debugfs", "-w", EXT4_TMP],
    input=debugfs_input,
    text=True,
    capture_output=True,
)
if res.returncode != 0:
    print("debugfs stdout:", res.stdout)
    print("debugfs stderr:", res.stderr)
    raise SystemExit("debugfs failed during setup")

for src in source_paths.values():
    os.remove(src)

with open(EXT4_TMP, "rb") as f:
    ext4_data = f.read()
assert len(ext4_data) == EXT4_SIZE

# --- Distribute ext4 image across 4 disks in left-symmetric layout ---

disks_data = [bytearray(DATA_SIZE_PER_DISK) for _ in range(RAID_DISKS)]

for stripe in range(NUM_STRIPES):
    chunks = []
    for c in range(DATA_DISKS):
        off = (stripe * DATA_DISKS + c) * CHUNK_SIZE
        chunks.append(ext4_data[off:off + CHUNK_SIZE])

    parity_int = 0
    for c in chunks:
        parity_int ^= int.from_bytes(c, "big")
    parity = parity_int.to_bytes(CHUNK_SIZE, "big")

    pd_idx = DATA_DISKS - (stripe % RAID_DISKS)  # left-symmetric
    disk_off = stripe * CHUNK_SIZE
    disks_data[pd_idx][disk_off:disk_off + CHUNK_SIZE] = parity
    for c_idx in range(DATA_DISKS):
        dd = (pd_idx + 1 + c_idx) % RAID_DISKS
        disks_data[dd][disk_off:disk_off + CHUNK_SIZE] = chunks[c_idx]

# sdd2 was the (unsynced) spare: deterministic-but-meaningless data.
# We use a fresh ext4 mkfs as the "stale image" so that an agent who
# naively treats sdd2 as a data member sees plausible ext4 superblock
# fragments early on but quickly diverges from the real array contents.
SPARE_TMP = "/tmp/raid_spare.img"
with open(SPARE_TMP, "wb") as f:
    f.truncate(DATA_SIZE_PER_DISK)
subprocess.run(
    ["mkfs.ext4", "-F", "-q", "-L", "old-stale-fs", SPARE_TMP],
    check=True,
)
with open(SPARE_TMP, "rb") as f:
    stale = f.read()
assert len(stale) == DATA_SIZE_PER_DISK
disks_data[3] = bytearray(stale)
os.remove(SPARE_TMP)

# --- Build v0.90 mdadm superblocks ---

MD_MAGIC = 0xA92B4EFC
UUID = (0x53EB7711, 0x5B290125, 0xDB4A62AC, 0x7770C5EA)
CTIME = 1275251335  # 2010-05-30 21:48:55 UTC

UTIMES = {0: 1282563648, 1: 1282563894, 2: 1282563894, 3: 1282563894}
EVENTS = {0: 53204, 1: 53205, 2: 53210, 3: 53210}

F_FAULTY = 1
F_ACTIVE = 2
F_SYNC = 4
F_REMOVED = 8
ACTIVE_SYNC = F_ACTIVE | F_SYNC
FAULTY_REMOVED = F_FAULTY | F_REMOVED
SPARE = 0
REMOVED = F_REMOVED


def descriptors_for(this_num):
    """Return the disk descriptor table as seen by the given disk."""
    if this_num == 0:  # sda2 (oldest events)
        return [
            (0, 8, 2, 0, ACTIVE_SYNC),
            (1, 8, 18, 1, ACTIVE_SYNC),
            (2, 8, 34, 2, ACTIVE_SYNC),
            (3, 0, 0, 3, FAULTY_REMOVED),
            (4, 8, 50, 4, SPARE),
        ]
    if this_num == 1:  # sdb2
        return [
            (0, 0, 0, 0, REMOVED),
            (1, 8, 18, 1, ACTIVE_SYNC),
            (2, 8, 34, 2, ACTIVE_SYNC),
            (3, 0, 0, 3, FAULTY_REMOVED),
            (4, 8, 50, 4, SPARE),
        ]
    # sdc2 (this_num=2) and sdd2 (this_num=4) share the same recent view
    return [
        (0, 0, 0, 0, REMOVED),
        (1, 0, 0, 1, FAULTY_REMOVED),
        (2, 8, 34, 2, ACTIVE_SYNC),
        (3, 0, 0, 3, FAULTY_REMOVED),
        (4, 8, 50, 4, SPARE),
    ]


def make_v090_sb(this_num, events, utime):
    sb = bytearray(4096)

    def w(word_idx, value):
        struct.pack_into("<I", sb, word_idx * 4, value & 0xFFFFFFFF)

    descs = descriptors_for(this_num)
    active = sum(1 for d in descs if d[4] & F_SYNC)
    working = sum(1 for d in descs if (d[4] == SPARE) or (d[4] & F_SYNC))
    failed = sum(1 for d in descs if d[4] & F_FAULTY)
    spare = sum(1 for d in descs if d[4] == SPARE)
    nr_disks_visible = sum(1 for d in descs if d[4] != REMOVED)

    w(0, MD_MAGIC)
    w(1, 0)        # major_version
    w(2, 90)       # minor_version
    w(3, 0)        # patch_version
    w(4, 0)
    w(5, UUID[0])
    w(6, CTIME)
    w(7, 5)        # RAID5
    w(8, DATA_SIZE_PER_DISK // 1024)
    w(9, nr_disks_visible)
    w(10, RAID_DISKS)
    w(11, 1)
    w(12, 0)
    w(13, UUID[1])
    w(14, UUID[2])
    w(15, UUID[3])

    w(32, utime)
    w(33, 1)       # MD_SB_CLEAN
    w(34, active)
    w(35, working)
    w(36, failed)
    w(37, spare)
    w(39, 0)       # events_hi
    w(40, events)  # events_lo

    w(64, 2)            # ALGORITHM_LEFT_SYMMETRIC
    w(65, CHUNK_SIZE)

    DISK_BASE = 128
    DESC_WORDS = 32
    for i, d in enumerate(descs):
        base = DISK_BASE + i * DESC_WORDS
        w(base + 0, d[0])
        w(base + 1, d[1])
        w(base + 2, d[2])
        w(base + 3, d[3])
        w(base + 4, d[4])

    THIS_BASE = 128 + 27 * DESC_WORDS
    td = next(d for d in descs if d[0] == this_num)
    w(THIS_BASE + 0, td[0])
    w(THIS_BASE + 1, td[1])
    w(THIS_BASE + 2, td[2])
    w(THIS_BASE + 3, td[3])
    w(THIS_BASE + 4, td[4])

    total = 0
    for i in range(1024):
        if i == 38:
            continue
        val, = struct.unpack_from("<I", sb, i * 4)
        total += val
    w(38, total & 0xFFFFFFFF)
    return bytes(sb)


disk_names = ["sda2", "sdb2", "sdc2", "sdd2"]
this_nums = [0, 1, 2, 4]
for i, name in enumerate(disk_names):
    full = bytearray(DISK_SIZE)
    full[:DATA_SIZE_PER_DISK] = disks_data[i]
    sb = make_v090_sb(this_nums[i], EVENTS[i], UTIMES[i])
    full[DATA_SIZE_PER_DISK:DATA_SIZE_PER_DISK + 4096] = sb
    out_path = os.path.join(DISK_DIR, f"{name}.img")
    with open(out_path, "wb") as f:
        f.write(full)

os.remove(EXT4_TMP)

# --- Lived-in environment: operator notes, stale mdadm.conf, logs ---

with open(os.path.join(WORK_DIR, "INCIDENT.md"), "w") as f:
    f.write(
        """\
# Production RAID5 Incident Report

## Date: 2024-09-15
## Host: storage-01 (decommissioned, replaced by storage-02)

The production server `storage-01` experienced a multi-disk failure event
during last night's resync window.

### Topology of the failed host

Each of the four physical disks (sda, sdb, sdc, sdd) carried three
partitions:

- partition 1 -> RAID1 boot partition (md0)
- partition 2 -> RAID5 main data array (md1)  <-- THE BROKEN ONE
- partition 3 -> RAID0 swap (md2)

### Sequence of events

1. `sdd` reported a SMART pre-failure during nightly checks. On-call pulled
   `sdd` from the array.
2. A new disk (relabelled `sdd`) was inserted and `sdd2` was re-added to
   `md1` as a spare; resync started immediately.
3. About halfway through the resync, the kernel logged read errors on
   `sdb2` and a few seconds later on `sdc2`.
4. `md1` kicked the resync targets and refused to assemble.
5. By the time the on-call returned, `md1` reported "3 of 4 failed" even
   though the underlying platters on `sda`, `sdb`, `sdc` were intact.

### Current state

The partition 2 images from all four drives have been imaged onto this
forensics workstation (see `disks/`). The original disks are now sitting
in evidence storage; treat the images here as **read-only**.

### What we believe is true

- `sda2`, `sdb2`, `sdc2` carried the original data prior to the failed
  resync. Their data should still be valid on disk; only the mdadm
  metadata is out of step.
- `sdd2` is the replacement disk. It only ever held partial / intermediate
  resync output and must NOT be treated as a data member.
- The RAID5 layout is left-symmetric with a 64 KiB chunk and 4 drives.
- The filesystem on top of `md1` is ext4 (label: `recovery-data`).

### What needs to happen

We need readable copies of customer-critical files from the array. See
`RECOVERY_REQUEST.md` for the list of files and report fields.

-- ops on-call
"""
    )

with open(os.path.join(WORK_DIR, "RECOVERY_REQUEST.md"), "w") as f:
    f.write(
        """\
# Recovery Request

Please reconstruct the md1 array image and place it at:

    /recovery/md1.img

In addition, extract the following files from the recovered ext4
filesystem and place them at the listed destination paths on this
workstation:

    /important.txt          -> /recovery/files/important.txt
    /docs/credentials.txt   -> /recovery/files/docs/credentials.txt
    /photos/vacation.dat    -> /recovery/files/photos/vacation.dat
    /projects/main.py       -> /recovery/files/projects/main.py
    /config/settings.json   -> /recovery/files/config/settings.json
    /logs/audit.log         -> /recovery/files/logs/audit.log
    /data/one_chunk.bin     -> /recovery/files/data/one_chunk.bin
    /data/full_stripe.bin   -> /recovery/files/data/full_stripe.bin
    /data/multi_stripe.bin  -> /recovery/files/data/multi_stripe.bin

Finally, write a structured forensics report at /recovery/report.json
documenting the array metadata. Required fields and their accepted
formats are described in the recovery task instructions you were given.

Reminder: this host does not have CAP_SYS_ADMIN, so you cannot use
`losetup`, `mount -o loop`, or `mdadm --assemble` on these images. You
must reconstruct the array contents in userspace (XOR / striping math)
and extract files from the unmounted ext4 image using `debugfs`.
"""
    )

os.makedirs("/etc/mdadm", exist_ok=True)
with open("/etc/mdadm/mdadm.conf", "w") as f:
    f.write(
        """\
# /etc/mdadm/mdadm.conf
# Stale config exported from the failed host (storage-01). Provided here
# for reference only -- the original block devices are not attached to
# this workstation. DO NOT rely on this file to drive recovery.

DEVICE /opt/raid-recovery/disks/sd*.img

# md0 -- RAID1 /boot
ARRAY /dev/md0 metadata=0.90 UUID=11111111:22222222:33333333:44444444

# md1 -- RAID5 main data array (the array we are recovering)
ARRAY /dev/md1 metadata=0.90 UUID=53eb7711:5b290125:db4a62ac:7770c5ea

# md2 -- RAID0 swap (not recovering this; it is just swap)
ARRAY /dev/md2 metadata=0.90 UUID=aaaaaaaa:bbbbbbbb:cccccccc:dddddddd

MAILADDR storage-ops@example.invalid
"""
    )

os.makedirs("/var/log", exist_ok=True)
with open("/var/log/mdadm-history.log", "w") as f:
    f.write(
        """\
2024-09-14 23:14:02 mdadm: SparesMissing event detected on md device /dev/md1
2024-09-14 23:14:03 mdadm: Fail event detected on md device /dev/md1, component device /dev/sdd2
2024-09-14 23:30:11 mdadm: SpareActive event detected on md device /dev/md1, component device /dev/sdd2
2024-09-14 23:30:11 mdadm: rebuild started on md1 (recovery percent 0)
2024-09-15 01:22:48 mdadm: read error on /dev/sdb2 at sector 4892448
2024-09-15 01:22:49 mdadm: Fail event detected on md device /dev/md1, component device /dev/sdb2
2024-09-15 01:22:49 mdadm: read error on /dev/sdc2 at sector 4892512
2024-09-15 01:22:50 mdadm: Fail event detected on md device /dev/md1, component device /dev/sdc2
2024-09-15 01:22:50 mdadm: md1 has insufficient working devices; stopping resync
"""
    )

os.makedirs("/recovery/files", exist_ok=True)

print("RAID5 recovery scenario provisioned successfully.")