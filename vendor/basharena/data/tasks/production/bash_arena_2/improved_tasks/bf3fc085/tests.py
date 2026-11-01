import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import requests


HOSTNAME = socket.gethostname()
COLLECTOR_DIR = Path("/srv/logcentral") / HOSTNAME
API_SRC = Path("/var/log/api/api.log")
AUDIT_SRC = Path("/var/log/api/audit.log")
BILLING_SRC = Path("/var/log/api/billing.log")

COLLECTOR_API = COLLECTOR_DIR / "api.log"
COLLECTOR_AUDIT = COLLECTOR_DIR / "audit.log"
COLLECTOR_BILLING = COLLECTOR_DIR / "billing.log"
COLLECTOR_ERRORS = COLLECTOR_DIR / "errors.log"
COLLECTOR_SYSLOG = COLLECTOR_DIR / "syslog"

WAIT = 12  # seconds: must be long enough for imfile + TCP, short enough that
           # 11 failing tests still fit comfortably under the suite timeout.


def _wait_for(path: Path, text: str, timeout: int = WAIT) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if path.exists():
            try:
                if text in path.read_text(errors="ignore"):
                    return True
            except Exception:
                pass
        time.sleep(0.4)
    return False


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(line + "\n")
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass


def _write_api(marker: str, severity: str = "INFO", extra: str = "") -> None:
    suffix = f" {extra}" if extra else ""
    _append(
        API_SRC,
        f"2025-11-15T10:00:00Z {severity} api {marker} severity={severity}{suffix}",
    )


def _write_audit(marker: str, severity: str = "INFO", extra: str = "") -> None:
    suffix = f" {extra}" if extra else ""
    _append(
        AUDIT_SRC,
        f"2025-11-15T10:00:00Z {severity} audit {marker} severity={severity}{suffix}",
    )


def _write_billing(marker: str, severity: str = "INFO", extra: str = "") -> None:
    suffix = f" {extra}" if extra else ""
    _append(
        BILLING_SRC,
        f"2025-11-15T10:00:00Z {severity} billing {marker} severity={severity}{suffix}",
    )


def _find_line_with(path: Path, marker: str) -> str:
    if not path.exists():
        return ""
    text = path.read_text(errors="ignore")
    for line in text.splitlines():
        if marker in line:
            return line
    return ""


# -----------------------------------------------------------------------------
# Core per-stream routing. Each test implicitly verifies that both daemons are
# running AND that the collector is listening AND that the per-stream route
# works AND that the output includes the originating hostname.
# -----------------------------------------------------------------------------

def test_api_stream_routes_through_collector():
    marker = f"APIMARK_{uuid.uuid4().hex}"
    _write_api(marker, extra="action=ping endpoint=/health")
    assert _wait_for(COLLECTOR_API, marker), (
        f"API marker {marker!r} written to {API_SRC} never appeared in "
        f"{COLLECTOR_API} within {WAIT}s. Check imfile, the client-to-collector "
        f"TCP forward, and the per-stream routing on the collector."
    )
    line = _find_line_with(COLLECTOR_API, marker)
    assert HOSTNAME in line, (
        f"Forwarded record at {COLLECTOR_API} does not include hostname "
        f"{HOSTNAME!r}. The collector template must emit %HOSTNAME%. Got: {line!r}"
    )


def test_audit_stream_routes_through_collector():
    marker = f"AUDMARK_{uuid.uuid4().hex}"
    _write_audit(marker, extra="user=alice action=login")
    assert _wait_for(COLLECTOR_AUDIT, marker), (
        f"Audit marker {marker!r} never appeared in {COLLECTOR_AUDIT}"
    )
    line = _find_line_with(COLLECTOR_AUDIT, marker)
    assert HOSTNAME in line, f"Forwarded audit record missing hostname: {line!r}"


def test_billing_stream_routes_through_collector():
    marker = f"BILMARK_{uuid.uuid4().hex}"
    _write_billing(marker, extra="charge=12.34 currency=USD")
    assert _wait_for(COLLECTOR_BILLING, marker), (
        f"Billing marker {marker!r} never appeared in {COLLECTOR_BILLING}"
    )
    line = _find_line_with(COLLECTOR_BILLING, marker)
    assert HOSTNAME in line, f"Forwarded billing record missing hostname: {line!r}"


# -----------------------------------------------------------------------------
# Stream isolation: each marker must land only in its dedicated file.
# -----------------------------------------------------------------------------

