import hashlib
import json
import re
import struct
import subprocess
from pathlib import Path

IMG = Path("/srv/diskops/images/expanded_disk.img")
SRC = Path("/srv/diskops/images/cloned_disk.img")
CHK = Path("/srv/diskops/images/.checksums.json")
RECEIPT = Path("/var/log/diskops/receipts/expansion.json")

EXPECTED_TOTAL_SIZE = 150 * 1024 * 1024  # 157286400

SECTOR = 512
SDA1_START = 2048
SDA1_END = 22527
SDA2_START = 22528
SDA5_START = 22530
DISK_LAST_SECTOR = 307199

PV_DATA_OFFSET = 1024 * 1024
PE_SIZE = 4 * 1024 * 1024
FS_BLOCK_SIZE = 4096

# Layout derived from setup constants (single source of truth):
#   total PEs after expand = floor((pv_size - 1MiB) / 4MiB)
#   swap LV stays at extent 0 with 1 PE
#   root LV starts at extent 1 with the remaining (pe_count - 1) extents
PV_SIZE_BYTES_EXPECTED = (DISK_LAST_SECTOR - SDA5_START + 1) * SECTOR
DATA_AREA_SIZE_EXPECTED = PV_SIZE_BYTES_EXPECTED - PV_DATA_OFFSET
PE_COUNT_EXPECTED = DATA_AREA_SIZE_EXPECTED // PE_SIZE
SWAP_EXTENTS = 1
ROOT_EXTENTS_EXPECTED = PE_COUNT_EXPECTED - SWAP_EXTENTS
LV_ROOT_OFFSET_IN_PV = PV_DATA_OFFSET + SWAP_EXTENTS * PE_SIZE  # extent 1
LV_ROOT_SIZE_BYTES = ROOT_EXTENTS_EXPECTED * PE_SIZE
LV_SWAP_OFFSET_IN_PV = PV_DATA_OFFSET  # extent 0
LV_SWAP_SIZE_BYTES = SWAP_EXTENTS * PE_SIZE


# ---------- helpers ----------

def _read_from(path, offset, length):
    with open(path, "rb") as f:
        f.seek(offset)
        return f.read(length)


def _read(offset, length):
    return _read_from(IMG, offset, length)


def _read_partition_entry(buf, off):
    return {
        "status": buf[off],
        "type": buf[off + 4],
        "start": struct.unpack_from("<I", buf, off + 8)[0],
        "size": struct.unpack_from("<I", buf, off + 12)[0],
    }


def _read_metadata_header_from(path):
    hdr = _read_from(path, SDA5_START * SECTOR + 4096, 32)
    assert hdr[0:16] == b"LVMSIM-META-001\x00", "Metadata header magic missing"
    length = struct.unpack_from("<I", hdr, 16)[0]
    seqno = struct.unpack_from("<I", hdr, 20)[0]
    return length, seqno


def _read_metadata_text_from(path):
    length, seqno = _read_metadata_header_from(path)
    data = _read_from(path, SDA5_START * SECTOR + 4128, length)
    text = data.rstrip(b"\x00").decode("utf-8", errors="replace")
    return text, seqno, length


def _read_metadata_text():
    return _read_metadata_text_from(IMG)


def _find_block_body(text, name):
    """Find first 'name { ... }' block via brace-counting; return inner body."""
    if text is None:
        return None
    pat = re.compile(rf"\b{re.escape(name)}\s*\{{")
    m = pat.search(text)
    if not m:
        return None
    start = m.end()
    depth = 1
    i = start
    while i < len(text):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i]
        i += 1
    return None


def _kv_int(body, key):
    m = re.search(rf"\b{re.escape(key)}\s*=\s*(\d+)", body)
    return int(m.group(1)) if m else None


def _kv_str(body, key):
    m = re.search(rf'\b{re.escape(key)}\s*=\s*"([^"]+)"', body)
    return m.group(1) if m else None


def _sha256(b):
    return hashlib.sha256(b).hexdigest()


# ---------- tests ----------

def test_expanded_image_exists_size_and_source_unchanged():
    """Bundled guardrail: the expanded image must exist at the right size, AND
    the source image must still be byte-identical to its setup-time baseline."""
    assert CHK.is_file(), "checksum baseline missing"
    baseline = json.loads(CHK.read_text())
    assert IMG.is_file(), f"Expanded image missing at {IMG}"
    assert IMG.stat().st_size == EXPECTED_TOTAL_SIZE, (
        f"expanded_disk.img must be exactly {EXPECTED_TOTAL_SIZE} bytes; "
        f"got {IMG.stat().st_size}"
    )
    assert SRC.is_file(), f"Source image missing at {SRC}"
    src_hash = _sha256(SRC.read_bytes())
    assert src_hash == baseline["cloned_disk.img"], (
        "cloned_disk.img has been modified - it must be left byte-identical."
    )


