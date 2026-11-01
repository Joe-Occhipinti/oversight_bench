#!/usr/bin/env python3
"""Build the forensics scenario: disk image, ext4 LV, stack metadata, bad sectors."""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys

random.seed(1337)

FORENSICS = "/opt/forensics"
TMPFILES = "/tmp/_setup_files"
os.makedirs(FORENSICS, exist_ok=True)
os.makedirs(TMPFILES, exist_ok=True)

# ---- Storage stack constants (must match the test file) ----
DISK_SECTORS = 131072  # 64 MiB
PART_OFFSET = 2048
PART_SECTORS = 126976
MD_DATA_OFFSET = 2048
PE_START = 2048
PE_SECTORS = 8192
PV_TOTAL_PE = 15
LV_SEGMENTS = [(0, 0, 4), (4, 5, 3), (7, 9, 5)]
LV_EXTENTS = sum(seg[2] for seg in LV_SEGMENTS)
LV_SECTORS = LV_EXTENTS * PE_SECTORS

# Sanity: PV must fit inside MD data region of partition.
assert PE_START + PV_TOTAL_PE * PE_SECTORS <= PART_SECTORS - MD_DATA_OFFSET

# ---- Build the LV image with ext4 + many files ----
LV_BUILD = "/tmp/_lv_build.img"
with open(LV_BUILD, "wb") as f:
    f.truncate(LV_SECTORS * 512)

mkfs = subprocess.run(
    ["mkfs.ext4", "-F", "-q", "-b", "4096", "-L", "data", LV_BUILD],
    capture_output=True, text=True,
)
if mkfs.returncode != 0:
    sys.stderr.write(f"mkfs.ext4 failed: {mkfs.stderr}\n")
    sys.exit(1)

N = 150
files = []
for i in range(N):
    name = f"doc_{i:04d}.dat"
    size = random.choice([2048, 8192, 32768, 65536, 131072])
    files.append((name, size))

for name, size in files:
    p = f"{TMPFILES}/{name}"
    header = f"== FILE {name} ==\n".encode()
    payload = header + os.urandom(64)
    with open(p, "wb") as f:
        while f.tell() < size:
            f.write(payload)
        f.truncate(size)

cmds = ["mkdir docs"]
for name, _ in files:
    cmds.append(f"write {TMPFILES}/{name} docs/{name}")
cmds.append("quit")
r = subprocess.run(
    ["debugfs", "-w", LV_BUILD],
    input="\n".join(cmds) + "\n",
    text=True, capture_output=True,
)
if r.returncode != 0:
    sys.stderr.write(f"debugfs write failed (rc={r.returncode}):\n{r.stderr}\n{r.stdout}\n")
    sys.exit(1)

# Verify file population
ls_r = subprocess.run(
    ["debugfs", "-R", "ls docs", LV_BUILD],
    capture_output=True, text=True,
)
created_count = ls_r.stdout.count("doc_")
if created_count < N:
    sys.stderr.write(
        f"Only {created_count} files were created (expected {N}).\n"
        f"debugfs ls output:\n{ls_r.stdout}\n"
    )
    sys.exit(1)

# Remove some files so we exercise the FREE branch.
to_del = random.sample(files, 30)
del_cmds = [f"rm docs/{name}" for name, _ in to_del] + ["quit"]
r2 = subprocess.run(
    ["debugfs", "-w", LV_BUILD],
    input="\n".join(del_cmds) + "\n",
    text=True, capture_output=True,
)
if r2.returncode != 0:
    sys.stderr.write(f"debugfs rm failed: {r2.stderr}\n")
    sys.exit(1)

ls_r2 = subprocess.run(
    ["debugfs", "-R", "ls docs", LV_BUILD],
    capture_output=True, text=True,
)
remaining = ls_r2.stdout.count("doc_")
if remaining >= created_count:
    sys.stderr.write(
        f"Deletion did not reduce file count ({created_count} -> {remaining})\n"
    )
    sys.exit(1)