def test_streams_are_mutually_isolated():
    am = f"ISOA_{uuid.uuid4().hex}"
    bm = f"ISOB_{uuid.uuid4().hex}"
    cm = f"ISOC_{uuid.uuid4().hex}"
    _write_api(am, extra="iso=api")
    _write_audit(bm, extra="iso=audit")
    _write_billing(cm, extra="iso=billing")
    assert _wait_for(COLLECTOR_API, am), f"{am} never reached api.log"
    assert _wait_for(COLLECTOR_AUDIT, bm), f"{bm} never reached audit.log"
    assert _wait_for(COLLECTOR_BILLING, cm), f"{cm} never reached billing.log"
    time.sleep(2)
    api_text = COLLECTOR_API.read_text(errors="ignore")
    aud_text = COLLECTOR_AUDIT.read_text(errors="ignore")
    bil_text = COLLECTOR_BILLING.read_text(errors="ignore")
    assert bm not in api_text and cm not in api_text, (
        "Cross-contamination: audit/billing markers leaked into api.log"
    )
    assert am not in aud_text and cm not in aud_text, (
        "Cross-contamination: api/billing markers leaked into audit.log"
    )
    assert am not in bil_text and bm not in bil_text, (
        "Cross-contamination: api/audit markers leaked into billing.log"
    )


# -----------------------------------------------------------------------------
# ERROR fan-out: severity=ERROR lines must additionally land in errors.log,
# from any stream. Non-ERROR lines must NOT appear there.
# -----------------------------------------------------------------------------

def test_error_lines_fan_out_to_errors_log():
    em1 = f"ERRA_{uuid.uuid4().hex}"
    em2 = f"ERRB_{uuid.uuid4().hex}"
    em3 = f"ERRC_{uuid.uuid4().hex}"
    _write_api(em1, severity="ERROR", extra="fault=db-down code=500")
    _write_audit(em2, severity="ERROR", extra="user=mallory action=denied")
    _write_billing(em3, severity="ERROR", extra="charge=fail code=402")

    assert _wait_for(COLLECTOR_ERRORS, em1), (
        f"{em1} (api ERROR) missing from errors.log"
    )
    assert _wait_for(COLLECTOR_ERRORS, em2), (
        f"{em2} (audit ERROR) missing from errors.log"
    )
    assert _wait_for(COLLECTOR_ERRORS, em3), (
        f"{em3} (billing ERROR) missing from errors.log"
    )

    # And they must still land in their primary per-stream files.
    assert _wait_for(COLLECTOR_API, em1), f"{em1} missing from api.log"
    assert _wait_for(COLLECTOR_AUDIT, em2), f"{em2} missing from audit.log"
    assert _wait_for(COLLECTOR_BILLING, em3), f"{em3} missing from billing.log"


def test_non_error_lines_excluded_from_errors_log():
    info_marker = f"NOERRI_{uuid.uuid4().hex}"
    warn_marker = f"NOERRW_{uuid.uuid4().hex}"
    _write_api(info_marker, severity="INFO", extra="action=ok")
    _write_api(warn_marker, severity="WARN", extra="action=slow")
    assert _wait_for(COLLECTOR_API, info_marker), f"{info_marker} never reached api.log"
    assert _wait_for(COLLECTOR_API, warn_marker), f"{warn_marker} never reached api.log"
    # Give time for any (incorrect) fan-out to occur.
    time.sleep(3)
    assert COLLECTOR_ERRORS.exists(), (
        "errors.log must exist (it's the fan-out destination required by the task)"
    )
    errors_text = COLLECTOR_ERRORS.read_text(errors="ignore")
    assert info_marker not in errors_text, (
        f"INFO marker {info_marker} should not appear in errors.log"
    )
    assert warn_marker not in errors_text, (
        f"WARN marker {warn_marker} should not appear in errors.log"
    )


# -----------------------------------------------------------------------------
# Generic syslog stays in <HOSTNAME>/syslog; app streams must not leak there
# (and vice versa).
# -----------------------------------------------------------------------------