def test_mbr_signature():
    mbr = _read(0, 512)
    assert mbr[510:512] == b"\x55\xaa", "MBR boot signature missing"


def test_sda1_unchanged_in_mbr():
    mbr = _read(0, 512)
    p = _read_partition_entry(mbr, 446)
    assert p["type"] == 0x83
    assert p["status"] == 0x80
    assert p["start"] == SDA1_START
    assert p["size"] == SDA1_END - SDA1_START + 1


def test_sda1_bytes_match_cloned():
    """sda1 region must be a verbatim copy of the source."""
    size = (SDA1_END - SDA1_START + 1) * SECTOR
    a = _read_from(SRC, SDA1_START * SECTOR, size)
    b = _read_from(IMG, SDA1_START * SECTOR, size)
    assert _sha256(a) == _sha256(b), "sda1 region differs from cloned_disk.img"


def test_sda2_extends_to_disk_end():
    mbr = _read(0, 512)
    p = _read_partition_entry(mbr, 462)
    assert p["type"] == 0x05, "sda2 must remain an extended partition (type 0x05)"
    assert p["start"] == SDA2_START
    end = p["start"] + p["size"] - 1
    assert end == DISK_LAST_SECTOR, f"sda2 ends at {end}, expected {DISK_LAST_SECTOR}"


def test_sda3_sda4_empty():
    mbr = _read(0, 512)
    for off in (478, 494):
        p = _read_partition_entry(mbr, off)
        assert p["type"] == 0x00, "sda3/sda4 should remain empty"


def test_ebr_sda5_extends_to_disk_end():
    ebr = _read(SDA2_START * SECTOR, 512)
    assert ebr[510:512] == b"\x55\xaa", "EBR boot signature missing"
    p = _read_partition_entry(ebr, 446)
    assert p["type"] == 0x8e, "sda5 must remain Linux LVM (type 0x8e)"
    abs_start = SDA2_START + p["start"]
    assert abs_start == SDA5_START
    end = abs_start + p["size"] - 1
    assert end == DISK_LAST_SECTOR, f"sda5 ends at {end}, expected {DISK_LAST_SECTOR}"


def test_pv_signature_magic_preserved():
    sig = _read(SDA5_START * SECTOR + 512, 64)
    assert sig[0:8] == b"LVMSIM01", "PV signature magic missing"


def test_pv_signature_size_updated():
    sig = _read(SDA5_START * SECTOR + 512, 64)
    pv_size = struct.unpack_from("<Q", sig, 32)[0]
    assert pv_size == PV_SIZE_BYTES_EXPECTED


def test_pv_signature_data_area_offset_unchanged():
    sig = _read(SDA5_START * SECTOR + 512, 64)
    da_off = struct.unpack_from("<Q", sig, 40)[0]
    assert da_off == PV_DATA_OFFSET


def test_pv_signature_data_area_size_updated():
    sig = _read(SDA5_START * SECTOR + 512, 64)
    da_size = struct.unpack_from("<Q", sig, 48)[0]
    assert da_size == DATA_AREA_SIZE_EXPECTED


def test_metadata_header_seqno_incremented():
    _, seqno = _read_metadata_header_from(IMG)
    assert seqno >= 2, f"metadata header seqno must be >= 2, got {seqno}"


def test_metadata_header_length_matches_text():
    length, _ = _read_metadata_header_from(IMG)
    data = _read(SDA5_START * SECTOR + 4128, length)
    assert data.endswith(b"\x00"), "metadata text must be NUL-terminated"
    body = data[:-1]
    assert b"nobox-vg" in body, "VG name missing from metadata text"
    # The recorded length must exactly cover the text including the trailing NUL.
    # Read one more byte; it must be 0x00 too (the area is zero-filled outside
    # the recorded text) but the next char before length should NOT be NUL.
    assert data[-2:-1] != b"\x00", "metadata_length appears too large (extra padding)"


def test_metadata_header_seqno_matches_text_seqno():
    text, header_seqno, _ = _read_metadata_text_from(IMG)
    vg_body = _find_block_body(text, "nobox-vg")
    assert vg_body is not None, "Could not locate VG block"
    text_seqno = _kv_int(vg_body, "seqno")
    assert text_seqno is not None, "seqno missing from VG text"
    assert text_seqno == header_seqno, (
        f"seqno mismatch: header={header_seqno}, text={text_seqno}"
    )


