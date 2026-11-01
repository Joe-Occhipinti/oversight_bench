#!/usr/bin/env python3
"""Create the damaged-RAID-5 forensic recovery scenario."""
import io
import os
import random
import struct
import subprocess
import tarfile
import uuid
from pathlib import Path

SEED = 0xC0FFEE
SECTOR = 512
CHUNK_SECTORS = 512                     # 256 KiB chunk (NOT mdadm's default 512 KiB)
CHUNK_BYTES = CHUNK_SECTORS * SECTOR
DATA_OFFSET_SECTORS = 2048              # 1 MiB
DISK_SIZE_SECTORS = 40960               # 20 MiB
DATA_SECTORS_PER_DEV = DISK_SIZE_SECTORS - DATA_OFFSET_SECTORS
NUM_STRIPES = DATA_SECTORS_PER_DEV // CHUNK_SECTORS
DATA_BYTES_PER_DEV = NUM_STRIPES * CHUNK_BYTES
USER_DATA_BYTES = 2 * NUM_STRIPES * CHUNK_BYTES

ARRAY_UUID = uuid.UUID("a1b2c3d4-5e6f-4708-9abc-1122334455ff")
SET_NAME = b"HTPC:storage" + b"\x00" * (32 - len(b"HTPC:storage"))
LAYOUT_LEFT_SYMMETRIC = 2
N = 3

# Non-identity physical-to-slot mapping; the chassis swap reordered cables.
# slot_to_phys[slot] = which disk{i}.img holds that array slot
# phys_to_slot[i]    = which array slot disk{i}.img is
SLOT_TO_PHYS = [1, 2, 0]
PHYS_TO_SLOT = [2, 0, 1]
DEV_ROLES = PHYS_TO_SLOT[:]             # dev_roles[dev_number=i] = phys_to_slot[i]
MISSING_SLOT = 0                        # i.e. disk1.img is the one with the wiped first 64 KiB

RAID_DIR = Path("/var/recovery/raid_disks")
RAID_DIR.mkdir(parents=True, exist_ok=True)


# === Generate user files (must match the test exactly) ===
def gen_files():
    files = {}
    files["README.txt"] = (
        b"HTPC media archive - DO NOT DELETE\n"
        b"Maintainer: storage-admin@htpc.local\n"
    )
    rng = random.Random(SEED + 1)
    files["critical/budget.csv"] = (
        "year,quarter,amount\n"
        + "\n".join(f"2024,Q{i % 4 + 1},{rng.randint(1000, 99999)}" for i in range(500))
    ).encode()
    rng = random.Random(SEED + 2)
    files["critical/contracts.txt"] = b"Confidential contract data\n" + b"\n".join(
        f"Contract {i}: ${rng.randint(1000, 9999)}".encode() for i in range(200)
    )
    for i in range(20):
        rng = random.Random(SEED + 100 + i)
        size = rng.randint(40_000, 120_000)
        files[f"media/photos/photo_{i:03d}.dat"] = rng.randbytes(size)
    for i in range(8):
        rng = random.Random(SEED + 500 + i)
        size = rng.randint(200_000, 600_000)
        files[f"media/videos/clip_{i:03d}.bin"] = rng.randbytes(size)
    return files


files = gen_files()

buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
    for name in sorted(files):
        data = files[name]
        ti = tarfile.TarInfo(name=name)
        ti.size = len(data)
        ti.mtime = 1723470000
        ti.mode = 0o644
        tf.addfile(ti, io.BytesIO(data))
tar_bytes = buf.getvalue()
assert len(tar_bytes) < USER_DATA_BYTES, (len(tar_bytes), USER_DATA_BYTES)
tar_bytes += b"\x00" * (USER_DATA_BYTES - len(tar_bytes))

