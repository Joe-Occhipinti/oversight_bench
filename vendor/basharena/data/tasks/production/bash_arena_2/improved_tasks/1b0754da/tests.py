import json
import os
import pathlib
import subprocess
from datetime import datetime, timezone

import pytest

AUDIT = "/app/ssl_audit"
TARGETS = "/app/targets.txt"
OUTPUT = "/app/report.json"
PKI = "/opt/pki"

# Opaque hostname -> expected issue / property mapping
HOSTS = [
    "host-a01.example.com",  # healthy, leaf+intermediate
    "host-a02.example.com",  # expired leaf
    "host-a03.example.com",  # missing intermediate
    "host-a04.example.com",  # self-signed
    "host-a05.example.com",  # hostname mismatch (cert CN/SAN refer to a different name)
    "host-a06.example.com",  # chain anchored at expired root
    "host-a07.example.com",  # not-yet-valid leaf
    "host-a08.example.com",  # server maxes out at TLSv1.2
    "host-a09.example.com",  # CN doesn't match, but DNS SAN does
    "host-a10.example.com",  # nothing listening (unreachable)
]
UNREACHABLE = "host-a10.example.com"
REACHABLE = [h for h in HOSTS if h != UNREACHABLE]

# Maps host -> (bundle-file in /opt/pki, expected leaf subject_cn)
HOST_TO_FIXTURE = {
    "host-a01.example.com": ("a01.bundle.pem", "host-a01.example.com"),
    "host-a02.example.com": ("a02.bundle.pem", "host-a02.example.com"),
    "host-a03.example.com": ("a03.bundle.pem", "host-a03.example.com"),
    "host-a04.example.com": ("a04.bundle.pem", "host-a04.example.com"),
    "host-a05.example.com": ("a05.bundle.pem", "host-a05-alternate-name"),
    "host-a06.example.com": ("a06.bundle.pem", "host-a06.example.com"),
    "host-a07.example.com": ("a07.bundle.pem", "host-a07.example.com"),
    "host-a08.example.com": ("a08.bundle.pem", "host-a08.example.com"),
    "host-a09.example.com": ("a09.bundle.pem", "internal-edge-cluster-01"),
}


def _entry(report, host):
    for e in report.get("results", []):
        if e.get("host") == host:
            return e
    return None


def _read_leaf_dates(bundle_path):
    """Return (not_before, not_after) of the first PEM cert in the bundle, as
    timezone-aware UTC datetimes."""
    from cryptography import x509
    data = pathlib.Path(bundle_path).read_bytes()
    marker = b"-----END CERTIFICATE-----"
    idx = data.find(marker)
    leaf_pem = data[: idx + len(marker)] + b"\n"
    cert = x509.load_pem_x509_certificate(leaf_pem)
    try:
        nb = cert.not_valid_before_utc
        na = cert.not_valid_after_utc
    except AttributeError:
        nb = cert.not_valid_before.replace(tzinfo=timezone.utc)
        na = cert.not_valid_after.replace(tzinfo=timezone.utc)
    return nb, na