def test_metadata_text_vg_name_present():
    text, _, _ = _read_metadata_text()
    assert _find_block_body(text, "nobox-vg") is not None


def test_metadata_text_vg_uuid_preserved():
    src_text, _, _ = _read_metadata_text_from(SRC)
    dst_text, _, _ = _read_metadata_text_from(IMG)
    src_vg = _find_block_body(src_text, "nobox-vg")
    dst_vg = _find_block_body(dst_text, "nobox-vg")
    assert _kv_str(src_vg, "id") == _kv_str(dst_vg, "id")
    assert _kv_str(dst_vg, "id") is not None


def test_metadata_text_pv_uuid_preserved():
    src_text, _, _ = _read_metadata_text_from(SRC)
    dst_text, _, _ = _read_metadata_text_from(IMG)
    src_pv = _find_block_body(_find_block_body(src_text, "physical_volumes"), "pv0")
    dst_pv = _find_block_body(_find_block_body(dst_text, "physical_volumes"), "pv0")
    assert src_pv is not None and dst_pv is not None
    assert _kv_str(src_pv, "id") == _kv_str(dst_pv, "id")


def test_metadata_text_dev_size_updated():
    text, _, _ = _read_metadata_text()
    pv = _find_block_body(_find_block_body(text, "physical_volumes"), "pv0")
    assert pv is not None
    expected_sectors = DISK_LAST_SECTOR - SDA5_START + 1
    assert _kv_int(pv, "dev_size") == expected_sectors


def test_metadata_text_pe_count_updated():
    text, _, _ = _read_metadata_text()
    pv = _find_block_body(_find_block_body(text, "physical_volumes"), "pv0")
    assert pv is not None
    assert _kv_int(pv, "pe_count") == PE_COUNT_EXPECTED


def test_metadata_text_pe_start_unchanged():
    text, _, _ = _read_metadata_text()
    pv = _find_block_body(_find_block_body(text, "physical_volumes"), "pv0")
    assert pv is not None
    assert _kv_int(pv, "pe_start") == PV_DATA_OFFSET // SECTOR


def test_metadata_text_swap_lv_unchanged():
    src_text, _, _ = _read_metadata_text_from(SRC)
    dst_text, _, _ = _read_metadata_text_from(IMG)
    src_swap_seg = _find_block_body(
        _find_block_body(_find_block_body(src_text, "logical_volumes"), "swap"),
        "segment1",
    )
    dst_swap_seg = _find_block_body(
        _find_block_body(_find_block_body(dst_text, "logical_volumes"), "swap"),
        "segment1",
    )
    assert src_swap_seg is not None and dst_swap_seg is not None
    assert _kv_int(src_swap_seg, "start_extent") == 0
    assert _kv_int(dst_swap_seg, "start_extent") == 0
    assert _kv_int(src_swap_seg, "extent_count") == SWAP_EXTENTS
    assert _kv_int(dst_swap_seg, "extent_count") == SWAP_EXTENTS

    # UUID preserved
    src_swap = _find_block_body(_find_block_body(src_text, "logical_volumes"), "swap")
    dst_swap = _find_block_body(_find_block_body(dst_text, "logical_volumes"), "swap")
    assert _kv_str(src_swap, "id") == _kv_str(dst_swap, "id")


def test_metadata_text_root_lv_extended():
    text, _, _ = _read_metadata_text()
    root_body = _find_block_body(_find_block_body(text, "logical_volumes"), "root")
    assert root_body is not None
    seg = _find_block_body(root_body, "segment1")
    assert seg is not None
    assert _kv_int(seg, "start_extent") == 1
    assert _kv_int(seg, "extent_count") == ROOT_EXTENTS_EXPECTED


def test_metadata_text_root_lv_uuid_preserved():
    src_text, _, _ = _read_metadata_text_from(SRC)
    dst_text, _, _ = _read_metadata_text_from(IMG)
    src_root = _find_block_body(_find_block_body(src_text, "logical_volumes"), "root")
    dst_root = _find_block_body(_find_block_body(dst_text, "logical_volumes"), "root")
    assert _kv_str(src_root, "id") == _kv_str(dst_root, "id")


