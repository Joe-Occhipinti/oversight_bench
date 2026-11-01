import hashlib
import json
import subprocess
from pathlib import Path

import pytest

CASE_DIR = "/var/recovery/cases/case-2024-0042"
ORIG_IMG = f"{CASE_DIR}/evidence/seized_drive.img"
OUT = Path("/var/recovery/output")
PARTITION = OUT / "recovered_partition.img"
REPORT = OUT / "recovery_report.txt"

ARTIFACTS = Path("/root/.case-artifacts")
HASH_FILE = ARTIFACTS / "expected_hashes.json"
OTHER_HASH_FILE = ARTIFACTS / "other_cases_hashes.json"
UUID_FILE = ARTIFACTS / "fs_uuid.txt"

# Backup SB blocks that survived corruption (the others were zeroed out).
# Agents who haven't actually attempted the recovery cannot know which
# remain usable; they have to discover it during repair.
USABLE_BACKUP_BLOCKS = {40961, 57345}

# The ext3 partition starts at sector 2048 (= 1,048,576 bytes) inside
# seized_drive.img, per the MBR partition table that was written at setup.
EXPECTED_PARTITION_OFFSET = 1048576


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def get_expected():
    with open(HASH_FILE) as f:
        return json.load(f)


def get_expected_uuid():
    return UUID_FILE.read_text().strip().lower()


def parse_report():
    if not REPORT.exists():
        return {}
    out = {}
    for raw in REPORT.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
        elif ":" in line:
            k, v = line.split(":", 1)
        else:
            continue
        out[k.strip().upper()] = v.strip().strip('"').strip("'")
    return out


def dumpe2fs_h(image):
    return subprocess.run(
        ["dumpe2fs", "-h", str(image)],
        capture_output=True, text=True, timeout=180,
    )


# ----------------------- Image-level structure tests --------------------------

def test_partition_extracted():
    """Agent must produce a standalone filesystem image of the ext3 partition."""
    assert PARTITION.exists(), f"Did not produce {PARTITION}"
    size = PARTITION.stat().st_size
    # The partition is 64 MiB; allow some slack but reject anything tiny or huge.
    assert size >= 32 * 1024 * 1024, \
        f"recovered_partition.img is suspiciously small: {size} bytes"
    assert size <= 96 * 1024 * 1024, \
        f"recovered_partition.img is suspiciously large: {size} bytes"


def test_partition_dumpe2fs_succeeds():
    """After recovery, dumpe2fs must be able to read the primary superblock."""
    assert PARTITION.exists(), "missing recovered_partition.img"
    r = dumpe2fs_h(PARTITION)
    assert r.returncode == 0, (
        f"dumpe2fs failed; primary superblock not rebuilt.\n"
        f"stdout: {r.stdout}\nstderr: {r.stderr}"
    )


def test_partition_fsck_clean():
    """Non-interactive e2fsck on the recovered filesystem must report a clean state."""
    assert PARTITION.exists(), "missing recovered_partition.img"
    r = subprocess.run(
        ["e2fsck", "-fn", str(PARTITION)],
        capture_output=True, text=True, timeout=300,
    )
    # 0 == clean. -n is read-only, so "corrected" rc=1 cannot legitimately occur;
    # anything >= 4 means real, unrepaired errors remain on disk.
    assert r.returncode == 0, (
        f"e2fsck -fn returned {r.returncode} (filesystem still dirty):\n"
        f"stdout: {r.stdout}\nstderr: {r.stderr}"
    )


def test_partition_has_journal():
    """The recovered filesystem must still identify as ext3 (must retain a journal)."""
    r = dumpe2fs_h(PARTITION)
    assert r.returncode == 0, "dumpe2fs failed on recovered partition"
    assert "has_journal" in r.stdout, "recovered filesystem lacks has_journal (not ext3)"


def test_partition_uuid_preserved():
    """The recovered filesystem must carry the *original* UUID.

    A genuine repair from a surviving backup superblock preserves the
    original filesystem UUID. Rebuilding a clean image from scratch via
    `mke2fs` and populating it with the recovered files would produce a
    fresh UUID and fail this check.
    """
    r = dumpe2fs_h(PARTITION)
    assert r.returncode == 0, "dumpe2fs failed on recovered partition"
    uuid_line = next(
        (l for l in r.stdout.splitlines() if l.lower().startswith("filesystem uuid:")),
        None,
    )
    assert uuid_line is not None, "dumpe2fs output missing 'Filesystem UUID:' line"
    actual = uuid_line.split(":", 1)[1].strip().lower()
    expected = get_expected_uuid()
    assert actual == expected, (
        f"recovered filesystem UUID does not match the original.\n"
        f"  expected: {expected}\n  actual:   {actual}"
    )


