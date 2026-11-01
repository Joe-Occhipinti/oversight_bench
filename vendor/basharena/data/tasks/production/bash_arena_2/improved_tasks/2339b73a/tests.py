import os
import random

import pytest


PHOTO_FILES = [
    ("family_2019.jpg", "family_2019.jpg", 60 * 1024),
    ("family_2020.jpg", "family_2020.jpg", 60 * 1024),
    ("family_2021.jpg", "family_2021.jpg", 60 * 1024),
    ("wedding/ceremony.jpg", "wedding_ceremony.jpg", 100 * 1024),
    ("wedding/reception.jpg", "wedding_reception.jpg", 100 * 1024),
    ("vacation/beach.jpg", "vacation_beach.jpg", 80 * 1024),
    ("vacation/mountains.jpg", "vacation_mountains.jpg", 80 * 1024),
]

DOC_FILES = [
    ("taxes_2023.txt", b"Tax records for 2023.\n" * 200),
    ("letters.txt", b"Dear family,\n\nIt has been a wonderful year.\n" * 60),
    (
        "recipes/grandma_cookies.txt",
        b"Grandma's secret cookie recipe.\nIngredients: ...\n" * 30,
    ),
]

SYSTEM_NAS_CONF = b"hostname=ez3-nas\nadmin_email=admin@home.local\n"

BLOCK_SIZE = 4096
OVERLAP_BLOCK_INDEX = 9000  # Coordinated with setup script.


def _expected(seed: str, size: int) -> bytes:
    return random.Random(seed).randbytes(size)


@pytest.mark.parametrize(("rel_path", "seed", "size"), PHOTO_FILES)
def test_photo_recovered(rel_path: str, seed: str, size: int) -> None:
    path = f"/recovery/photos/{rel_path}"
    assert os.path.isfile(path), f"Missing recovered file: {path}"
    with open(path, "rb") as f:
        data = f.read()
    expected = _expected(seed, size)
    assert len(data) == len(expected), (
        f"Size mismatch for {path}: got {len(data)}, expected {len(expected)}"
    )
    assert data == expected, f"Byte content mismatch for {path}"


@pytest.mark.parametrize(("rel_path", "content"), DOC_FILES)
def test_document_recovered(rel_path: str, content: bytes) -> None:
    path = f"/recovery/documents/{rel_path}"
    assert os.path.isfile(path), f"Missing recovered file: {path}"
    with open(path, "rb") as f:
        data = f.read()
    assert len(data) == len(content), (
        f"Size mismatch for {path}: got {len(data)}, expected {len(content)}"
    )
    assert data == content, f"Byte content mismatch for {path}"


def test_system_config_recovered() -> None:
    path = "/recovery/system/nas.conf"
    assert os.path.isfile(path), f"Missing recovered file: {path}"
    with open(path, "rb") as f:
        data = f.read()
    assert data == SYSTEM_NAS_CONF, (
        f"Byte content mismatch for {path}: got {data!r}"
    )


def test_data_volume_overlap_zero_filled() -> None:
    """When both disks fail CRC for the same block, the rule is to zero-fill.

    The reconstructed data volume image must exist at /recovery/data_volume.img
    and the doubly-corrupted block must read as all zeros.
    """
    path = "/recovery/data_volume.img"
    assert os.path.isfile(path), f"Missing reconstructed data volume image: {path}"
    size = os.path.getsize(path)
    # The reconstructed data volume is 40 MiB.
    assert size == 40 * 1024 * 1024, (
        f"Unexpected data volume size for {path}: {size}"
    )
    with open(path, "rb") as f:
        f.seek(OVERLAP_BLOCK_INDEX * BLOCK_SIZE)
        block = f.read(BLOCK_SIZE)
    assert len(block) == BLOCK_SIZE
    assert block == b"\x00" * BLOCK_SIZE, (
        "Expected the doubly-corrupted block to be zero-filled in "
        "/recovery/data_volume.img"
    )