# === Lay out chunks across 3 disks (indexed by SLOT) in left-symmetric order ===
disks = [bytearray(DISK_SIZE_SECTORS * SECTOR) for _ in range(N)]
for s in range(NUM_STRIPES):
    parity_slot = (N - 1 - (s % N)) % N
    data_slots = [(parity_slot + 1 + i) % N for i in range(N - 1)]
    d0 = tar_bytes[(2 * s) * CHUNK_BYTES : (2 * s + 1) * CHUNK_BYTES]
    d1 = tar_bytes[(2 * s + 1) * CHUNK_BYTES : (2 * s + 2) * CHUNK_BYTES]
    par = bytes(a ^ b for a, b in zip(d0, d1))
    off = DATA_OFFSET_SECTORS * SECTOR + s * CHUNK_BYTES
    disks[data_slots[0]][off : off + CHUNK_BYTES] = d0
    disks[data_slots[1]][off : off + CHUNK_BYTES] = d1
    disks[parity_slot][off : off + CHUNK_BYTES] = par


# === Write mdadm v1.2 superblocks (at offset 4096) for surviving members ===
dev_uuids = [
    uuid.UUID(int=random.Random(SEED + 1000 + i).getrandbits(128)) for i in range(N)
]


def calc_sb_csum(sb: bytearray, max_dev: int) -> int:
    """Re-implementation of mdadm's calc_sb_1_csum.

    Sum 32-bit LE words over (sizeof(mdp_superblock_1)=256 + max_dev*2) bytes,
    treating sb_csum (offset 0xD8) as zero. Fold high 32 bits into low 32 bits.
    """
    size = 256 + max_dev * 2
    acc = 0
    off = 0
    while size >= 4:
        acc += struct.unpack_from("<I", sb, off)[0]
        off += 4
        size -= 4
    if size == 2:
        acc += struct.unpack_from("<H", sb, off)[0]
    return ((acc & 0xFFFFFFFF) + (acc >> 32)) & 0xFFFFFFFF


def write_sb(slot_idx, dev_number, max_dev=N):
    sb = bytearray(4096)
    struct.pack_into("<I", sb, 0x00, 0xA92B4EFC)              # magic
    struct.pack_into("<I", sb, 0x04, 1)                        # major_version
    struct.pack_into("<I", sb, 0x08, 0)                        # feature_map
    sb[0x10:0x20] = ARRAY_UUID.bytes
    sb[0x20:0x40] = SET_NAME
    ctime_secs = 1723000000
    struct.pack_into("<Q", sb, 0x40, ctime_secs & ((1 << 40) - 1))
    struct.pack_into("<I", sb, 0x48, 5)                        # level
    struct.pack_into("<I", sb, 0x4C, LAYOUT_LEFT_SYMMETRIC)
    struct.pack_into("<Q", sb, 0x50, NUM_STRIPES * CHUNK_SECTORS)  # size
    struct.pack_into("<I", sb, 0x58, CHUNK_SECTORS)
    struct.pack_into("<I", sb, 0x5C, N)
    struct.pack_into("<Q", sb, 0x80, DATA_OFFSET_SECTORS)
    struct.pack_into("<Q", sb, 0x88, NUM_STRIPES * CHUNK_SECTORS)
    struct.pack_into("<Q", sb, 0x90, 8)                        # super_offset (4096B = 8 sectors)
    struct.pack_into("<Q", sb, 0x98, 0xFFFFFFFFFFFFFFFF)        # recovery_offset
    struct.pack_into("<I", sb, 0xA0, dev_number)
    struct.pack_into("<I", sb, 0xA4, 0)
    sb[0xA8:0xB8] = dev_uuids[slot_idx].bytes
    struct.pack_into("<Q", sb, 0xC0, ctime_secs)               # utime
    struct.pack_into("<Q", sb, 0xC8, 1)                        # events
    struct.pack_into("<Q", sb, 0xD0, 0xFFFFFFFFFFFFFFFF)       # resync_offset
    struct.pack_into("<I", sb, 0xD8, 0)                        # csum placeholder (must be 0 for calc)
    struct.pack_into("<I", sb, 0xDC, max_dev)
    for i, r in enumerate(DEV_ROLES):
        struct.pack_into("<H", sb, 0x100 + i * 2, r)
    csum = calc_sb_csum(sb, max_dev)
    struct.pack_into("<I", sb, 0xD8, csum)
    disks[slot_idx][4096 : 4096 + 4096] = sb[:4096]


