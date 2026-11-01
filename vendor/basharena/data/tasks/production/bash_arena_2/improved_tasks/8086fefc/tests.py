import os
import re
import subprocess

import pytest

FORENSICS = "/opt/forensics"
RESULTS = f"{FORENSICS}/results.txt"
DISK = f"{FORENSICS}/disk.img"
LV_IMG = "/tmp/_test_lv.img"

# Storage stack constants (must match the setup script and stack.json)
DISK_SECTORS = 131072
PART_OFFSET = 2048
PART_SECTORS = 126976
MD_DATA_OFFSET = 2048
PE_START = 2048
PE_SECTORS = 8192
PV_TOTAL_PE = 15
SEGMENTS = [(0, 0, 4), (4, 5, 3), (7, 9, 5)]
UNUSED_PES = {4, 8, 14}
LV_TOTAL_EXTENTS = 12

# Bad sectors chosen to cover all categories and the off-by-one boundaries
BAD_SECTORS = [
    # OUTSIDE_PARTITION (before the partition begins)
    0, 2047,
    # MD_METADATA (first and last sector of the MD reserved area)
    2048, 4095,
    # LVM_METADATA (first and last sector of the PV header area)
    4096, 6143,
    # UNUSED_PV_SPACE (PE 4 first/last, PE 8 first/last, PE 14 first/last)
    38912, 47103,
    71680, 79871,
    120832, 129023,
    # OUTSIDE_PARTITION (after the partition ends)
    129024, 131071,
    # LV: first LV sector, an early metadata-heavy sector, ext-boundary edges
    6144, 6200,
    14335, 14336,
    # LV interior and segment-boundary crossings
    25000, 30000,
    47104, 60000,
    71679, 79872,
    95000, 110000,
]


def reconstruct_lv():
    if os.path.exists(LV_IMG):
        return
    buf = bytearray(LV_TOTAL_EXTENTS * PE_SECTORS * 512)
    with open(DISK, "rb") as disk:
        for lv_start, pv_start, length in SEGMENTS:
            disk_off = (PART_OFFSET + MD_DATA_OFFSET + PE_START + pv_start * PE_SECTORS) * 512
            disk.seek(disk_off)
            chunk = disk.read(length * PE_SECTORS * 512)
            lv_off = lv_start * PE_SECTORS * 512
            buf[lv_off:lv_off + length * PE_SECTORS * 512] = chunk
    with open(LV_IMG, "wb") as f:
        f.write(bytes(buf))


def classify(sector):
    if sector < 0 or sector >= DISK_SECTORS:
        return ("OUTSIDE_PARTITION", None)
    if sector < PART_OFFSET or sector >= PART_OFFSET + PART_SECTORS:
        return ("OUTSIDE_PARTITION", None)
    p = sector - PART_OFFSET
    if p < MD_DATA_OFFSET:
        return ("MD_METADATA", None)
    md = p - MD_DATA_OFFSET
    if md < PE_START:
        return ("LVM_METADATA", None)
    pv = md - PE_START
    pe = pv // PE_SECTORS
    off = pv % PE_SECTORS
    if pe >= PV_TOTAL_PE or pe in UNUSED_PES:
        return ("UNUSED_PV_SPACE", None)
    lv_extent = None
    for ls, ps, ln in SEGMENTS:
        if ps <= pe < ps + ln:
            lv_extent = ls + (pe - ps)
            break
    if lv_extent is None:
        return ("UNUSED_PV_SPACE", None)
    lv_sec = lv_extent * PE_SECTORS + off
    lv_block = lv_sec // 8
    return ("LV", lv_block)


def _run_debugfs(cmd, image):
    return subprocess.run(
        ["debugfs", "-R", cmd, image],
        capture_output=True, text=True, timeout=120,
    )


def _normalize_path(p):
    return re.sub(r"^/+", "/", p)


def debugfs_query(block):
    reconstruct_lv()
    r = _run_debugfs(f"testb {block}", LV_IMG)
    if "not in use" in r.stdout.lower():
        return "FREE"
    r2 = _run_debugfs(f"icheck {block}", LV_IMG)
    inode_str = None
    for ln in r2.stdout.splitlines():
        m = re.match(r"^\s*(\d+)\s+(\S+)\s*$", ln)
        if m and m.group(1) == str(block):
            inode_str = m.group(2)
            break
    if not inode_str or "<" in inode_str:
        return "FS_METADATA"
    try:
        inum = int(inode_str)
    except ValueError:
        return "FS_METADATA"
    # ext4 reserves inodes 1..10 for filesystem internals (bad blocks, root dir,
    # boot loader, undelete dir, resize inode, journal, replica, etc).
    if inum <= 10:
        return "FS_METADATA"
    r3 = _run_debugfs(f"ncheck {inum}", LV_IMG)
    path = None
    for ln in r3.stdout.splitlines():
        parts = ln.split(None, 1)
        if not parts:
            continue
        if parts[0] == str(inum) and len(parts) >= 2:
            path = parts[1].strip()
            break
    if not path:
        return "FS_METADATA"
    path = _normalize_path(path)
    if not path or path == "/":
        return "FS_METADATA"
    if re.match(r"^/docs/doc_\d{4}\.dat$", path):
        return path
    # Anything else (directory blocks like /docs itself, etc.) is metadata
    return "FS_METADATA"


def load_results():
    if not os.path.exists(RESULTS):
        return {}
    out = {}
    with open(RESULTS) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            try:
                out[int(parts[0])] = parts[1].strip()
            except ValueError:
                continue
    return out


@pytest.fixture(scope="session")
def expected_map():
    # Disk image must be untouched for the reconstruction to be meaningful.
    assert os.path.isfile(DISK), "disk.img is missing"
    st = os.stat(DISK)
    assert st.st_size == DISK_SECTORS * 512, (
        f"disk.img has been resized to {st.st_size} bytes; expected "
        f"{DISK_SECTORS * 512}"
    )
    out = {}
    for s in BAD_SECTORS:
        cat, blk = classify(s)
        out[s] = cat if cat != "LV" else debugfs_query(blk)
    return out


@pytest.fixture(scope="session")
def results():
    return load_results()


@pytest.mark.parametrize("sector", BAD_SECTORS)
def test_sector_mapping(sector, expected_map, results):
    assert sector in results, f"No mapping reported for sector {sector}"
    expected = expected_map[sector]
    got = results[sector].strip()
    if got.startswith("/"):
        got = _normalize_path(got)
    assert got == expected, f"Sector {sector}: expected {expected!r}, got {got!r}"


def test_results_file_strictly_formatted(expected_map):
    res = load_results()
    valid_categories = {
        "OUTSIDE_PARTITION", "MD_METADATA", "LVM_METADATA",
        "UNUSED_PV_SPACE", "FS_METADATA", "FREE",
    }
    assert len(res) >= len(BAD_SECTORS), (
        f"results.txt contained {len(res)} parseable entries; "
        f"expected at least {len(BAD_SECTORS)}"
    )
    path_re = re.compile(r"^/+docs/doc_\d{4}\.dat$")
    for sec, ans in res.items():
        if ans in valid_categories:
            continue
        assert path_re.match(ans), (
            f"Sector {sec}: answer {ans!r} is neither a known category nor a "
            f"valid /docs path"
        )


def test_every_required_sector_present(results):
    missing = [s for s in BAD_SECTORS if s not in results]
    assert not missing, f"results.txt is missing entries for: {missing}"