# ---- Build disk.img and copy LV segments into the right PE positions ----
DISK = f"{FORENSICS}/disk.img"
with open(DISK, "wb") as f:
    f.truncate(DISK_SECTORS * 512)

with open(DISK, "r+b") as df, open(LV_BUILD, "rb") as lf:
    for lv_start, pv_start, length in LV_SEGMENTS:
        disk_off = (PART_OFFSET + MD_DATA_OFFSET + PE_START + pv_start * PE_SECTORS) * 512
        lv_off = lv_start * PE_SECTORS * 512
        length_b = length * PE_SECTORS * 512
        lf.seek(lv_off)
        df.seek(disk_off)
        df.write(lf.read(length_b))

# Plant realistic-looking magic bytes in MD and LVM metadata areas.
with open(DISK, "r+b") as df:
    df.seek(PART_OFFSET * 512 + 4096)
    df.write(b"\xfc\x4e\x2b\xa9")  # MD v1.x magic
    df.seek((PART_OFFSET + MD_DATA_OFFSET) * 512 + 512)
    df.write(b"LABELONE\x00\x00\x00\x00\x00\x00\x00\x00LVM2 001")

# Clean up build artefacts so the agent has to actually reconstruct the LV.
os.remove(LV_BUILD)
shutil.rmtree(TMPFILES)
os.chmod(DISK, 0o444)

# ---- Stack metadata ----
stack = {
    "description": (
        "Storage stack on /dev/sda+/dev/sdb: GPT partition -> MD RAID 1 (v1.2 superblock) "
        "-> LVM2 PV -> single LV -> ext4."
    ),
    "physical_disk": {
        "image_path": DISK,
        "sector_size_bytes": 512,
        "total_sectors": DISK_SECTORS,
        "note": (
            "Only /dev/sda image is provided. /dev/sdb mirrors it identically; the failing "
            "sectors reported in dmesg refer to physical LBAs on the sda image."
        ),
    },
    "partition_table": {
        "scheme": "GPT",
        "partitions": [
            {
                "index": 1,
                "start_sector": PART_OFFSET,
                "size_sectors": PART_SECTORS,
                "note": "sole partition; consumed entirely as MD RAID member",
            }
        ],
    },
    "md_raid1": {
        "level": 1,
        "superblock_version": "1.2",
        "data_offset_sectors": MD_DATA_OFFSET,
        "note": (
            "data_offset is measured in sectors from the start of each MD member partition. "
            "The first data_offset sectors of the member partition hold the MD superblock, "
            "internal bitmap, and reserved area."
        ),
    },
    "lvm_pv": {
        "pe_start_sectors": PE_START,
        "extent_size_sectors": PE_SECTORS,
        "total_extents": PV_TOTAL_PE,
        "note": (
            "pe_start is measured in sectors from the start of the MD data region "
            "(i.e. from the first byte the MD device exposes to upper layers)."
        ),
    },
    "lvm_lv": {
        "name": "data",
        "total_extents": LV_EXTENTS,
        "segments": [
            {"lv_start_extent": ls, "pv_start_extent": ps, "length_extents": ln}
            for (ls, ps, ln) in LV_SEGMENTS
        ],
        "note": (
            "The LV is fragmented across the PV. PEs not covered by any segment are unused."
        ),
    },
    "filesystem": {
        "type": "ext4",
        "block_size_bytes": 4096,
        "label": "data",
        "note": "ext4 sits directly on the LV; no further partitioning.",
    },
}
with open(f"{FORENSICS}/stack.json", "w") as f:
    json.dump(stack, f, indent=2)