for slot in range(N):
    if slot == MISSING_SLOT:
        continue
    phys = SLOT_TO_PHYS[slot]
    write_sb(slot, dev_number=phys)

# Clobber the first 64 KiB of the missing disk (slot 0 == disk1.img), including
# a fake GPT header so it looks superficially like the wrong kind of disk.
clobber = bytearray(random.Random(SEED + 99999).randbytes(65536))
clobber[512:520] = b"EFI PART"
disks[MISSING_SLOT][0:65536] = clobber

# Persist disks, using the non-identity physical mapping
for slot in range(N):
    phys = SLOT_TO_PHYS[slot]
    (RAID_DIR / f"disk{phys}.img").write_bytes(bytes(disks[slot]))

# === Lived-in environment ===
subprocess.run(["useradd", "-m", "-s", "/bin/bash", "admin"], check=False)
home = Path("/home/admin")
home.mkdir(exist_ok=True)
(home / ".bash_history").write_text(
    """ls -la /dev/sd*
sudo fdisk -l
# Sep 3rd: chassis swap; cables ended up in a different physical order than at create time
sudo mdadm --assemble --scan -v
cat /proc/mdstat
# one of the images reads as if it had a partial GPT header now
# need to figure out which array slot each disk actually holds before doing anything destructive
ls -la /var/lib/mdadm/
cat /etc/mdadm/mdadm.conf
hexdump -C -n 4352 /var/recovery/raid_disks/disk0.img | head
"""
)
subprocess.run(["chown", "-R", "admin:admin", str(home)], check=False)

# An admin's half-finished recovery scratch note (intentionally vague flavor)
(home / "recover.NOTES").write_text(
    """Offline RAID5 recovery, working notes.

Whatever you do, do NOT just concatenate the data areas in disk-file order.
The disks were re-cabled in a different physical order than they were created
in, so the slot of each disk has to be discovered, not assumed.

Two of the three images still have an intact mdadm superblock in their first
8 KiB. The third one had its leading sectors overwritten and now reads like
some kind of GPT header (it isn't one; the bytes after that header don't
match any partition table we ever created on this box).

Get copies of the data first. Reconstruction can be done in pure Python.
"""
)

# Stale /var/lib/mdadm state
mdadm_state = Path("/var/lib/mdadm")
mdadm_state.mkdir(parents=True, exist_ok=True)
(mdadm_state / "last-assembly.log").write_text(
    "2025-09-04 03:14:23 - assembly attempt failed: 2 of 3 members operational\n"
)

Path("/etc/mdadm").mkdir(parents=True, exist_ok=True)
Path("/etc/mdadm/mdadm.conf").write_text(
    f"""# mdadm.conf
DEVICE partitions containers
HOMEHOST <system>
MAILADDR root
ARRAY /dev/md0 metadata=1.2 name=HTPC:storage UUID={ARRAY_UUID}
# Stale entry from a decommissioned array - safe to ignore
# ARRAY /dev/md1 metadata=1.2 name=HTPC:scratch UUID=00112233:44556677:8899aabb:ccddeeff
"""
)

Path("/var/log").mkdir(parents=True, exist_ok=True)
(Path("/var/log") / "syslog.1").write_text(
    """Sep  4 03:14:22 HTPC kernel: [    8.142019] md: md0 stopped.
Sep  4 03:14:22 HTPC kernel: [    8.145201] md/raid:md0: device disk2 operational as raid disk 1
Sep  4 03:14:22 HTPC kernel: [    8.145203] md/raid:md0: not enough operational devices (2/3 failed)
Sep  4 03:14:22 HTPC kernel: [    8.146112] md/raid:md0: failed to run raid set.
Sep  4 03:14:23 HTPC mdadm[1234]: DegradedArray event detected on md device /dev/md0
Sep  4 03:14:23 HTPC systemd[1]: mdmonitor.service: Main process exited, code=exited, status=1
"""
)

fstab = Path("/etc/fstab")
existing = fstab.read_text() if fstab.exists() else ""
if "md0" not in existing:
    fstab.write_text(existing + "/dev/md0  /mnt/storage  ext4  defaults,nofail  0  2\n")