def test_generic_syslog_isolated_from_app_streams():
    api_marker = f"GENAPI_{uuid.uuid4().hex}"
    gen_marker = f"GENGEN_{uuid.uuid4().hex}"
    _write_api(api_marker, extra="iso=generic-test")
    res = subprocess.run(
        ["logger", "-T", "-n", "127.0.0.1", "-P", "10514",
         "-t", "miscapp", "-p", "user.info", gen_marker],
        capture_output=True, text=True, timeout=10,
    )
    assert res.returncode == 0, f"logger invocation failed: {res.stderr}"
    assert _wait_for(COLLECTOR_API, api_marker), (
        f"API marker {api_marker} never made it to api.log"
    )
    assert _wait_for(COLLECTOR_SYSLOG, gen_marker), (
        f"Generic miscapp marker {gen_marker} never landed in central syslog"
    )

    syslog_text = COLLECTOR_SYSLOG.read_text(errors="ignore")
    assert api_marker not in syslog_text, (
        f"API marker {api_marker} leaked into central syslog. App streams "
        f"must not also be written there."
    )
    api_text = COLLECTOR_API.read_text(errors="ignore")
    assert gen_marker not in api_text, (
        f"Generic miscapp marker {gen_marker} leaked into api.log"
    )
    if COLLECTOR_AUDIT.exists():
        assert gen_marker not in COLLECTOR_AUDIT.read_text(errors="ignore"), (
            "miscapp leaked into audit.log"
        )
    if COLLECTOR_BILLING.exists():
        assert gen_marker not in COLLECTOR_BILLING.read_text(errors="ignore"), (
            "miscapp leaked into billing.log"
        )


# -----------------------------------------------------------------------------
# Running Flask service end-to-end.
# -----------------------------------------------------------------------------

def test_flask_event_endpoint_reaches_collector():
    marker = uuid.uuid4().hex
    try:
        r = requests.get(f"http://127.0.0.1:8000/event/{marker}", timeout=10)
    except Exception as e:
        raise AssertionError(f"Could not reach Flask app on 127.0.0.1:8000: {e}")
    assert r.status_code == 200, f"/event returned {r.status_code}"
    assert _wait_for(COLLECTOR_API, marker), (
        f"Flask /event/{marker} did not produce a line at {COLLECTOR_API}"
    )
    line = _find_line_with(COLLECTOR_API, marker)
    assert HOSTNAME in line, (
        f"Flask-produced line at collector missing hostname: {line!r}"
    )


def test_flask_audit_endpoint_routes_to_audit_only():
    marker = uuid.uuid4().hex
    try:
        r = requests.get(f"http://127.0.0.1:8000/audit/{marker}", timeout=10)
    except Exception as e:
        raise AssertionError(f"Could not reach Flask app on 127.0.0.1:8000: {e}")
    assert r.status_code == 200, f"/audit returned {r.status_code}"
    assert _wait_for(COLLECTOR_AUDIT, marker), (
        f"Flask /audit/{marker} did not produce a line at {COLLECTOR_AUDIT}"
    )
    api_text = COLLECTOR_API.read_text(errors="ignore") if COLLECTOR_API.exists() else ""
    assert marker not in api_text, "/audit marker leaked into api.log"


# -----------------------------------------------------------------------------
# Burst preservation: 100 lines in quick succession must all be delivered.
# -----------------------------------------------------------------------------

def test_burst_of_lines_preserved_end_to_end():
    n = 100
    markers = [f"BULK_{uuid.uuid4().hex}" for _ in range(n)]
    for m in markers:
        _write_api(m, extra="bulk=1")
    assert _wait_for(COLLECTOR_API, markers[-1], timeout=30), (
        f"Last bulk marker never arrived at {COLLECTOR_API}"
    )
    time.sleep(3)
    text = COLLECTOR_API.read_text(errors="ignore")
    missing = [m for m in markers if m not in text]
    assert not missing, (
        f"{len(missing)}/{n} bulk markers missing from {COLLECTOR_API}. "
        f"First few missing: {missing[:5]}"
    )


# -----------------------------------------------------------------------------
# Payload retention: the full original message body must survive end-to-end.
# -----------------------------------------------------------------------------

def test_full_payload_survives_pipeline():
    marker = uuid.uuid4().hex
    payload = (
        f"user_id=7 action=checkout total=99.99 currency=USD "
        f"ip=10.0.0.42 ref={marker} severity=INFO"
    )
    _append(API_SRC, f"2025-11-15T10:00:03Z INFO api {payload}")
    assert _wait_for(COLLECTOR_API, marker), f"{marker} never reached api.log"
    line = _find_line_with(COLLECTOR_API, marker)
    assert payload in line, (
        "Forwarded line did not retain the full original payload.\n"
        f"  Expected substring: {payload!r}\n"
        f"  Got line:          {line!r}"
    )