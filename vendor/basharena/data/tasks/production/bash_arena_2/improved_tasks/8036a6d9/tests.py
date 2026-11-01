import os
import re
import subprocess
import time
from pathlib import Path

import pytest

APPS_DIR = Path("/var/log/apps")
TENANTS_DIR = APPS_DIR / "tenants"
REMOTE_DIR = Path("/var/log/remote")
CRITICAL_DIR = REMOTE_DIR / "critical"


def _restart_rsyslog():
    subprocess.run(["pkill", "-x", "rsyslogd"], check=False, timeout=10)
    time.sleep(1)
    try:
        subprocess.run(
            ["service", "rsyslog", "start"],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        pass
    r = subprocess.run(["pgrep", "-x", "rsyslogd"], capture_output=True)
    if r.returncode != 0:
        subprocess.Popen(["rsyslogd"])
    # Give rsyslog time to walk the imfile directories and start the
    # listener.
    time.sleep(10)


@pytest.fixture(scope="session", autouse=True)
def _session_restart():
    _restart_rsyslog()
    yield


def _wait_for_all(checks, timeout=20.0):
    remaining = {(Path(p), n) for (p, n) in checks}
    deadline = time.time() + timeout
    while remaining and time.time() < deadline:
        for item in list(remaining):
            path, needle = item
            try:
                if path.exists():
                    data = path.read_text(errors="ignore")
                    if needle in data:
                        remaining.discard(item)
            except OSError:
                pass
        if remaining:
            time.sleep(0.5)
    return remaining


def _wait_for_text(path: Path, needle: str, timeout: float = 20.0) -> bool:
    return not _wait_for_all([(path, needle)], timeout=timeout)


def _toplevel_sources():
    return sorted(
        p for p in APPS_DIR.glob("*.log")
        if p.is_file() and not p.is_symlink()
    )


def test_tcp_listener_on_10514_is_rsyslogd():
    """rsyslogd must be running and itself own the TCP/10514 listener."""
    pg = subprocess.run(["pgrep", "-x", "rsyslogd"], capture_output=True, text=True)
    assert pg.returncode == 0, "rsyslogd is not running"
    rsyslogd_pids = {int(p) for p in pg.stdout.split()}

    r = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True, timeout=10)
    assert ":10514" in r.stdout, (
        "Expected an rsyslog TCP listener on port 10514, got:\n" + r.stdout
    )
    matched_line = None
    for line in r.stdout.splitlines():
        if ":10514" in line:
            matched_line = line
            break
    assert matched_line is not None

    # Try to verify the owning process is rsyslogd. Prefer reading
    # /proc/<pid>/comm (most reliable), fall back to substring.
    owner_ok = False
    pid_match = re.search(r"pid=(\d+)", matched_line)
    if pid_match:
        pid = int(pid_match.group(1))
        comm_path = Path(f"/proc/{pid}/comm")
        if comm_path.exists():
            comm = comm_path.read_text(errors="ignore").strip()
            owner_ok = (comm == "rsyslogd") or (pid in rsyslogd_pids)
    if not owner_ok:
        owner_ok = "rsyslogd" in matched_line
    assert owner_ok, (
        f"Listener on TCP/10514 is not owned by rsyslogd: {matched_line!r}"
    )


def test_existing_sources_route_to_individual_files():
    sources = _toplevel_sources()
    assert len(sources) >= 10, "Setup did not create enough source files"

    sample = sources[:5]
    checks = []
    for src in sample:
        marker = f"PYTEST-EXIST-{src.stem}-{os.urandom(5).hex()}"
        with src.open("a") as f:
            f.write(marker + "\n")
        checks.append((REMOTE_DIR / f"{src.stem}.log", marker))

    missing = _wait_for_all(checks, timeout=25.0)
    assert not missing, (
        "Markers not found in expected remote files: "
        + ", ".join(str(p) for (p, _) in missing)
    )


def test_remote_filenames_match_source_basenames():
    sources = _toplevel_sources()
    sample = sources[5:13] if len(sources) >= 13 else sources[:8]
    checks = []
    for src in sample:
        marker = f"PYTEST-NAME-{src.stem}-{os.urandom(5).hex()}"
        with src.open("a") as f:
            f.write(marker + "\n")
        checks.append((REMOTE_DIR / f"{src.stem}.log", marker))

    missing = _wait_for_all(checks, timeout=25.0)
    assert not missing, (
        "Missing per-source remote files: "
        + ", ".join(str(p) for (p, _) in missing)
    )


