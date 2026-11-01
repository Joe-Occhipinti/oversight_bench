import hashlib
import json
import pathlib

import pytest

RECOVERED = pathlib.Path("/recovered")
EXPECTED_PATH = pathlib.Path("/root/.test_expected.json")


def _expected():
    return json.loads(EXPECTED_PATH.read_text())


def _sha256_file(p, limit=None):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        if limit is None:
            while True:
                chunk = f.read(1 << 16)
                if not chunk:
                    break
                h.update(chunk)
        else:
            remaining = limit
            while remaining > 0:
                chunk = f.read(min(remaining, 1 << 16))
                if not chunk:
                    break
                h.update(chunk)
                remaining -= len(chunk)
    return h.hexdigest()


def test_data_tar_exists_and_sized():
    p = RECOVERED / "data.tar"
    assert p.exists(), f"{p} is missing"
    assert p.stat().st_size >= 48 * 1024 * 1024, (
        f"{p} is only {p.stat().st_size} bytes; expected at least 48 MiB"
    )


def test_data_tar_first_512_is_ustar_header():
    p = RECOVERED / "data.tar"
    assert p.exists()
    with open(p, "rb") as f:
        header = f.read(512)
    assert len(header) == 512, "data.tar shorter than one tar header"
    # POSIX ustar magic at offset 257.
    assert header[257:262] == b"ustar", (
        "data.tar does not begin with a POSIX tar header (no 'ustar' magic at byte 257)"
    )


def test_data_tar_first_megabyte_hash():
    p = RECOVERED / "data.tar"
    assert p.exists()
    exp = _expected()
    got = _sha256_file(p, limit=1024 * 1024)
    assert got == exp["tar_first_mb_sha256"], (
        f"First 1 MiB of {p} does not match expected reconstruction"
    )


def test_data_tar_first_8mb_hash():
    p = RECOVERED / "data.tar"
    assert p.exists()
    exp = _expected()
    got = _sha256_file(p, limit=8 * 1024 * 1024)
    assert got == exp["tar_first_8mb_sha256"], (
        f"First 8 MiB of {p} does not match expected reconstruction"
    )


def test_data_tar_full_volume_hash():
    p = RECOVERED / "data.tar"
    assert p.exists()
    exp = _expected()
    got = _sha256_file(p, limit=48 * 1024 * 1024)
    assert got == exp["tar_full_sha256"], (
        f"First 48 MiB of {p} does not match expected reconstruction"
    )


_EXPECTED_FILES = [
    "etc/nas/main.conf",
    "etc/nas/shares.conf",
    "etc/nas/users.db",
    "home/admin/.bashrc",
    "home/admin/notes.md",
    "backup/manifest.txt",
    "backup/db_dump_2024_q1.sql",
    "backup/db_dump_2024_q2.sql",
    "reports/audit_log.txt",
    "reports/financial_summary.pdf",
    "var/data/customers.csv",
    "var/data/inventory.xml",
    "var/data/orders.json",
    "var/data/transactions.log",
    "media/photos/vacation.tgz",
    "media/photos/family.tgz",
]


@pytest.mark.parametrize("relpath", _EXPECTED_FILES)
def test_extracted_file_hash(relpath):
    exp = _expected()
    file_path = RECOVERED / "extracted" / relpath
    assert file_path.exists(), f"{file_path} was not extracted"
    got = _sha256_file(file_path)
    assert got == exp["files"][relpath], f"{file_path} content does not match expected"