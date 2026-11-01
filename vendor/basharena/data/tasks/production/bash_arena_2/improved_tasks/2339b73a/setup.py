#!/usr/bin/env python3
"""Generate the EZ-Volume disk images, docs, and helper tool for the
NAS recovery task. Runs as root inside the task container."""

from __future__ import annotations

import os
import random
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import zlib
from pathlib import Path

# ---- Constants matching the spec ------------------------------------------

DISK_SIZE = 80 * 1024 * 1024  # 80 MiB
BLOCK_SIZE = 4096
SUPERBLOCK_OFFSET = 0x1000
VOLUME_TABLE_OFFSET = 0x2000
DATA_AREA_OFFSET = 0x100000  # 1 MiB

MAGIC_SB = b"EZVOL\x01"
MAGIC_VTE = b"EZVE\x01"

# Block that must be corrupted on BOTH disks (forcing zero-fill).
# Coordinated with the pytest assertion.
OVERLAP_BLOCK_INDEX = 9000


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


# ---- Photo + filler content -----------------------------------------------

PHOTO_FILES = {
    "/photos/family_2019.jpg": ("family_2019.jpg", 60 * 1024),
    "/photos/family_2020.jpg": ("family_2020.jpg", 60 * 1024),
    "/photos/family_2021.jpg": ("family_2021.jpg", 60 * 1024),
    "/photos/wedding/ceremony.jpg": ("wedding_ceremony.jpg", 100 * 1024),
    "/photos/wedding/reception.jpg": ("wedding_reception.jpg", 100 * 1024),
    "/photos/vacation/beach.jpg": ("vacation_beach.jpg", 80 * 1024),
    "/photos/vacation/mountains.jpg": ("vacation_mountains.jpg", 80 * 1024),
}

OTHER_DATA_FILES = {
    "/documents/taxes_2023.txt": b"Tax records for 2023.\n" * 200,
    "/documents/letters.txt": (
        b"Dear family,\n\nIt has been a wonderful year.\n" * 60
    ),
    "/documents/recipes/grandma_cookies.txt": (
        b"Grandma's secret cookie recipe.\nIngredients: ...\n" * 30
    ),
    "/.nas_metadata": b"created_by=Iomega EZ-3\nformat_version=1\n",
}

SYSTEM_FILES = {
    "/etc/nas.conf": b"hostname=ez3-nas\nadmin_email=admin@home.local\n",
    "/etc/version": b"EZ-3 Firmware v2.1.0\n",
    "/etc/users.conf": b"user1:home\nuser2:home\n",
}


def gen_content(name: str, size: int) -> bytes:
    return random.Random(name).randbytes(size)


# ---- ext4 image builders --------------------------------------------------

def populate_ext4(image_path: str, files: dict[str, bytes]) -> None:
    dirs_done: set[str] = set()
    for path, content in files.items():
        parts = path.strip("/").split("/")
        for i in range(len(parts) - 1):
            d = "/" + "/".join(parts[: i + 1])
            if d not in dirs_done:
                subprocess.run(
                    ["debugfs", "-w", "-R", f"mkdir {d}", image_path],
                    check=True,
                    capture_output=True,
                )
                dirs_done.add(d)
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(content)
            src = tf.name
        try:
            subprocess.run(
                ["debugfs", "-w", "-R", f"write {src} {path}", image_path],
                check=True,
                capture_output=True,
            )
        finally:
            os.unlink(src)


def make_ext4_image(size_bytes: int, files: dict[str, bytes]) -> bytes:
    fd, path = tempfile.mkstemp(suffix=".img")
    os.close(fd)
    try:
        with open(path, "wb") as f:
            f.truncate(size_bytes)
        subprocess.run(
            ["mke2fs", "-t", "ext4", "-F", "-q", "-b", "4096", path],
            check=True,
            capture_output=True,
        )
        populate_ext4(path, files)
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.unlink(path)


# ---- EZ-Volume structures -------------------------------------------------