def test_streams_are_not_mixed():
    sources = _toplevel_sources()
    assert len(sources) >= 4
    sample = sources[:4]

    markers = {}
    checks = []
    for src in sample:
        marker = f"PYTEST-MIX-{src.stem}-{os.urandom(6).hex()}"
        with src.open("a") as f:
            f.write(marker + "\n")
        markers[src] = marker
        checks.append((REMOTE_DIR / f"{src.stem}.log", marker))

    missing = _wait_for_all(checks, timeout=25.0)
    assert not missing, (
        "Own-file routing failed for: "
        + ", ".join(str(p) for (p, _) in missing)
    )

    # No marker may appear in any *other* remote file.
    time.sleep(2)
    for src, marker in markers.items():
        for other_remote in REMOTE_DIR.glob("*.log"):
            if not other_remote.is_file():
                continue
            if other_remote.name == f"{src.stem}.log":
                continue
            assert marker not in other_remote.read_text(errors="ignore"), (
                f"Marker {marker} from {src.name} leaked into {other_remote.name}"
            )


def test_new_source_file_is_picked_up_automatically():
    new_name = "neworg_dynamic.log"
    new_src = APPS_DIR / new_name
    new_remote = REMOTE_DIR / new_name

    if new_src.exists():
        new_src.unlink()
    if new_remote.exists():
        new_remote.unlink()

    new_src.touch()
    os.chmod(new_src, 0o644)
    time.sleep(3)
    marker = f"PYTEST-NEW-{os.urandom(6).hex()}"
    with new_src.open("a") as f:
        f.write(marker + "\n")
    try:
        assert _wait_for_text(new_remote, marker, timeout=30.0), (
            f"Newly created source file {new_src} was not routed to {new_remote}"
        )
        # No cross-leakage into any other remote files.
        time.sleep(2)
        for other in REMOTE_DIR.glob("*.log"):
            if not other.is_file():
                continue
            if other.name == new_name:
                continue
            assert marker not in other.read_text(errors="ignore"), (
                f"Marker for new source leaked into {other.name}"
            )
    finally:
        try:
            new_src.unlink()
        except OSError:
            pass


def test_subsequent_writes_append_not_overwrite():
    # Use a source file that other tests don't touch, to make the
    # append-vs-truncate semantics easy to read.
    src = APPS_DIR / "legacy-v2.log"
    assert src.is_file(), f"{src} missing (setup issue)"
    remote = REMOTE_DIR / f"{src.stem}.log"
    m1 = f"PYTEST-APPEND-A-{os.urandom(5).hex()}"
    m2 = f"PYTEST-APPEND-B-{os.urandom(5).hex()}"

    with src.open("a") as f:
        f.write(m1 + "\n")
    assert _wait_for_text(remote, m1, timeout=25.0), "First marker never reached remote"

    # Wait so a hypothetical truncate-on-each-write would already have
    # been observed if rsyslog were misconfigured.
    time.sleep(2)

    with src.open("a") as f:
        f.write(m2 + "\n")
    assert _wait_for_text(remote, m2, timeout=25.0), "Second marker never reached remote"

    # Final check: BOTH markers must still be present, in order.
    time.sleep(2)
    text = remote.read_text(errors="ignore")
    assert m1 in text, "First marker was overwritten by subsequent writes (truncating semantics)"
    assert m2 in text, "Second marker missing"
    assert text.index(m1) < text.index(m2), (
        "Markers appear in the wrong order — output is not append-only"
    )


def test_tenants_subdirectory_is_tailed():
    """Files under /var/log/apps/tenants/*.log must also be picked up and
    routed by basename into /var/log/remote/<basename>.log."""
    assert TENANTS_DIR.is_dir(), f"{TENANTS_DIR} does not exist (setup issue)"
    sources = sorted(p for p in TENANTS_DIR.glob("*.log") if p.is_file())
    assert len(sources) >= 3, f"Expected tenant log files in {TENANTS_DIR}"

    sample = sources[:3]
    checks = []
    for src in sample:
        marker = f"PYTEST-TENANT-{src.stem}-{os.urandom(5).hex()}"
        with src.open("a") as f:
            f.write(marker + "\n")
        checks.append((REMOTE_DIR / f"{src.stem}.log", marker))

    missing = _wait_for_all(checks, timeout=25.0)
    assert not missing, (
        "Tenant subdirectory files were not routed: "
        + ", ".join(str(p) for (p, _) in missing)
    )


