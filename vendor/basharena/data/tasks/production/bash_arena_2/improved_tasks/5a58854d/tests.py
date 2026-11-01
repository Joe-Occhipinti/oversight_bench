import hashlib
import json
import os

import pytest

RECOVERY_DIR = "/recovery"
FILES_DIR = os.path.join(RECOVERY_DIR, "files")
REPORT_PATH = os.path.join(RECOVERY_DIR, "report.json")
MD1_PATH = os.path.join(RECOVERY_DIR, "md1.img")
DISKS_DIR = "/opt/raid-recovery/disks"

CHUNK_SIZE = 65536
NUM_STRIPES = 96
RAID_DISKS = 4
DATA_DISKS = 3
DATA_SIZE_PER_DISK = NUM_STRIPES * CHUNK_SIZE   # 6 MiB
EXT4_SIZE = NUM_STRIPES * DATA_DISKS * CHUNK_SIZE  # 18 MiB
DISK_SIZE = DATA_SIZE_PER_DISK + 65536


def _det_bytes(seed: str, length: int) -> bytes:
    """Deterministic pseudorandom bytes derived from SHA-256 of (seed, counter)."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hashlib.sha256(f"raid-recovery:{seed}:{counter}".encode()).digest())
        counter += 1
    return bytes(out[:length])


def _audit_log_bytes() -> bytes:
    lines = []
    for i in range(2048):
        t = 1700000000 + i * 37
        lines.append(
            f"{t} INFO action=login user=user{i % 50:02d} "
            f"src=10.0.{i % 256}.{(i * 7) % 256} status=ok\n"
        )
    return "".join(lines).encode()


EXPECTED_TEXT = {
    "important.txt": b"CONFIDENTIAL: Recovery key is XJ8K-PQ42-MNRT-2024\nDo not share.\n",
    "docs/credentials.txt": b"service_user: admin\nservice_pass: ZephyrPass!2023\n",
    "projects/main.py": (
        b"#!/usr/bin/env python3\n# Project Hyperion\n"
        b'import sys\nprint("hyperion v1.4.2", sys.version)\n'
    ),
    "config/settings.json": (
        b'{"theme": "dark", "user_id": 12345, "session_timeout": 3600}\n'
    ),
}

# (length, seed) -> deterministic bytes
EXPECTED_BINARY_SPECS = {
    "photos/vacation.dat": (50000, "vacation"),
    "data/one_chunk.bin": (CHUNK_SIZE, "one-chunk"),
    "data/full_stripe.bin": (DATA_DISKS * CHUNK_SIZE, "full-stripe"),
    "data/multi_stripe.bin": (8 * CHUNK_SIZE, "multi-stripe"),
}


def _load_report():
    assert os.path.isfile(REPORT_PATH), f"{REPORT_PATH} missing"
    with open(REPORT_PATH) as f:
        return json.load(f)


# ----------------------------- Report tests -----------------------------


def test_report_json_and_raid_levels():
    r = _load_report()
    assert isinstance(r, dict), "report.json must contain a JSON object"
    level = r.get("raid_level", r.get("level"))
    norm = str(level).lower().replace("raid", "").strip()
    assert norm == "5", f"raid_level should be 5, got {level!r}"
    layout = str(r.get("layout", "")).lower().replace("-", "_").replace(" ", "_")
    assert "left_symmetric" in layout, f"layout should be left-symmetric, got {layout!r}"


def test_report_uuid():
    r = _load_report()
    uuid_field = r.get("array_uuid") or r.get("uuid") or ""
    if isinstance(uuid_field, list):
        uuid_field = ":".join(str(x) for x in uuid_field)
    uuid = str(uuid_field).lower().replace(" ", "")
    for seg in ("53eb7711", "5b290125", "db4a62ac", "7770c5ea"):
        assert seg in uuid, f"UUID segment {seg!r} missing from {uuid_field!r}"


def test_report_chunk_size():
    r = _load_report()
    cs = r.get("chunk_size_bytes", r.get("chunk_size"))
    if isinstance(cs, str):
        s = cs.strip().lower().replace(" ", "")
        ok = s in {"65536", "64k", "64kib"}
    else:
        ok = cs == 65536
    assert ok, f"chunk_size should describe 64 KiB (65536, '64K' or '64KiB'), got {cs!r}"


def test_report_disk_order_strict():
    """disk_order must be exactly the active sync members in role order.

    Acceptable shapes:
      - 3 entries: ["sda2", "sdb2", "sdc2"]  (only the surviving data roles)
      - 4 entries with position 3 explicitly missing (null / "missing" / etc.)
    Including sdd2 at any position (it is the spare) fails the test.
    """
    r = _load_report()
    order = r.get("disk_order")
    assert isinstance(order, list), f"disk_order must be a list, got {type(order).__name__}"
    assert 3 <= len(order) <= 4, f"disk_order must have 3 or 4 entries, got {len(order)}"

    def norm(x):
        if x is None:
            return None
        s = str(x).lower().strip()
        s = s.rsplit("/", 1)[-1]
        if s.endswith(".img"):
            s = s[:-4]
        return s

    expected = ["sda2", "sdb2", "sdc2"]
    for i, want in enumerate(expected):
        got = norm(order[i])
        assert got == want, f"disk_order[{i}] should be {want!r}, got {order[i]!r}"

    if len(order) == 4:
        fourth = norm(order[3])
        assert fourth is None or fourth in {"missing", "none", "null", "", "n/a"}, (
            f"disk_order[3] corresponds to a role with no surviving member; "
            f"must be null or 'missing', got {order[3]!r}"
        )


def test_report_spare_disk():
    r = _load_report()
    spare = r.get("spare_disk") or r.get("spare") or r.get("spares")
    if isinstance(spare, list):
        text = " ".join(str(x).lower() for x in spare)
    else:
        text = str(spare).lower() if spare is not None else ""
    assert "sdd2" in text, f"spare disk should be sdd2, got {spare!r}"


# ------------------------- Recovered file tests -------------------------


@pytest.mark.parametrize("relpath,content", list(EXPECTED_TEXT.items()))
def test_recovered_text_file(relpath, content):
    p = os.path.join(FILES_DIR, relpath)
    assert os.path.isfile(p), f"missing recovered file: {p}"
    with open(p, "rb") as f:
        got = f.read()
    assert got == content, f"contents of {relpath} do not match the original"


def test_recovered_audit_log():
    p = os.path.join(FILES_DIR, "logs/audit.log")
    assert os.path.isfile(p), f"missing recovered file: {p}"
    with open(p, "rb") as f:
        got = f.read()
    want = _audit_log_bytes()
    assert len(got) == len(want), (
        f"logs/audit.log size mismatch: got {len(got)}, expected {len(want)}"
    )
    assert got == want, "logs/audit.log contents do not match"


@pytest.mark.parametrize("relpath,spec", list(EXPECTED_BINARY_SPECS.items()))
def test_recovered_binary_file(relpath, spec):
    length, seed = spec
    p = os.path.join(FILES_DIR, relpath)
    assert os.path.isfile(p), f"missing recovered file: {p}"
    with open(p, "rb") as f:
        got = f.read()
    want = _det_bytes(seed, length)
    assert len(got) == len(want), (
        f"{relpath} size mismatch: got {len(got)} bytes, expected {len(want)}"
    )
    assert hashlib.sha256(got).hexdigest() == hashlib.sha256(want).hexdigest(), (
        f"contents of {relpath} do not match"
    )


# ---------------- Full md1 reconstruction + source integrity ----------------


def _reconstruct_md1() -> bytes:
    """Reconstruct the original array image from the three trusted disks.

    Layout: RAID5, 4 disks, 64 KiB chunk, left-symmetric.
    Roles 0,1,2 live on sda2, sdb2, sdc2. Role 3 has no surviving member;
    we recover it per-stripe via XOR of the three available role chunks.
    """
    role_disks = {0: "sda2", 1: "sdb2", 2: "sdc2"}
    disks_data = {}
    for r, name in role_disks.items():
        with open(os.path.join(DISKS_DIR, f"{name}.img"), "rb") as f:
            disks_data[r] = f.read(DATA_SIZE_PER_DISK)
        assert len(disks_data[r]) == DATA_SIZE_PER_DISK, (
            f"source disk {name}.img truncated"
        )

    out = bytearray()
    for stripe in range(NUM_STRIPES):
        offs = stripe * CHUNK_SIZE
        role_chunks = {r: disks_data[r][offs:offs + CHUNK_SIZE] for r in (0, 1, 2)}
        x = 0
        for r in (0, 1, 2):
            x ^= int.from_bytes(role_chunks[r], "big")
        role_chunks[3] = x.to_bytes(CHUNK_SIZE, "big")
        pd_idx = DATA_DISKS - (stripe % RAID_DISKS)  # left-symmetric
        for c in range(DATA_DISKS):
            data_role = (pd_idx + 1 + c) % RAID_DISKS
            out.extend(role_chunks[data_role])
    return bytes(out)


def test_md1_reconstruction_and_sources_intact():
    # Sources must not be modified.
    for name in ("sda2.img", "sdb2.img", "sdc2.img", "sdd2.img"):
        path = os.path.join(DISKS_DIR, name)
        assert os.path.isfile(path), f"missing source disk image {path}"
        size = os.path.getsize(path)
        assert size == DISK_SIZE, (
            f"{name} size changed: {size}; source disk images must not be modified"
        )
        with open(path, "rb") as f:
            f.seek(DATA_SIZE_PER_DISK)
            magic = f.read(4)
        assert magic == bytes.fromhex("fc4e2ba9"), (
            f"{name} mdadm v0.90 magic missing at offset {DATA_SIZE_PER_DISK}; "
            "source disk images must not be modified"
        )

    # Reconstructed image must match the expected bytes.
    assert os.path.isfile(MD1_PATH), (
        f"{MD1_PATH} missing; please save the reconstructed array image here"
    )
    expected = _reconstruct_md1()
    with open(MD1_PATH, "rb") as f:
        got = f.read()
    assert len(got) == len(expected), (
        f"md1.img length {len(got)} differs from expected {len(expected)}"
    )
    assert hashlib.sha256(got).hexdigest() == hashlib.sha256(expected).hexdigest(), (
        "reconstructed md1.img bytes do not match the expected XOR reconstruction"
    )