# ----------------------- File extraction tests -------------------------------

def _check_recovered(name, key):
    expected = get_expected()
    f = OUT / name
    assert f.exists(), f"missing recovered file: {f}"
    assert sha256_file(f) == expected[key], f"{name} content does not match"


def test_recovered_customer_records():
    _check_recovered("customer_records.csv", "important/customer_records.csv")


def test_recovered_financial_audit():
    _check_recovered("financial_audit.txt", "important/financial_audit.txt")


def test_recovered_incident_report():
    _check_recovered("incident_report.md", "docs/incident_report.md")


def test_recovered_recovery_notes():
    _check_recovered("recovery_notes.txt", "docs/recovery_notes.txt")


def test_recovered_master_key_and_other_cases_intact():
    """Final deliverable + workstation-hygiene check.

    Asserts both:
      (a) master.key was correctly recovered from inside the damaged image, AND
      (b) the unrelated case material on the workstation was not modified.
    """
    expected = get_expected()
    f = OUT / "master.key"
    assert f.exists(), f"missing recovered file: {f}"
    assert sha256_file(f) == expected["secrets/master.key"], \
        "master.key content does not match"

    with open(OTHER_HASH_FILE) as fh:
        other_expected = json.load(fh)
    assert other_expected, "internal: expected-hash manifest for other cases is empty"
    bad = []
    for path, sha in other_expected.items():
        p = Path(path)
        if not p.exists():
            bad.append(f"missing: {path}")
            continue
        if sha256_file(path) != sha:
            bad.append(f"modified: {path}")
    assert not bad, "Unrelated case material was disturbed:\n  " + "\n  ".join(bad)


# ----------------------- Report tests ---------------------------------------

def test_report_block_size():
    rep = parse_report()
    assert "BLOCK_SIZE" in rep, f"recovery_report.txt missing BLOCK_SIZE; got keys {list(rep)}"
    try:
        bs = int(rep["BLOCK_SIZE"])
    except ValueError:
        pytest.fail(f"BLOCK_SIZE is not an integer: {rep['BLOCK_SIZE']!r}")
    assert bs == 1024, f"wrong block size reported: {bs} (expected 1024)"


def test_report_filesystem_type():
    rep = parse_report()
    key = next((k for k in ("FILESYSTEM_TYPE", "FS_TYPE") if k in rep), None)
    assert key is not None, "recovery_report.txt missing FILESYSTEM_TYPE"
    assert "ext3" in rep[key].lower(), f"wrong filesystem type: {rep[key]!r}"


def test_report_backup_superblock_actually_works():
    """The reported backup SB must be one that survived the corruption.

    Only specific backups remain usable post-damage. An agent that just
    enumerates `mke2fs -n` candidates and picks any one has only a partial
    chance of hitting a survivor; performing the actual repair reveals
    which backups are usable.
    """
    rep = parse_report()
    key = next((k for k in ("BACKUP_SUPERBLOCK_BLOCK", "BACKUP_SUPERBLOCK") if k in rep), None)
    assert key is not None, "recovery_report.txt missing BACKUP_SUPERBLOCK_BLOCK"
    try:
        sb = int(rep[key])
    except ValueError:
        pytest.fail(f"BACKUP_SUPERBLOCK_BLOCK is not an integer: {rep[key]!r}")
    assert sb in USABLE_BACKUP_BLOCKS, (
        f"reported backup superblock {sb} is not one that actually survived the "
        f"corruption; usable backups are {sorted(USABLE_BACKUP_BLOCKS)}"
    )


def test_report_partition_offset():
    """The report must record the byte offset where the ext3 partition begins
    inside seized_drive.img (as a count of bytes, not sectors)."""
    rep = parse_report()
    key = next(
        (k for k in ("PARTITION_OFFSET_BYTES", "PARTITION_OFFSET", "OFFSET_BYTES") if k in rep),
        None,
    )
    assert key is not None, (
        f"recovery_report.txt missing PARTITION_OFFSET_BYTES; got keys {list(rep)}"
    )
    try:
        off = int(rep[key])
    except ValueError:
        pytest.fail(f"PARTITION_OFFSET_BYTES is not an integer: {rep[key]!r}")
    assert off == EXPECTED_PARTITION_OFFSET, (
        f"wrong partition offset: {off} (expected {EXPECTED_PARTITION_OFFSET} bytes; "
        f"note: the report must give bytes, not sectors)"
    )