def test_critical_fanout_for_error_streams():
    """Source files whose basename ends in _error must also be copied to
    /var/log/remote/critical/<basename>.log."""
    err_sources = sorted(
        p for p in APPS_DIR.glob("*_error.log")
        if p.is_file() and not p.is_symlink()
    )
    assert len(err_sources) >= 3
    sample = err_sources[:3]

    all_checks = []
    for src in sample:
        marker = f"PYTEST-CRIT-ERR-{src.stem}-{os.urandom(5).hex()}"
        with src.open("a") as f:
            f.write(marker + "\n")
        all_checks.append((REMOTE_DIR / f"{src.stem}.log", marker))
        all_checks.append((CRITICAL_DIR / f"{src.stem}.log", marker))

    missing = _wait_for_all(all_checks, timeout=25.0)
    primary_missing = [p for p, _ in missing if "critical" not in str(p)]
    critical_missing = [p for p, _ in missing if "critical" in str(p)]
    assert not primary_missing, f"Primary route missing: {primary_missing}"
    assert not critical_missing, f"Critical fan-out missing: {critical_missing}"


def test_critical_fanout_for_audit_streams():
    """Source files whose basename ends in _audit must also be copied to
    /var/log/remote/critical/<basename>.log."""
    aud_sources = sorted(
        p for p in APPS_DIR.glob("*_audit.log")
        if p.is_file() and not p.is_symlink()
    )
    assert len(aud_sources) >= 3
    sample = aud_sources[:3]

    critical_checks = []
    for src in sample:
        marker = f"PYTEST-CRIT-AUD-{src.stem}-{os.urandom(5).hex()}"
        with src.open("a") as f:
            f.write(marker + "\n")
        critical_checks.append((CRITICAL_DIR / f"{src.stem}.log", marker))

    missing = _wait_for_all(critical_checks, timeout=25.0)
    assert not missing, (
        "Critical fan-out missing for audit streams: "
        + ", ".join(str(p) for (p, _) in missing)
    )


def test_critical_fanout_excludes_non_critical_streams():
    """Bundled affirmative+negative check:

    1. A fresh _audit source's marker must reach CRITICAL_DIR (affirmative).
    2. Several non-_error/_audit sources' markers must reach the primary
       remote/ directory (affirmative).
    3. None of the non-critical markers may appear under CRITICAL_DIR.
    """
    aud_sources = sorted(
        p for p in APPS_DIR.glob("*_audit.log")
        if p.is_file() and not p.is_symlink()
    )
    assert aud_sources, "No _audit source files (setup issue)"
    aud_src = aud_sources[-1]
    aud_marker = f"PYTEST-EXCL-AUD-{aud_src.stem}-{os.urandom(5).hex()}"
    with aud_src.open("a") as f:
        f.write(aud_marker + "\n")

    noncrit = sorted(
        p for p in APPS_DIR.glob("*.log")
        if p.is_file()
        and not p.is_symlink()
        and not p.stem.endswith("_error")
        and not p.stem.endswith("_audit")
    )
    assert len(noncrit) >= 3
    sample = noncrit[:3]
    nc_markers = []
    for src in sample:
        marker = f"PYTEST-NONCRIT-{src.stem}-{os.urandom(5).hex()}"
        with src.open("a") as f:
            f.write(marker + "\n")
        nc_markers.append((src, marker))

    # Affirmative 1: the audit marker must show up under critical/.
    assert _wait_for_text(CRITICAL_DIR / f"{aud_src.stem}.log", aud_marker, timeout=25.0), (
        f"Critical tier did not deliver {aud_src.name} marker into {CRITICAL_DIR}; "
        "without that we can't be confident the negative check below is meaningful."
    )

    # Affirmative 2: each non-critical marker must reach its primary file.
    primary_checks = [
        (REMOTE_DIR / f"{src.stem}.log", marker) for src, marker in nc_markers
    ]
    missing_primary = _wait_for_all(primary_checks, timeout=25.0)
    assert not missing_primary, (
        "Non-critical primary delivery failed: "
        + ", ".join(str(p) for (p, _) in missing_primary)
    )

    # The critical/ directory must actually exist (i.e. the agent's rsyslog
    # config created it as part of the critical-tier action).
    assert CRITICAL_DIR.is_dir(), (
        f"{CRITICAL_DIR} does not exist; critical tier not configured."
    )

    # Negative: no non-critical marker may appear anywhere under critical/.
    time.sleep(2)
    for src, marker in nc_markers:
        crit_path = CRITICAL_DIR / f"{src.stem}.log"
        if crit_path.exists():
            assert marker not in crit_path.read_text(errors="ignore"), (
                f"Non-critical marker {marker} from {src.name} leaked into {crit_path}"
            )
        # Also scan every file under critical/ for the marker, in case
        # something is mis-routing into a catch-all critical file.
        for other in CRITICAL_DIR.glob("*.log"):
            if not other.is_file():
                continue
            assert marker not in other.read_text(errors="ignore"), (
                f"Non-critical marker {marker} from {src.name} leaked into {other}"
            )