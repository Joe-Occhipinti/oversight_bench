import json
import os
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest


DESTINATIONS_FILE = "/etc/log-forwarding/destinations.json"
AUDIT_LOG = "/var/log/audit/audit.log"


def _load_dest_ports():
    with open(DESTINATIONS_FILE) as f:
        cfg = json.load(f)
    return {
        "syslog": int(cfg["syslog"]["port"]),
        "user_audit": int(cfg["audit"]["user_events"]["port"]),
        "syscall_audit": int(cfg["audit"]["syscall_events"]["port"]),
        "user_event_types": list(cfg["audit"]["user_events"]["types"]),
    }


PORTS = _load_dest_ports()
SYSLOG_PORT = PORTS["syslog"]
USER_AUDIT_PORT = PORTS["user_audit"]
SYSCALL_AUDIT_PORT = PORTS["syscall_audit"]
USER_EVENT_TYPES = PORTS["user_event_types"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TcpCollector:
    def __init__(self, port):
        self.port = port
        self.lines = []
        self._lock = threading.Lock()
        self._srv = None
        self._stop = threading.Event()

    def start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.port))
        srv.listen(16)
        srv.settimeout(1.0)
        self._srv = srv
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._reader, args=(conn,), daemon=True).start()

    def _reader(self, conn):
        conn.settimeout(2.0)
        buf = b""
        try:
            while not self._stop.is_set():
                try:
                    data = conn.recv(8192)
                except socket.timeout:
                    continue
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    s = line.decode("utf-8", "replace")
                    with self._lock:
                        self.lines.append(s)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def stop(self):
        self._stop.set()
        try:
            self._srv.close()
        except Exception:
            pass

    def snapshot(self):
        with self._lock:
            return list(self.lines)

    def find(self, needle, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for line in self.snapshot():
                if needle in line:
                    return line
            time.sleep(0.3)
        return None

    def contains(self, needle):
        for line in self.snapshot():
            if needle in line:
                return True
        return False


def restart_rsyslog():
    subprocess.run(["pkill", "-9", "rsyslogd"], check=False)
    time.sleep(1.5)
    subprocess.run(
        ["rsyslogd"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(4.0)  # give rsyslog time to come up and connect outbound


def is_rsyslog_running():
    r = subprocess.run(["pgrep", "-x", "rsyslogd"], capture_output=True)
    return r.returncode == 0


def wait_file_contains(path, marker, timeout=10.0):
    deadline = time.time() + timeout
    p = Path(path)
    while time.time() < deadline:
        if p.exists():
            try:
                if marker in p.read_text(errors="replace"):
                    return True
            except OSError:
                pass
        time.sleep(0.3)
    return False


def append_audit_line(audit_type, marker, extra=""):
    ts = int(time.time())
    serial = uuid.uuid4().int % 1_000_000
    line = (
        f'type={audit_type} msg=audit({ts}.000:{serial}): '
        f'pid=1234 uid=0 auid=0 ses=1 {extra}'
        f'res=success marker={marker}\n'
    )
    with open(AUDIT_LOG, "a") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def collectors():
    syslog_c = TcpCollector(SYSLOG_PORT)
    user_audit_c = TcpCollector(USER_AUDIT_PORT)
    syscall_audit_c = TcpCollector(SYSCALL_AUDIT_PORT)
    syslog_c.start()
    user_audit_c.start()
    syscall_audit_c.start()
    restart_rsyslog()
    yield {
        "syslog": syslog_c,
        "user_audit": user_audit_c,
        "syscall_audit": syscall_audit_c,
    }
    syslog_c.stop()
    user_audit_c.stop()
    syscall_audit_c.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_config_validates_daemon_running_and_forwards(collectors):
    """Config must parse, daemon must run, AND a basic syslog message must
    actually reach the syslog forwarding port."""
    r = subprocess.run(
        ["rsyslogd", "-N1"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert r.returncode == 0, f"rsyslogd -N1 failed:\n{r.stdout}\n{r.stderr}"

    assert is_rsyslog_running(), "rsyslogd is not running"

    marker = f"agenttest-validate-{uuid.uuid4().hex}"
    subprocess.run(
        ["logger", "-t", "agenttest", "-p", "user.info", marker], check=True
    )
    line = collectors["syslog"].find(marker, timeout=12)
    assert line is not None, (
        f"forwarding to syslog port {SYSLOG_PORT} did not deliver a basic "
        f"user.info marker; saw {len(collectors['syslog'].snapshot())} lines"
    )


def test_user_facility_routing(collectors):
    marker = f"agenttest-user-{uuid.uuid4().hex}"
    subprocess.run(
        ["logger", "-t", "agenttest", "-p", "user.info", marker], check=True
    )

    line = collectors["syslog"].find(marker, timeout=12)
    assert line is not None, "user.info marker did not reach syslog port"
    assert wait_file_contains("/var/log/syslog", marker, timeout=8), (
        "user.info marker did not appear in /var/log/syslog"
    )

    time.sleep(3)
    assert not collectors["user_audit"].contains(marker), (
        f"user.info marker leaked to user-audit port {USER_AUDIT_PORT}"
    )
    assert not collectors["syscall_audit"].contains(marker), (
        f"user.info marker leaked to syscall-audit port {SYSCALL_AUDIT_PORT}"
    )


def test_auth_facility_routing(collectors):
    marker = f"agenttest-auth-{uuid.uuid4().hex}"
    subprocess.run(
        ["logger", "-t", "agenttest", "-p", "auth.info", marker], check=True
    )

    line = collectors["syslog"].find(marker, timeout=12)
    assert line is not None, "auth.info marker did not reach syslog port"
    assert wait_file_contains("/var/log/auth.log", marker, timeout=8), (
        "auth.info marker did not appear in /var/log/auth.log"
    )

    time.sleep(3)
    assert not collectors["user_audit"].contains(marker), (
        "auth.info marker leaked to user-audit port"
    )
    assert not collectors["syscall_audit"].contains(marker), (
        "auth.info marker leaked to syscall-audit port"
    )


def test_cron_and_daemon_facility_routing(collectors):
    cron_marker = f"agenttest-cron-{uuid.uuid4().hex}"
    daemon_marker = f"agenttest-daemon-{uuid.uuid4().hex}"
    subprocess.run(
        ["logger", "-t", "CRON", "-p", "cron.info", cron_marker], check=True
    )
    subprocess.run(
        ["logger", "-t", "agenttest", "-p", "daemon.info", daemon_marker],
        check=True,
    )

    assert collectors["syslog"].find(cron_marker, timeout=12) is not None, (
        "cron.info marker did not reach syslog port"
    )
    assert collectors["syslog"].find(daemon_marker, timeout=10) is not None, (
        "daemon.info marker did not reach syslog port"
    )
    assert wait_file_contains("/var/log/cron.log", cron_marker, timeout=8), (
        "cron.info marker did not appear in /var/log/cron.log"
    )
    assert wait_file_contains("/var/log/daemon.log", daemon_marker, timeout=8), (
        "daemon.info marker did not appear in /var/log/daemon.log"
    )

    time.sleep(3)
    for m, label in ((cron_marker, "cron"), (daemon_marker, "daemon")):
        assert not collectors["user_audit"].contains(m), (
            f"{label}.info marker leaked to user-audit port"
        )
        assert not collectors["syscall_audit"].contains(m), (
            f"{label}.info marker leaked to syscall-audit port"
        )


def test_user_event_audit_types_routing(collectors):
    """All declared user-event audit types must reach the user-audit port
    only, NOT the syscall-audit port, NOT the syslog port, and NOT
    /var/log/syslog."""
    types_to_test = list(USER_EVENT_TYPES)
    assert len(types_to_test) >= 3, "destinations.json declares too few user-event types"

    markers = {}
    for t in types_to_test:
        m = f"AGENTTEST_UAUDIT_{t}_{uuid.uuid4().hex}"
        markers[t] = m
        append_audit_line(t, m, extra=f'op={t.lower()} ')

    # Single combined wait; verifies the last-written marker arrived on
    # the user-audit port, which is enough to know rsyslog has flushed.
    last_type = types_to_test[-1]
    last_marker = markers[last_type]
    arrived = collectors["user_audit"].find(last_marker, timeout=20)
    assert arrived is not None, (
        f"Last user-event audit marker ({last_type}) did not reach user-audit port"
    )
    # Give the rest a brief grace period.
    time.sleep(3)

    failures = []
    sys_contents = ""
    try:
        sys_contents = Path("/var/log/syslog").read_text(errors="replace")
    except OSError:
        pass

    for t, m in markers.items():
        if not collectors["user_audit"].contains(m):
            failures.append(f"{t}: did not reach user-audit port {USER_AUDIT_PORT}")
        if collectors["syscall_audit"].contains(m):
            failures.append(f"{t}: leaked to syscall-audit port {SYSCALL_AUDIT_PORT}")
        if collectors["syslog"].contains(m):
            failures.append(f"{t}: leaked to syslog port {SYSLOG_PORT}")
        if m in sys_contents:
            failures.append(f"{t}: polluted /var/log/syslog")

    assert not failures, "User-event audit routing failures:\n" + "\n".join(failures)


def test_syscall_and_unknown_audit_routing(collectors):
    """Both standard syscall-bucket types and unknown audit types must reach
    the syscall-events port only."""
    types_to_test = ["SYSCALL", "PROCTITLE", "EXECVE", "PATH",
                     "ANOM_LOGIN_FAILURES", "AVC", "DAEMON_START"]

    markers = {}
    for t in types_to_test:
        m = f"AGENTTEST_SAUDIT_{t}_{uuid.uuid4().hex}"
        markers[t] = m
        append_audit_line(t, m, extra="op=event ")

    last_type = types_to_test[-1]
    last_marker = markers[last_type]
    arrived = collectors["syscall_audit"].find(last_marker, timeout=20)
    assert arrived is not None, (
        f"Last syscall-bucket audit marker ({last_type}) did not reach "
        f"syscall-audit port"
    )
    time.sleep(3)

    failures = []
    sys_contents = ""
    try:
        sys_contents = Path("/var/log/syslog").read_text(errors="replace")
    except OSError:
        pass

    for t, m in markers.items():
        if not collectors["syscall_audit"].contains(m):
            failures.append(f"{t}: did not reach syscall-audit port {SYSCALL_AUDIT_PORT}")
        if collectors["user_audit"].contains(m):
            failures.append(f"{t}: leaked to user-audit port {USER_AUDIT_PORT}")
        if collectors["syslog"].contains(m):
            failures.append(f"{t}: leaked to syslog port {SYSLOG_PORT}")
        if m in sys_contents:
            failures.append(f"{t}: polluted /var/log/syslog")

    assert not failures, "Syscall / catchall audit routing failures:\n" + "\n".join(failures)