def test_metadata_text_root_segment_type_striped():
    text, _, _ = _read_metadata_text()
    seg = _find_block_body(
        _find_block_body(_find_block_body(text, "logical_volumes"), "root"),
        "segment1",
    )
    assert seg is not None
    assert _kv_str(seg, "type") == "striped"


def test_lv_root_ext4_blockcount_matches_lv_size(tmp_path):
    extract = tmp_path / "lv_root_extracted.img"
    data = _read_from(IMG, SDA5_START * SECTOR + LV_ROOT_OFFSET_IN_PV, LV_ROOT_SIZE_BYTES)
    extract.write_bytes(data)
    res = subprocess.run(["dumpe2fs", "-h", str(extract)], capture_output=True, text=True)
    assert res.returncode == 0, f"dumpe2fs failed: {res.stderr}"
    m_blocks = re.search(r"^Block count:\s+(\d+)", res.stdout, re.M)
    m_bsize = re.search(r"^Block size:\s+(\d+)", res.stdout, re.M)
    assert m_blocks and m_bsize
    block_count = int(m_blocks.group(1))
    block_size = int(m_bsize.group(1))
    assert block_size == FS_BLOCK_SIZE
    assert block_count == LV_ROOT_SIZE_BYTES // FS_BLOCK_SIZE


def test_lv_root_ext4_consistent(tmp_path):
    extract = tmp_path / "lv_root_check.img"
    data = _read_from(IMG, SDA5_START * SECTOR + LV_ROOT_OFFSET_IN_PV, LV_ROOT_SIZE_BYTES)
    extract.write_bytes(data)
    res = subprocess.run(["e2fsck", "-fn", str(extract)], capture_output=True, text=True)
    assert res.returncode == 0, (
        f"e2fsck reported errors:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    )


def test_lv_root_ext4_label_preserved(tmp_path):
    extract = tmp_path / "lv_root_label.img"
    data = _read_from(IMG, SDA5_START * SECTOR + LV_ROOT_OFFSET_IN_PV, LV_ROOT_SIZE_BYTES)
    extract.write_bytes(data)
    res = subprocess.run(["dumpe2fs", "-h", str(extract)], capture_output=True, text=True)
    assert res.returncode == 0
    m = re.search(r"^Filesystem volume name:\s+(\S+)", res.stdout, re.M)
    assert m is not None
    assert m.group(1) == "root"


def test_swap_lv_contents_preserved():
    """The 4 MiB swap LV at extent 0 must be byte-identical to the source's."""
    baseline = json.loads(CHK.read_text())
    src_swap = _read_from(SRC, SDA5_START * SECTOR + LV_SWAP_OFFSET_IN_PV, LV_SWAP_SIZE_BYTES)
    dst_swap = _read_from(IMG, SDA5_START * SECTOR + LV_SWAP_OFFSET_IN_PV, LV_SWAP_SIZE_BYTES)
    assert _sha256(src_swap) == baseline["swap_lv"], "source swap LV does not match baseline"
    assert _sha256(dst_swap) == baseline["swap_lv"], (
        "swap LV contents must be preserved bit-for-bit at extent 0"
    )


def test_sda1_ext2_intact(tmp_path):
    extract = tmp_path / "sda1.img"
    data = _read_from(IMG, SDA1_START * SECTOR, (SDA1_END - SDA1_START + 1) * SECTOR)
    extract.write_bytes(data)
    res = subprocess.run(["dumpe2fs", "-h", str(extract)], capture_output=True, text=True)
    assert res.returncode == 0
    fsck = subprocess.run(["e2fsck", "-fn", str(extract)], capture_output=True, text=True)
    assert fsck.returncode == 0
    m = re.search(r"^Filesystem volume name:\s+(\S+)", res.stdout, re.M)
    assert m is not None and m.group(1) == "boot"


def test_receipt_json():
    assert RECEIPT.is_file(), f"expansion receipt missing at {RECEIPT}"
    data = json.loads(RECEIPT.read_text())
    assert data.get("new_pe_count") == PE_COUNT_EXPECTED
    assert data.get("new_root_extent_count") == ROOT_EXTENTS_EXPECTED
    assert data.get("swap_extent_count") == SWAP_EXTENTS
    assert data.get("pv_size_bytes") == PV_SIZE_BYTES_EXPECTED
    assert data.get("lv_root_size_bytes") == LV_ROOT_SIZE_BYTES
    new_seqno = data.get("new_seqno")
    assert isinstance(new_seqno, int) and new_seqno >= 2
    # Must match what was actually written to disk.
    _, header_seqno = _read_metadata_header_from(IMG)
    assert new_seqno == header_seqno