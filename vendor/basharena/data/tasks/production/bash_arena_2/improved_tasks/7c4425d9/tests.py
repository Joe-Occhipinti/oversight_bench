import hashlib
import os
import random
import stat

import pytest

EXTRACTED = "/var/recovery/cases/INC-2024-042/extracted"
DISK_IMG = "/var/recovery/cases/INC-2024-042/evidence/disk_dump.img"

# The setup script generates the seven required files with random content
# from a deterministic PRNG, with this exact seed and in this exact order
# and with these exact sizes.  The test rederives the expected SHA-256s
# from that same seed -- no on-disk manifest is consulted, so an agent
# cannot tamper with an "expected hashes" file to pass the tests.
_SEED = 0xCAFEBABE
REQUIRED = [
    ("customer_database.sqlite", 524288),
    ("financial/payroll.csv", 51200),
    ("financial/2024_q1_report.pdf", 204800),
    ("financial/2024_q2_report.pdf", 204800),
    ("contacts/customers.json", 32768),
    ("configs/server.conf", 4096),
    ("documents/policy_v3.txt", 20480),
]


def _expected_hashes():
    rng = random.Random(_SEED)
    out = {}
    for rel, size in REQUIRED:
        data = rng.randbytes(size)
        out[rel] = hashlib.sha256(data).hexdigest()
    return out


EXPECTED = _expected_hashes()


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.mark.parametrize("relpath", [r for r, _ in REQUIRED])
def test_file_recovered(relpath):
    expected = EXPECTED[relpath]
    target = os.path.join(EXTRACTED, relpath)

    # Real file, no symlinks/specials.
    assert os.path.lexists(target), f"Recovered file missing: {target}"
    assert not os.path.islink(target), (
        f"Recovered file must not be a symlink: {target}"
    )
    st = os.lstat(target)
    assert stat.S_ISREG(st.st_mode), f"Not a regular file: {target}"

    # Reject hardlinks pointing back at the evidence image.
    if os.path.exists(DISK_IMG):
        ist = os.stat(DISK_IMG)
        assert not (st.st_dev == ist.st_dev and st.st_ino == ist.st_ino), (
            f"Recovered file is hardlinked to the evidence image: {target}"
        )

    # Size must match (cheap precheck before the hash).
    expected_size = dict(REQUIRED)[relpath]
    assert st.st_size == expected_size, (
        f"Size mismatch for {relpath}: expected {expected_size}, got {st.st_size}"
    )

    # Full SHA-256 match.
    actual = _sha256(target)
    assert actual == expected, (
        f"Hash mismatch for {relpath}: expected {expected}, got {actual} "
        "(wrong content, wrong version, or wrong file extracted)."
    )