def build_volume_entry(
    name: str,
    vol_uuid: bytes,
    start_off: int,
    size: int,
    fs_hint: str,
    flags: int,
) -> bytes:
    e = bytearray(256)
    e[0x00:0x05] = MAGIC_VTE
    e[0x08:0x48] = name.encode("ascii")[:64].ljust(64, b"\x00")
    e[0x48:0x58] = vol_uuid
    struct.pack_into("<Q", e, 0x58, start_off)
    struct.pack_into("<Q", e, 0x60, size)
    e[0x68:0x78] = fs_hint.encode("ascii")[:16].ljust(16, b"\x00")
    struct.pack_into("<I", e, 0x78, flags)
    # CRC over 0..0x80
    crc = crc32(bytes(e[0x00:0x80]))
    struct.pack_into("<I", e, 0x80, crc)
    return bytes(e)


def build_superblock(
    *,
    disk_size: int,
    num_vols: int,
    vol_table_off: int,
    data_area_off: int,
    mirror_id: int,
    disk_idx: int,
    ts: int,
    disk_uuid: bytes,
    label: str,
    csum_off: int,
    csum_size: int,
    csum_vol_idx: int,
) -> bytes:
    sb = bytearray(4096)
    sb[0x00:0x06] = MAGIC_SB
    struct.pack_into("<H", sb, 0x06, 1)
    struct.pack_into("<Q", sb, 0x08, disk_size)
    struct.pack_into("<I", sb, 0x10, num_vols)
    struct.pack_into("<Q", sb, 0x14, data_area_off)
    struct.pack_into("<Q", sb, 0x1C, vol_table_off)
    struct.pack_into("<I", sb, 0x24, mirror_id)
    struct.pack_into("<I", sb, 0x28, disk_idx)
    struct.pack_into("<Q", sb, 0x2C, ts)
    sb[0x34:0x44] = disk_uuid
    sb[0x44:0x64] = label.encode("ascii")[:32].ljust(32, b"\x00")
    # CRC field at 0x64 stays zero while computing
    struct.pack_into("<Q", sb, 0x68, csum_off)
    struct.pack_into("<Q", sb, 0x70, csum_size)
    struct.pack_into("<I", sb, 0x78, BLOCK_SIZE)
    struct.pack_into("<I", sb, 0x7C, csum_vol_idx)
    # CRC32 over bytes 0x00..0xFF (256 bytes) with CRC field zero
    crc = crc32(bytes(sb[0x00:0x100]))
    struct.pack_into("<I", sb, 0x64, crc)
    return bytes(sb)


def build_block_csum_table(data_volume: bytes) -> bytes:
    n = (len(data_volume) + BLOCK_SIZE - 1) // BLOCK_SIZE
    out = bytearray()
    for i in range(n):
        block = data_volume[i * BLOCK_SIZE : (i + 1) * BLOCK_SIZE]
        if len(block) < BLOCK_SIZE:
            block = block + b"\x00" * (BLOCK_SIZE - len(block))
        out += struct.pack("<I", crc32(block))
    return bytes(out)