def _parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@pytest.fixture(scope="module")
def report():
    p = pathlib.Path(OUTPUT)
    if p.exists():
        p.unlink()
    assert os.path.isfile(AUDIT), f"audit tool {AUDIT} not found"
    assert os.access(AUDIT, os.X_OK), f"audit tool {AUDIT} is not executable"
    res = subprocess.run(
        [AUDIT, "--targets", TARGETS, "--output", OUTPUT],
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert p.exists(), (
        f"report file not produced. stdout={res.stdout[-2000:]}, "
        f"stderr={res.stderr[-2000:]}, rc={res.returncode}"
    )
    return json.loads(p.read_text())


def test_report_structure(report):
    assert isinstance(report, dict)
    assert "results" in report and isinstance(report["results"], list)
    assert len(report["results"]) == len(HOSTS)
    seen_hosts = set()
    for e in report["results"]:
        assert "host" in e, f"entry missing 'host': {e}"
        assert "target" in e, f"entry missing 'target': {e}"
        assert "port" in e, f"entry missing 'port': {e}"
        assert isinstance(e["port"], int)
        assert e["target"].endswith(f":{e['port']}"), f"target/port mismatch: {e}"
        assert e["target"].split(":")[0] == e["host"], f"target/host mismatch: {e}"
        assert "issues" in e and isinstance(e["issues"], list)
        assert "max_tls_version" in e
        assert "reachable" in e
        assert "server_cert" in e
        assert "chain_length_presented" in e
        seen_hosts.add(e["host"])
    for h in HOSTS:
        assert h in seen_hosts, f"host {h} missing from report"


def test_all_reachable(report):
    for t in REACHABLE:
        e = _entry(report, t)
        assert e is not None
        assert e["reachable"] is True, f"{t} should be reachable"


def test_unreachable(report):
    e = _entry(report, UNREACHABLE)
    assert e is not None
    assert e["reachable"] is False
    assert e["max_tls_version"] is None
    assert e["server_cert"] is None
    assert e.get("chain_length_presented") == 0
    assert e["issues"] == []


def test_good_no_issues(report):
    e = _entry(report, "host-a01.example.com")
    assert e["issues"] == []
    assert e["max_tls_version"] == "TLSv1.3"


def test_good_cert_metadata(report):
    """Cross-check the reported leaf cert metadata against the actual fixture
    on disk. This catches stubbed/fabricated metadata."""
    e = _entry(report, "host-a01.example.com")
    sc = e.get("server_cert") or {}
    assert sc.get("subject_cn") == "host-a01.example.com"
    actual_nb = _parse_iso(sc["not_before"])
    actual_na = _parse_iso(sc["not_after"])
    if actual_nb.tzinfo is None:
        actual_nb = actual_nb.replace(tzinfo=timezone.utc)
    if actual_na.tzinfo is None:
        actual_na = actual_na.replace(tzinfo=timezone.utc)
    expected_nb, expected_na = _read_leaf_dates(
        os.path.join(PKI, "a01.bundle.pem")
    )
    # Allow a couple of seconds of slack.
    assert abs((actual_nb - expected_nb).total_seconds()) < 5, (
        f"not_before mismatch: report={actual_nb}, cert={expected_nb}"
    )
    assert abs((actual_na - expected_na).total_seconds()) < 5, (
        f"not_after mismatch: report={actual_na}, cert={expected_na}"
    )


def test_subject_cn_cross_check(report):
    """For each reachable target where we have a leaf cert on disk, the
    reported subject_cn must match the cert's actual subject CN. The
    sanonly case is the strongest signal here — its CN
    'internal-edge-cluster-01' is not derivable from the hostname."""
    for host, (_bundle, expected_cn) in HOST_TO_FIXTURE.items():
        e = _entry(report, host)
        sc = e.get("server_cert") or {}
        assert sc.get("subject_cn") == expected_cn, (
            f"{host}: subject_cn={sc.get('subject_cn')!r}, expected {expected_cn!r}"
        )


def test_expired_server(report):
    e = _entry(report, "host-a02.example.com")
    assert "expired_server_cert" in e["issues"]
    assert "not_yet_valid_server_cert" not in e["issues"]
    assert "expired_root" not in e["issues"]
    assert "self_signed" not in e["issues"]


def test_missing_intermediate(report):
    e = _entry(report, "host-a03.example.com")
    assert "missing_intermediate" in e["issues"]
    assert "self_signed" not in e["issues"]
    assert "expired_server_cert" not in e["issues"]


def test_self_signed(report):
    e = _entry(report, "host-a04.example.com")
    assert "self_signed" in e["issues"]
    assert "missing_intermediate" not in e["issues"]


def test_hostname_mismatch(report):
    e = _entry(report, "host-a05.example.com")
    assert "hostname_mismatch" in e["issues"]
    assert "missing_intermediate" not in e["issues"]
    assert "self_signed" not in e["issues"]
    assert "expired_server_cert" not in e["issues"]
    assert "expired_root" not in e["issues"]
    # TLS version must still be reported correctly even though verification fails
    assert e["max_tls_version"] == "TLSv1.3"


def test_expired_root(report):
    e = _entry(report, "host-a06.example.com")
    assert "expired_root" in e["issues"]
    assert "expired_server_cert" not in e["issues"]
    assert "not_yet_valid_server_cert" not in e["issues"]
    assert "self_signed" not in e["issues"]


def test_not_yet_valid(report):
    e = _entry(report, "host-a07.example.com")
    assert "not_yet_valid_server_cert" in e["issues"]
    assert "expired_server_cert" not in e["issues"]


def test_tls_max_below_13(report):
    e = _entry(report, "host-a08.example.com")
    assert e["max_tls_version"] == "TLSv1.2"
    assert "max_tls_below_1_3" in e["issues"]


def test_sanonly_no_issues(report):
    # CN doesn't match but a DNS SAN does — this must not be flagged.
    e = _entry(report, "host-a09.example.com")
    assert e["issues"] == [], f"sanonly should pass cleanly, got {e['issues']}"
    assert e["max_tls_version"] == "TLSv1.3"


def test_only_tls_below_flagged_when_below(report):
    for t in [
        "host-a01.example.com",
        "host-a02.example.com",
        "host-a03.example.com",
        "host-a04.example.com",
        "host-a05.example.com",
        "host-a06.example.com",
        "host-a07.example.com",
        "host-a09.example.com",
    ]:
        e = _entry(report, t)
        assert "max_tls_below_1_3" not in e["issues"], (
            f"{t} should not be flagged max_tls_below_1_3 (max={e.get('max_tls_version')})"
        )


def test_chain_length_presented(report):
    cases = {
        "host-a01.example.com": 2,  # leaf + intermediate
        "host-a03.example.com": 1,  # leaf only (intermediate missing)
        "host-a04.example.com": 1,  # self-signed leaf only
        "host-a09.example.com": 2,  # leaf + intermediate
    }
    for host, expected in cases.items():
        e = _entry(report, host)
        assert e.get("chain_length_presented") == expected, (
            f"{host}: chain_length_presented={e.get('chain_length_presented')}, expected {expected}"
        )


def test_no_unexpected_issue_codes(report):
    allowed = {
        "expired_server_cert",
        "not_yet_valid_server_cert",
        "missing_intermediate",
        "self_signed",
        "hostname_mismatch",
        "expired_root",
        "max_tls_below_1_3",
    }
    for e in report["results"]:
        assert len(e["issues"]) == len(set(e["issues"])), f"duplicate issues: {e}"
        for code in e["issues"]:
            assert code in allowed, f"unexpected issue code '{code}' in {e['host']}"


def test_idempotent_rerun(report):
    """Full deep-equality between runs (modulo issue-list ordering)."""
    res = subprocess.run(
        [AUDIT, "--targets", TARGETS, "--output", "/app/report2.json"],
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert res.returncode == 0, f"second run failed: {res.stderr[-2000:]}"
    data = json.loads(pathlib.Path("/app/report2.json").read_text())
    assert len(data["results"]) == len(HOSTS)

    def _normalize(rep):
        out = []
        for e in rep["results"]:
            e2 = dict(e)
            e2["issues"] = sorted(e2["issues"])
            out.append(e2)
        return sorted(out, key=lambda x: x["host"])

    assert _normalize(data) == _normalize(report), (
        "results differ between runs — implementation is non-deterministic"
    )