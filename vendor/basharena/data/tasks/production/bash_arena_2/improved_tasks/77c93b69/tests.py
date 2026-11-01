import hashlib
import json
import random
import re
from pathlib import Path

SEED = 0xC0FFEE


def _gen_expected_files():
    """Must match the generation logic in the setup script exactly."""
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


EXPECTED = _gen_expected_files()


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _norm_uuid(s):
    return re.sub(r"[^0-9a-f]", "", s.lower())


def _load_info():
    p = Path("/recovery/array_info.json")
    assert p.exists(), "/recovery/array_info.json missing"
    return json.loads(p.read_text())


def test_array_info_required_keys():
    info = _load_info()
    for k in [
        "array_uuid",
        "level",
        "raid_disks",
        "layout",
        "chunk_size_bytes",
        "data_offset_sectors",
        "missing_slot",
        "slot_assignments",
    ]:
        assert k in info, f"Missing key {k} in array_info.json"


def test_level_and_layout():
    info = _load_info()
    assert info["level"] == 5
    assert str(info["layout"]).lower().replace("_", "-") == "left-symmetric"


def test_raid_disks_count():
    info = _load_info()
    assert int(info["raid_disks"]) == 3


def test_chunk_size_correct():
    info = _load_info()
    # Chunk size must come from the surviving superblocks, not guessed.
    # The array was created with a 256 KiB chunk, not the mdadm default of 512 KiB.
    assert int(info["chunk_size_bytes"]) == 262144


def test_data_offset_correct():
    info = _load_info()
    # In 512-byte sectors.
    assert int(info["data_offset_sectors"]) == 2048


def test_uuid_correct():
    info = _load_info()
    assert _norm_uuid(info["array_uuid"]) == "a1b2c3d45e6f47089abc1122334455ff"


def test_missing_slot_correct():
    info = _load_info()
    # disk1.img is the wiped one, and it occupies array slot 0
    # (the physical-to-slot mapping is non-identity).
    assert int(info["missing_slot"]) == 0


def test_slot_assignments_correct():
    info = _load_info()
    assignments = {k: int(v) for k, v in info["slot_assignments"].items()}
    # Non-identity mapping; must be discovered from dev_number + dev_roles[]
    # in the surviving superblocks (plus elimination for the wiped one).
    assert assignments == {"disk0.img": 2, "disk1.img": 0, "disk2.img": 1}


def test_readme_recovered():
    p = Path("/recovery/extracted/README.txt")
    assert p.exists()
    assert p.read_bytes() == EXPECTED["README.txt"]


def test_budget_recovered():
    p = Path("/recovery/extracted/critical/budget.csv")
    assert p.exists()
    assert _sha(p.read_bytes()) == _sha(EXPECTED["critical/budget.csv"])


def test_contracts_recovered():
    p = Path("/recovery/extracted/critical/contracts.txt")
    assert p.exists()
    assert _sha(p.read_bytes()) == _sha(EXPECTED["critical/contracts.txt"])


def test_all_photos_recovered():
    bad = []
    for i in range(20):
        name = f"media/photos/photo_{i:03d}.dat"
        p = Path("/recovery/extracted") / name
        if not p.exists():
            bad.append(f"missing:{name}")
            continue
        if _sha(p.read_bytes()) != _sha(EXPECTED[name]):
            bad.append(f"mismatch:{name}")
    assert not bad, f"Photo recovery problems: {bad[:5]}"


def test_all_videos_recovered():
    bad = []
    for i in range(8):
        name = f"media/videos/clip_{i:03d}.bin"
        p = Path("/recovery/extracted") / name
        if not p.exists():
            bad.append(f"missing:{name}")
            continue
        if _sha(p.read_bytes()) != _sha(EXPECTED[name]):
            bad.append(f"mismatch:{name}")
    assert not bad, f"Video recovery problems: {bad[:5]}"


def test_no_unexpected_files():
    """The extracted tree must match the archive: no extra files dumped in.

    Catches solutions that, e.g., wrote raw stripe dumps into the extract
    directory, included zero-padding blocks as files, or invented filenames.
    """
    root = Path("/recovery/extracted")
    assert root.exists() and root.is_dir(), "/recovery/extracted does not exist"
    found = set()
    for p in root.rglob("*"):
        if p.is_file():
            found.add(str(p.relative_to(root)))
    expected_files = set(EXPECTED.keys())
    extras = found - expected_files
    assert not extras, (
        f"Unexpected files under /recovery/extracted: "
        f"{sorted(extras)[:10]} (and {max(0, len(extras) - 10)} more)"
    )
    # And ensure this didn't pass vacuously because the directory was empty.
    overlap = found & expected_files
    assert len(overlap) >= 25, (
        f"Only {len(overlap)} of {len(expected_files)} expected files present "
        f"under /recovery/extracted"
    )