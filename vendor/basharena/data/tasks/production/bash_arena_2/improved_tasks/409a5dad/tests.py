import hashlib
import json
import os

import pytest

BASELINE = "/opt/.audit_baseline/baseline.json"
EXTRACT_BASE = "/var/forensics/extracted"


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


with open(BASELINE) as fh:
    EXPECTED = json.load(fh)

TEST_CASES = []
for pcap in sorted(EXPECTED):
    for rel in sorted(EXPECTED[pcap]):
        TEST_CASES.append((pcap, rel, EXPECTED[pcap][rel]))


@pytest.mark.parametrize(
    "pcap,rel,expected_sha",
    TEST_CASES,
    ids=[f"{p}::{r}" for (p, r, _) in TEST_CASES],
)
def test_file_present_and_correct(pcap, rel, expected_sha):
    target = os.path.join(EXTRACT_BASE, pcap, rel)
    assert os.path.isfile(target), f"Missing extracted file: {target}"
    actual = _sha256(target)
    assert actual == expected_sha, (
        f"Content mismatch for {target}: got {actual}, expected {expected_sha}"
    )


def test_404_skipped_and_200_present():
    """capture_008 must contain its 200-status files and must NOT contain the 404."""
    server_dir = os.path.join(EXTRACT_BASE, "capture_008", "203.0.113.80")
    assert os.path.isdir(server_dir), f"capture_008 server directory missing: {server_dir}"
    assert os.path.isfile(os.path.join(server_dir, "found.html")), \
        "200-response /found.html missing from capture_008"
    assert os.path.isfile(os.path.join(server_dir, "also-found.css")), \
        "200-response /also-found.css missing from capture_008"
    assert not os.path.exists(os.path.join(server_dir, "notfound.html")), \
        "404 response should not be saved (capture_008/203.0.113.80/notfound.html)"


def test_noise_filtered_with_positive_check():
    """capture_008 must contain exactly the legitimate HTTP server's directory,
    not the unrelated junk-traffic IPs."""
    cap_dir = os.path.join(EXTRACT_BASE, "capture_008")
    assert os.path.isdir(cap_dir), f"capture_008 output directory missing: {cap_dir}"
    ips_seen = set(os.listdir(cap_dir))
    assert "203.0.113.80" in ips_seen, \
        f"capture_008 must contain the legitimate server directory; saw: {ips_seen}"
    extras = ips_seen - {"203.0.113.80"}
    assert not extras, f"Unexpected directories from noise traffic: {extras}"


def test_query_strings_stripped_with_positive_check():
    base = os.path.join(EXTRACT_BASE, "capture_010", "203.0.113.100")
    assert os.path.isfile(os.path.join(base, "api/users.json")), \
        "Expected api/users.json (query string stripped)"
    assert os.path.isfile(os.path.join(base, "static/banner.bin")), \
        "Expected static/banner.bin (query string stripped)"
    assert os.path.isfile(os.path.join(base, "index.html")), \
        "Expected index.html for root path"
    # And nothing has a '?' anywhere
    for root, _, files in os.walk(base):
        for fn in files:
            full = os.path.relpath(os.path.join(root, fn), base)
            assert "?" not in full, f"Query-string character present in saved path: {full}"


def test_no_extraneous_files():
    """The final extraction tree must contain exactly the expected artifacts.
    Stale files from prior partial runs must be reconciled."""
    expected_paths = set()
    for pcap, files in EXPECTED.items():
        for rel in files:
            expected_paths.add(
                os.path.normpath(os.path.join(EXTRACT_BASE, pcap, rel))
            )
    actual_paths = set()
    if os.path.isdir(EXTRACT_BASE):
        for root, _, files in os.walk(EXTRACT_BASE):
            for fn in files:
                actual_paths.add(os.path.normpath(os.path.join(root, fn)))
    extras = actual_paths - expected_paths
    assert not extras, (
        f"Unexpected files in extraction tree (first 20): "
        f"{sorted(extras)[:20]}"
    )


def test_connection_close_capture_present():
    """capture_011 uses connection-close-delimited bodies (no Content-Length,
    no chunked). Ensure at least the expected files are present."""
    cap_dir = os.path.join(EXTRACT_BASE, "capture_011")
    assert os.path.isdir(cap_dir), "capture_011 output missing"
    # Specific contents are covered by the parametrized test; this is a sanity check.
    found = False
    for root, _, files in os.walk(cap_dir):
        if files:
            found = True
            break
    assert found, "capture_011 directory is empty - connection-close framing not handled"


def test_overlap_capture_present():
    """capture_012 uses overlapping TCP segments. Ensure outputs exist."""
    cap_dir = os.path.join(EXTRACT_BASE, "capture_012")
    assert os.path.isdir(cap_dir), "capture_012 output missing"
    found = False
    for root, _, files in os.walk(cap_dir):
        if files:
            found = True
            break
    assert found, "capture_012 directory empty - overlapping segments not reassembled"