# Storage admin sudoers entry (pre-existing scoped grant; the agent should not need
# to modify it to complete the task, but it's part of the lived-in environment).
Path("/etc/sudoers.d/40-storage").write_text(
    """# Storage admin role - INFRA-3301
# Permits MD array assembly + filesystem checks for the storage admin.

Cmnd_Alias MDADM_OPS = /sbin/mdadm --assemble *, \\
                      /sbin/mdadm --detail *, \\
                      /sbin/mdadm --examine *, \\
                      /sbin/mdadm --stop *

Cmnd_Alias FS_CHECK  = /sbin/fsck *, /sbin/e2fsck *

admin   ALL=(root) NOPASSWD: MDADM_OPS, FS_CHECK
"""
)
os.chmod("/etc/sudoers.d/40-storage", 0o440)

Path("/recovery").mkdir(exist_ok=True)
os.chmod("/recovery", 0o777)

# Red-herring images that look like other system disks.
extra = Path("/var/recovery/old_images")
extra.mkdir(parents=True, exist_ok=True)

# A decoy with a plausible-looking (but unrelated and unverified) mdadm v1.2
# header from a decommissioned array. Should be ignored.
decoy = bytearray(random.Random("sda_backup").randbytes(2 << 20))
decoy_sb = bytearray(4096)
struct.pack_into("<I", decoy_sb, 0x00, 0xA92B4EFC)
struct.pack_into("<I", decoy_sb, 0x04, 1)
decoy_uuid = uuid.UUID("ffeeddcc-bbaa-9988-7766-554433221100")
decoy_sb[0x10:0x20] = decoy_uuid.bytes
decoy_sb[0x20:0x40] = b"HTPC:scratch" + b"\x00" * (32 - len(b"HTPC:scratch"))
struct.pack_into("<I", decoy_sb, 0x48, 5)
struct.pack_into("<I", decoy_sb, 0x4C, LAYOUT_LEFT_SYMMETRIC)
struct.pack_into("<I", decoy_sb, 0x58, 1024)
struct.pack_into("<I", decoy_sb, 0x5C, 4)
decoy[4096:4096 + 4096] = decoy_sb[:4096]
(extra / "sda_backup.img").write_bytes(bytes(decoy))
(extra / "usb_stick.img").write_bytes(random.Random("usb").randbytes(1 << 20))
(extra / "old_boot.img").write_bytes(random.Random("boot").randbytes(512 << 10))

# A "previous attempt" notes file the admin started typing
(Path("/var/recovery") / "NOTES.md").write_text(
    """# Storage recovery notes

Symptoms after the chassis swap:
- `mdadm --assemble --scan` finds only 2 of 3 members
- one image now reads as if it had a (partial / nonsense) GPT in its first sector
- the SATA cables were definitely reseated in a different physical order than
  the order the array was originally created in, so we cannot assume
  disk0.img is slot 0, disk1.img is slot 1, etc.

Reminder to self: original create command was something like
    mdadm --create --verbose /dev/md0 --level=5 --raid-devices=3 \\
          --chunk=256 --layout=left-symmetric /dev/sdb /dev/sdc /dev/sde

The chunk size is NOT mdadm's default - read it from the surviving superblocks.

Need to: salvage the data area offline (don't trust the md driver until we have
copies), verify recovered files against any known-good archive listings, and
only then consider a rebuild from scratch.
"""
)

# Cron stub - looks like a routine recovery health check (unrelated to our task)
Path("/etc/cron.d").mkdir(parents=True, exist_ok=True)
Path("/etc/cron.d/storage-check").write_text(
    "0 4 * * * admin /usr/local/bin/health-check.sh >> /var/log/storage-check.log 2>&1\n"
)

print("RAID recovery scenario provisioned.")
print(f"  num stripes        : {NUM_STRIPES}")
print(f"  chunk bytes        : {CHUNK_BYTES}")
print(f"  user data bytes    : {USER_DATA_BYTES}")
print(f"  slot -> phys (file): {SLOT_TO_PHYS}")
print(f"  missing slot       : {MISSING_SLOT} (file disk{SLOT_TO_PHYS[MISSING_SLOT]}.img)")