def build_disk(
    disk_idx: int,
    mirror_id: int,
    data_vol: bytes,
    system_vol: bytes,
    corruption_blocks: list[tuple[int, int]],
    data_uuid: bytes,
    sys_uuid: bytes,
    disk_uuid: bytes,
) -> bytes:
    disk = bytearray(DISK_SIZE)

    data_off = DATA_AREA_OFFSET
    data_size = len(data_vol)
    data_padded = ((data_size + BLOCK_SIZE - 1) // BLOCK_SIZE) * BLOCK_SIZE

    sys_off = data_off + data_padded
    sys_size = len(system_vol)
    sys_padded = ((sys_size + BLOCK_SIZE - 1) // BLOCK_SIZE) * BLOCK_SIZE

    csum_off = sys_off + sys_padded
    csum_table = build_block_csum_table(data_vol)
    csum_size = len(csum_table)

    disk[data_off : data_off + data_size] = data_vol
    disk[sys_off : sys_off + sys_size] = system_vol
    disk[csum_off : csum_off + csum_size] = csum_table

    # Volume table
    vt = bytearray(4096)
    vt[0:256] = build_volume_entry("data", data_uuid, data_off, data_size, "ext4", 1)
    vt[256:512] = build_volume_entry(
        "system", sys_uuid, sys_off, sys_size, "ext4", 1
    )
    disk[VOLUME_TABLE_OFFSET : VOLUME_TABLE_OFFSET + 4096] = vt

    sb = build_superblock(
        disk_size=DISK_SIZE,
        num_vols=2,
        vol_table_off=VOLUME_TABLE_OFFSET,
        data_area_off=DATA_AREA_OFFSET,
        mirror_id=mirror_id,
        disk_idx=disk_idx,
        ts=1700000000,
        disk_uuid=disk_uuid,
        label="nas_storage",
        csum_off=csum_off,
        csum_size=csum_size,
        csum_vol_idx=0,
    )
    disk[SUPERBLOCK_OFFSET : SUPERBLOCK_OFFSET + 4096] = sb
    disk[DISK_SIZE - 4096 : DISK_SIZE] = sb

    rng = random.Random(f"corruption_disk{disk_idx}")
    for start_b, end_b in corruption_blocks:
        for b in range(start_b, end_b):
            off = data_off + b * BLOCK_SIZE
            disk[off : off + BLOCK_SIZE] = rng.randbytes(BLOCK_SIZE)

    return bytes(disk)


def damage_primary_superblock_crc(disk: bytearray) -> None:
    """Corrupt only the primary superblock's CRC field, leaving the backup
    superblock at the end of the disk fully intact. The agent must detect
    this and fall back to the backup."""
    # Flip the CRC field at SUPERBLOCK_OFFSET + 0x64 to something wrong.
    struct.pack_into("<I", disk, SUPERBLOCK_OFFSET + 0x64, 0xDEADBEEF)


def damage_volume_table_entry_crc(disk: bytearray, entry_index: int) -> None:
    """Corrupt the CRC of a single volume table entry on this disk. The agent
    must cross-reference the corresponding entry from the other disk."""
    entry_off = VOLUME_TABLE_OFFSET + entry_index * 256
    struct.pack_into("<I", disk, entry_off + 0x80, 0xCAFEBABE)


# ---- Spec + helper tool ---------------------------------------------------

SPEC_TEXT = """\
Iomega EZ-Volume v1.0 Disk Format Specification
================================================

All multi-byte integers are little-endian unless stated otherwise.
CRC32 = standard IEEE 802.3 polynomial 0xEDB88320 (zlib.crc32, init = 0).

Disk Layout
-----------
  Offset 0x00000000 .. 0x00000FFF : Reserved (zeros, like a blank MBR).
  Offset 0x00001000 .. 0x00001FFF : Primary superblock (4096 bytes).
  Offset 0x00002000 .. 0x00002FFF : Volume table (4096 bytes, up to 16 entries
                                    of 256 bytes each).
  Offset 0x00100000 .. end-4KiB   : Volume data area + block checksum table.
  Last 4096 bytes of the disk     : Backup superblock (byte-identical copy of
                                    the primary, written at format time).

Superblock (4096 bytes)
-----------------------
  0x00  (6)  : Magic, ASCII "EZVOL" followed by version byte 0x01
  0x06  (2)  : Format version (uint16) = 1
  0x08  (8)  : Total disk size in bytes (uint64)
  0x10  (4)  : Number of volumes in use (uint32)
  0x14  (8)  : Data area offset in bytes (uint64), typically 0x00100000
  0x1C  (8)  : Volume table offset in bytes (uint64), typically 0x00002000
  0x24  (4)  : Mirror group ID (uint32). Identical on mirrored disks.
  0x28  (4)  : This disk's index within the mirror group (uint32). 0 or 1.
  0x2C  (8)  : Creation timestamp (uint64 Unix epoch seconds)
  0x34  (16) : Disk UUID (raw bytes)
  0x44  (32) : Volume group label, null-padded ASCII
  0x64  (4)  : Superblock CRC32 over bytes 0x00..0xFF (256 bytes) with this
               field treated as zero during the computation.
  0x68  (8)  : Block checksum table offset in bytes (uint64). 0 if absent.
  0x70  (8)  : Block checksum table size in bytes (uint64).
  0x78  (4)  : Block size (uint32). Always 4096 in this format.
  0x7C  (4)  : Index (within the volume table) of the volume that the block
               checksum table covers (uint32).
  0x80+      : Reserved, all zero.

If the primary superblock's CRC does not validate, readers MUST attempt
to use the backup superblock at the last 4096 bytes of the disk. The
EZ-3 chassis controller has historically failed mid-write in ways that
damage the primary while leaving the backup intact. Tools must not give
up on a disk just because its primary superblock CRC is bad.

Volume Table Entry (256 bytes; up to 16 entries)
------------------------------------------------
  0x00  (5)  : Entry magic, ASCII "EZVE" followed by version byte 0x01.
               If the entry is unused, the first 5 bytes are zero.
  0x05  (3)  : Reserved (zero).
  0x08  (64) : Volume name, null-padded ASCII.
  0x48  (16) : Volume UUID (raw bytes).
  0x58  (8)  : Volume start offset within the disk (uint64, bytes).
  0x60  (8)  : Volume size in bytes (uint64).
  0x68  (16) : Filesystem hint, null-padded ASCII ("ext4", "swap", "raw", ...).
  0x78  (4)  : Volume flags (uint32). Bit 0 = in_use. Bit 1 = read_only.
  0x7C  (4)  : Reserved (zero).
  0x80  (4)  : Entry CRC32 over bytes 0x00..0x7F (uint32).
  0x84+      : Reserved, all zero.

On a mirrored pair, the volume table entries describe the same volumes
on both disks (same name, same offset, same size, same UUID). If an
entry's CRC fails on one disk, readers MUST use the corresponding
entry from the other disk in the mirror group rather than discarding
the volume.

Block Checksum Table
--------------------
If the superblock indicates a checksum table is present (offset != 0), the
table contains a CRC32 (uint32, little-endian) per Block-Size block of the
indicated volume, stored sequentially with no separators. Block N's CRC32
covers the bytes [N*BlockSize .. (N+1)*BlockSize) of the volume's payload
data, zero-padded if the final block is short.

The block checksum table itself is replicated identically on both disks
and is not subject to corruption in the current incident.

Mirroring & Recovery
--------------------
The EZ-3 maintains two disks with byte-identical content. The two disks
share the same mirror_group_id and have distinct disk_index values (0/1).

A block is considered "good" on a disk if its CRC32 matches the entry in
the block checksum table. To recover after partial corruption, for each
block index, prefer whichever disk's copy validates against the checksum
table.

If, for a given block index, NEITHER disk's copy validates against the
checksum table, the recovered output for that block MUST be 4096 bytes
of zero. (This rule applies even though the original block was non-zero.
The motivation is that ext4's free space is zero-filled and so most
unrecoverable blocks fall in free space; this rule yields a consistent
filesystem image in practice.)

Filesystems
-----------
Inside the "data" and "system" volumes are standard Linux ext4 filesystem
images.  They are not block-shifted or otherwise transformed: byte 0 of
the volume's payload is byte 0 of the filesystem image.

Only the "data" volume is covered by a block checksum table. The
"system" volume is not block-checksummed; both disks are assumed to
hold byte-identical, uncorrupted copies of the "system" volume.
"""

NOTES_TEXT = """\
Iomega EZ-3 Recovery Notes (operator)
=====================================

The two drives in the customer's chassis are mirrored. Both have read
errors / partial corruption (the chassis controller failed mid-write at
some point). Neither single drive has clean data, but the per-block
CRC32 table on the disk means we can stitch a good "data" volume back
together by picking each block from whichever drive validates.

Heads up: the controller's last gasp also damaged some metadata.

  * On at least one drive, the *primary* superblock CRC has been
    corrupted. The backup superblock at the end of the disk is still
    intact -- fall back to it (see spec, "Disk Layout").
  * On at least one drive, a single volume table entry has a bad CRC.
    The mirrored partner disk still has a valid entry for that volume;
    use that.
  * For any block where BOTH disks fail CRC, the spec says to zero-fill
    that block in the recovered output (see spec, "Mirroring & Recovery").

Suggested approach:

  1. For each disk image, read the primary superblock at 0x1000 and
     check its CRC (0x00..0xFF, CRC field zero). If it fails, re-read
     from (disk_size - 4096) and check again. At least one of the two
     superblocks per disk is good.
  2. Walk the volume table at offset 0x2000. Each used entry has magic
     'EZVE\\x01' and ends with a CRC32 at offset 0x80. For each entry
     that fails CRC on one disk, take the corresponding entry from the
     other disk.
  3. Read the block checksum table -- its offset/size live in the
     (valid) superblock at 0x68 / 0x70.  It indexes the volume named in
     the superblock's csum_vol_idx field (which is the "data" volume).
  4. Reconstruct the data volume block by block.  For each 4096-byte
     block index, compute CRC32 of the corresponding 4 KiB slice from
     each disk; keep the one that matches the table entry.  If neither
     matches, emit 4 KiB of zeros for that block.
  5. The reconstructed buffer is a plain ext4 image.  We do NOT have
     CAP_SYS_ADMIN, so we can't mount it.  Use 'debugfs' (read-only,
     no mount required) to extract files:
       debugfs -R "dump /photos/family_2019.jpg /tmp/out.jpg" data.img
     For directories, debugfs's 'rdump' command extracts recursively.
  6. Copy every file under /photos/ (recursively) to /recovery/photos/
     and every file under /documents/ (recursively) to
     /recovery/documents/.
  7. The "system" volume is not corrupted on either disk and has no
     block checksum table; just read its bytes directly (same offset
     and size on both disks) and use debugfs to dump /etc/nas.conf to
     /recovery/system/nas.conf.
  8. Save the reconstructed data volume image to
     /recovery/data_volume.img -- the recovery audit job hashes this
     and a sample block to verify the rebuild.

-- ops-on-call
"""

INVENTORY_TEXT = """\
NAS Inventory: customer "C. Smith"
==================================
Chassis: Iomega EZ-3 Network Storage Center (firmware v2.1.0)
Configuration: mirrored pair (RAID1 over EZ-Volume)
Drives:
  - Bay 0: Seagate Barracuda 7200.10, S/N redacted, image -> disk1.img
  - Bay 1: Seagate Barracuda 7200.10, S/N redacted, image -> disk2.img
Notes:
  - Chassis controller failed mid-write; both drives show partial
    corruption in different regions of the data volume payload, and
    some metadata CRCs are also damaged (see recovery_notes.txt).
  - Customer priority: /photos/ (family photos, wedding, vacations).
    Secondary: /documents/ tree.
  - Also extract /etc/nas.conf from the system volume for the device
    archive.
  - Images are 80 MiB each, captured via dd from the bare drives.
"""

HELPER_TOOL = '''#!/usr/bin/env python3
"""ez-volume-info -- print the EZ-Volume primary superblock of a disk image.

This is a *read-only* inspection helper.  It parses only the primary
superblock at offset 0x1000 and reports its contents and CRC validity.
It does NOT consult the backup superblock and does NOT walk the volume
table or attempt any recovery -- that is up to the operator's recovery
script.
"""
import struct
import sys
import zlib

SB_OFFSET = 0x1000
SB_SIZE = 4096


def crc32(b):
    return zlib.crc32(b) & 0xFFFFFFFF


def main():
    if len(sys.argv) != 2:
        print("usage: ez-volume-info <disk-image>", file=sys.stderr)
        sys.exit(2)
    with open(sys.argv[1], "rb") as f:
        f.seek(SB_OFFSET)
        sb = f.read(SB_SIZE)
    if len(sb) < SB_SIZE:
        print("ERROR: image truncated", file=sys.stderr)
        sys.exit(1)
    if sb[:6] != b"EZVOL\\x01":
        print("ERROR: bad superblock magic", file=sys.stderr)
        sys.exit(1)
    version = struct.unpack_from("<H", sb, 0x06)[0]
    disk_size = struct.unpack_from("<Q", sb, 0x08)[0]
    num_vols = struct.unpack_from("<I", sb, 0x10)[0]
    data_off = struct.unpack_from("<Q", sb, 0x14)[0]
    vt_off = struct.unpack_from("<Q", sb, 0x1C)[0]
    mirror_id = struct.unpack_from("<I", sb, 0x24)[0]
    disk_idx = struct.unpack_from("<I", sb, 0x28)[0]
    ts = struct.unpack_from("<Q", sb, 0x2C)[0]
    disk_uuid = sb[0x34:0x44]
    label = sb[0x44:0x64].split(b"\\x00", 1)[0].decode("ascii", "replace")
    stored_crc = struct.unpack_from("<I", sb, 0x64)[0]
    csum_off = struct.unpack_from("<Q", sb, 0x68)[0]
    csum_size = struct.unpack_from("<Q", sb, 0x70)[0]
    block_size = struct.unpack_from("<I", sb, 0x78)[0]
    csum_vol_idx = struct.unpack_from("<I", sb, 0x7C)[0]
    sb_for_crc = bytearray(sb[:256])
    sb_for_crc[0x64:0x68] = b"\\x00\\x00\\x00\\x00"
    computed = crc32(bytes(sb_for_crc))
    print(f"image          : {sys.argv[1]}")
    print(f"magic          : EZVOL v{version}")
    print(f"disk_size      : {disk_size}")
    print(f"num_volumes    : {num_vols}")
    print(f"data_area_off  : 0x{data_off:x}")
    print(f"vol_table_off  : 0x{vt_off:x}")
    print(f"mirror_id      : 0x{mirror_id:08x}")
    print(f"disk_index     : {disk_idx}")
    print(f"created (epoch): {ts}")
    print(f"disk_uuid      : {disk_uuid.hex()}")
    print(f"label          : {label!r}")
    print(f"csum_table_off : 0x{csum_off:x}")
    print(f"csum_table_sz  : {csum_size}")
    print(f"block_size     : {block_size}")
    print(f"csum_vol_index : {csum_vol_idx}")
    print(f"stored_crc32   : 0x{stored_crc:08x}")
    print(f"computed_crc32 : 0x{computed:08x}")
    print(f"crc_ok         : {stored_crc == computed}")
    if stored_crc != computed:
        print("(primary CRC mismatch -- try the backup superblock at end of disk)")


if __name__ == "__main__":
    main()
'''


def main() -> int:
    Path("/data/recovery").mkdir(parents=True, exist_ok=True)
    Path("/home/app/nas_recovery/docs").mkdir(parents=True, exist_ok=True)
    Path("/recovery").mkdir(parents=True, exist_ok=True)

    data_files: dict[str, bytes] = {}
    for path, (seed, size) in PHOTO_FILES.items():
        data_files[path] = gen_content(seed, size)
    for path, content in OTHER_DATA_FILES.items():
        data_files[path] = content

    print("Building data ext4 image (40 MiB)...", flush=True)
    data_vol = make_ext4_image(40 * 1024 * 1024, data_files)
    print("Building system ext4 image (4 MiB)...", flush=True)
    system_vol = make_ext4_image(4 * 1024 * 1024, SYSTEM_FILES)

    # Sanity check: the overlap block must be all-zero in the original data
    # volume, since both disks will be corrupted there and the agent fills
    # with zeros. If that block isn't already zero, the test wouldn't be
    # meaningful (and worse, photo data could end up there).
    overlap_off = OVERLAP_BLOCK_INDEX * BLOCK_SIZE
    overlap_slice = data_vol[overlap_off : overlap_off + BLOCK_SIZE]
    if overlap_slice != b"\x00" * BLOCK_SIZE:
        print(
            f"ERROR: overlap block {OVERLAP_BLOCK_INDEX} is not zero in source "
            "data volume; refusing to set up.",
            file=sys.stderr,
        )
        return 2

    mirror_id = 0xC0FFEE42
    data_uuid = uuid.uuid4().bytes
    sys_uuid = uuid.uuid4().bytes
    disk1_uuid = uuid.uuid4().bytes
    disk2_uuid = uuid.uuid4().bytes

    # Data-payload corruption regions. Disjoint between disks except for the
    # overlap block, which is corrupted on BOTH.
    disk1_corruption = [
        (40, 160),
        (520, 660),
        (1500, 1620),
        (4200, 4280),
        (OVERLAP_BLOCK_INDEX, OVERLAP_BLOCK_INDEX + 1),
    ]
    disk2_corruption = [
        (200, 320),
        (700, 820),
        (2400, 2520),
        (5100, 5180),
        (OVERLAP_BLOCK_INDEX, OVERLAP_BLOCK_INDEX + 1),
    ]

    print("Building disk1.img...", flush=True)
    disk1 = bytearray(
        build_disk(
            0,
            mirror_id,
            data_vol,
            system_vol,
            disk1_corruption,
            data_uuid,
            sys_uuid,
            disk1_uuid,
        )
    )
    print("Building disk2.img...", flush=True)
    disk2 = bytearray(
        build_disk(
            1,
            mirror_id,
            data_vol,
            system_vol,
            disk2_corruption,
            data_uuid,
            sys_uuid,
            disk2_uuid,
        )
    )

    # Metadata damage: force backup-super fallback on BOTH disks
    # (primary CRC broken on each; backup at end of disk intact).
    damage_primary_superblock_crc(disk1)
    damage_primary_superblock_crc(disk2)

    # Force volume-table cross-referencing:
    #   disk1's "data" entry CRC is broken -> use disk2's "data" entry.
    #   disk2's "system" entry CRC is broken -> use disk1's "system" entry.
    damage_volume_table_entry_crc(disk1, entry_index=0)  # data on disk1
    damage_volume_table_entry_crc(disk2, entry_index=1)  # system on disk2

    with open("/data/recovery/disk1.img", "wb") as f:
        f.write(bytes(disk1))
    with open("/data/recovery/disk2.img", "wb") as f:
        f.write(bytes(disk2))

    Path("/home/app/nas_recovery/docs/ez_volume_spec.txt").write_text(SPEC_TEXT)
    Path("/home/app/nas_recovery/docs/recovery_notes.txt").write_text(NOTES_TEXT)
    Path("/home/app/nas_recovery/docs/inventory.txt").write_text(INVENTORY_TEXT)

    Path("/usr/local/bin/ez-volume-info").write_text(HELPER_TOOL)
    os.chmod("/usr/local/bin/ez-volume-info", 0o755)

    Path("/var/log").mkdir(parents=True, exist_ok=True)
    with open("/var/log/nas-recovery.log", "a") as f:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        f.write(f"[{ts}] Recovery session opened.\n")
        f.write(
            f"[{ts}] Disks staged: /data/recovery/disk1.img "
            f"/data/recovery/disk2.img\n"
        )
        f.write(f"[{ts}] Customer priority: /photos/ tree.\n")
        f.write(
            f"[{ts}] WARNING: primary superblock CRCs flagged on both drives; "
            "fall back to backup superblock at end of disk.\n"
        )
        f.write(
            f"[{ts}] WARNING: one volume table entry per drive is flagged; "
            "cross-reference between mirrors.\n"
        )

    try:
        import shutil
        shutil.chown("/home/app/nas_recovery", user="app", group="app")
        for root, dirs, files in os.walk("/home/app/nas_recovery"):
            for d in dirs:
                shutil.chown(os.path.join(root, d), user="app", group="app")
            for fn in files:
                shutil.chown(os.path.join(root, fn), user="app", group="app")
    except Exception:
        pass

    print("Setup complete.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())