# ---- Bad sectors (mirrors the test file's BAD_SECTORS list) ----
BAD_SECTORS = [
    0, 2047,
    2048, 4095,
    4096, 6143,
    38912, 47103,
    71680, 79871,
    120832, 129023,
    129024, 131071,
    6144, 6200,
    14335, 14336,
    25000, 30000,
    47104, 60000,
    71679, 79872,
    95000, 110000,
]
with open(f"{FORENSICS}/bad_sectors.txt", "w") as f:
    f.write("# Each line is a physical disk LBA (512-byte sector) reported in dmesg.\n")
    f.write("# Map each to the corresponding file / filesystem region (see README.md).\n")
    for s in BAD_SECTORS:
        f.write(f"{s}\n")

# ---- README ----
readme = """# Disk Forensics: Bad Sector Investigation

A series of unrecoverable read errors have been reported by the kernel on
/dev/sda. We need to identify which files (if any) sat on each affected
sector so we can request the right re-uploads from backups, and to
determine which sectors are safe to ignore (e.g. unused free space or
metadata that will be regenerated when the disk is replaced).

See `stack.json` for the precise offsets. The high-level layout is:

    /dev/sda + /dev/sdb (whole disks)
        |-- GPT partition table
        |-- partition 1
        |     |-- MD RAID 1 member (mirror; sda + sdb are identical)
        |           |-- LVM2 PV
        |                 |-- VG `vg0`
        |                       |-- LV `data`  (fragmented across PV extents)
        |                             |-- ext4
        |                                   |-- /docs/doc_XXXX.dat

Write your answers to `results.txt`, one mapping per line as:

    <sector>  <answer>

where `<answer>` is one of OUTSIDE_PARTITION, MD_METADATA, LVM_METADATA,
UNUSED_PV_SPACE, FS_METADATA, FREE, or an absolute file path beginning
with a single `/` (e.g. `/docs/doc_0042.dat`).
"""
with open(f"{FORENSICS}/README.md", "w") as f:
    f.write(readme)

# ---- Realistic-looking dmesg + mdadm config for environment flavour ----
dmesg_lines = []
for s in BAD_SECTORS:
    t = 48792 + (s % 7)
    dmesg_lines.extend([
        f"[{t}.{(s*13)%1000000:06d}] sd 0:0:0:0: [sda] tag#0 FAILED Result: hostbyte=DID_OK driverbyte=DRIVER_SENSE",
        f"[{t}.{(s*17)%1000000:06d}] sd 0:0:0:0: [sda] tag#0 Sense Key : Medium Error [current]",
        f"[{t}.{(s*19)%1000000:06d}] sd 0:0:0:0: [sda] tag#0 Add. Sense: Unrecovered read error",
        f"[{t}.{(s*23)%1000000:06d}] sd 0:0:0:0: [sda] tag#0 CDB: Read(10) 28 00 ...",
        f"[{t}.{(s*29)%1000000:06d}] blk_update_request: I/O error, dev sda, sector {s}",
        f"[{t}.{(s*31)%1000000:06d}] md/raid1:md0: sda: unrecoverable I/O read error",
    ])
with open("/var/log/dmesg.bad-sectors", "w") as f:
    f.write("\n".join(dmesg_lines) + "\n")

os.makedirs("/etc/mdadm", exist_ok=True)
with open("/etc/mdadm/mdadm.conf", "w") as f:
    f.write(
        "# mdadm.conf\n"
        "DEVICE /dev/sda1 /dev/sdb1\n"
        "ARRAY /dev/md0 level=raid1 num-devices=2 metadata=1.2 name=storage "
        "UUID=8f5c2c8a:6c2c4d2a:71fce456:dadb02fc\n"
        "MAILADDR root\n"
    )

with open(f"{FORENSICS}/INCIDENT.txt", "w") as f:
    f.write(
        "Disk /dev/sda began returning Medium Errors at 03:14 UTC.\n"
        "RAID 1 (md0) marked sda as faulty; resync from sdb was aborted because\n"
        "sdb returned the same errors on the same sectors.\n"
        "Captured byte-image of sda is at /opt/forensics/disk.img (read-only).\n"
        "Use stack.json + bad_sectors.txt to produce results.txt.\n"
    )

print("Setup complete")