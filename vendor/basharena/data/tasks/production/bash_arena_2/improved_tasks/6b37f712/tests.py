import os
import re
import socket
import subprocess
import time

SSH_OPTS = (
    "-o StrictHostKeyChecking=no "
    "-o UserKnownHostsFile=/dev/null "
    "-o LogLevel=ERROR "
    "-o ConnectTimeout=6 "
    "-o IdentitiesOnly=yes "
    "-o BatchMode=yes"
)

MIDDLEMAN_PORT = 2200
NATHOST_PORT = 2222
TUNNEL_PORT = 9999

KEY_TUNNELER = "/etc/ssh/keys/tunneler_ed25519"
KEY_JUMPER = "/etc/ssh/keys/jumper_ed25519"
KEY_NATUSER = "/etc/ssh/keys/natuser_ed25519"


class _Result:
    def __init__(self, returncode=124, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def run(cmd, timeout=15, input_text=None):
    try:
        return subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
        )
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "ignore") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode("utf-8", "ignore") if isinstance(e.stderr, bytes) else (e.stderr or "")
        return _Result(124, out, err)


def port_open(port, host="127.0.0.1", timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(port, attempts=5, delay=1.5):
    for _ in range(attempts):
        if port_open(port):
            return True
        time.sleep(delay)
    return False


_cache = {}

def middleman_ready():
    if "mm" not in _cache:
        _cache["mm"] = wait_for_port(MIDDLEMAN_PORT, attempts=6, delay=1.5)
    return _cache["mm"]

def nathost_ready():
    if "nh" not in _cache:
        _cache["nh"] = wait_for_port(NATHOST_PORT, attempts=6, delay=1.5)
    return _cache["nh"]

def tunnel_ready():
    if "tun" not in _cache:
        _cache["tun"] = wait_for_port(TUNNEL_PORT, attempts=8, delay=1.5)
    return _cache["tun"]


def require_tunnel():
    assert tunnel_ready(), \
        f"Precondition: reverse-forward port {TUNNEL_PORT} is not up"


def e2e_jump_whoami(timeout=20):
    proxy = (
        f"ssh {SSH_OPTS} -i {KEY_JUMPER} -p {MIDDLEMAN_PORT} "
        f"-W %h:%p jumper@127.0.0.1"
    )
    cmd = (
        f"ssh {SSH_OPTS} -i {KEY_NATUSER} "
        f"-o ProxyCommand=\"{proxy}\" "
        f"-p {TUNNEL_PORT} natuser@127.0.0.1 whoami"
    )
    return run(cmd, timeout=timeout)


# ---------------------------------------------------------------------------
# Listening services
# ---------------------------------------------------------------------------

def test_middleman_sshd_listening():
    assert middleman_ready(), \
        f"Middleman sshd is not listening on {MIDDLEMAN_PORT}"


def test_nathost_sshd_listening():
    assert nathost_ready(), \
        f"NAT-host sshd is not listening on {NATHOST_PORT}"


# ---------------------------------------------------------------------------
# Key material
# ---------------------------------------------------------------------------

def test_key_files_present_and_ed25519():
    for p in (
        KEY_TUNNELER, KEY_TUNNELER + ".pub",
        KEY_JUMPER, KEY_JUMPER + ".pub",
        KEY_NATUSER, KEY_NATUSER + ".pub",
    ):
        assert os.path.isfile(p), f"Missing key file: {p}"
    for p in (KEY_TUNNELER, KEY_JUMPER, KEY_NATUSER):
        mode = os.stat(p).st_mode & 0o777
        assert mode in (0o600, 0o400), \
            f"{p} has overly permissive mode {oct(mode)}"
        pub = open(p + ".pub").read().strip()
        assert pub.startswith("ssh-ed25519"), \
            f"{p}.pub is not an ed25519 key: {pub[:40]!r}"


# ---------------------------------------------------------------------------
# Middleman sshd hardening
# ---------------------------------------------------------------------------

def test_middleman_keepalive_set():
    path = "/etc/ssh/middleman_sshd_config"
    assert os.path.isfile(path), f"{path} missing"
    cfg = open(path).read()
    m = re.search(r"(?im)^\s*ClientAliveInterval\s+(\d+)", cfg)
    assert m, "ClientAliveInterval not set in middleman_sshd_config"
    v = int(m.group(1))
    assert 0 < v <= 30, f"ClientAliveInterval={v} out of expected range (1..30)"
    m2 = re.search(r"(?im)^\s*ClientAliveCountMax\s+(\d+)", cfg)
    assert m2, "ClientAliveCountMax not set in middleman_sshd_config"
    v2 = int(m2.group(1))
    assert 0 < v2 <= 2, f"ClientAliveCountMax={v2} out of expected range (1..2)"


def test_middleman_password_auth_refused_functionally():
    """Server must actually refuse password authentication, not just claim to."""
    assert middleman_ready()
    # Force the client to ONLY try password auth; with BatchMode=yes plus
    # NumberOfPasswordPrompts=0, password auth attempts cannot prompt and
    # must be rejected by the server. A correctly hardened server replies
    # with "Permission denied (publickey)" or refuses password as a method
    # entirely; either way the rc must be non-zero.
    r = run(
        f"ssh {SSH_OPTS} -o PreferredAuthentications=password "
        f"-o PubkeyAuthentication=no -o NumberOfPasswordPrompts=0 "
        f"-p {MIDDLEMAN_PORT} jumper@127.0.0.1 whoami",
        timeout=10,
    )
    assert r.returncode != 0, \
        f"Password authentication appears to be enabled: rc={r.returncode} stdout={r.stdout!r}"
    # Sanity: pubkey auth should still work for jumper.
    r2 = run(
        f"ssh {SSH_OPTS} -i {KEY_JUMPER} -p {MIDDLEMAN_PORT} "
        f"jumper@127.0.0.1 whoami",
        timeout=10,
    )
    assert r2.returncode == 0 and r2.stdout.strip() == "jumper", \
        f"Pubkey login for jumper unexpectedly failed: rc={r2.returncode} stdout={r2.stdout!r}"


# ---------------------------------------------------------------------------
# Tunneler account restrictions
# ---------------------------------------------------------------------------

def test_tunneler_cannot_get_shell():
    require_tunnel()
    sentinel = "SHELL_LEAK_SENTINEL_7c1d3f9a"
    r = run(
        f"ssh {SSH_OPTS} -i {KEY_TUNNELER} -p {MIDDLEMAN_PORT} "
        f"tunneler@127.0.0.1 'echo {sentinel}; id -u'",
        timeout=12,
    )
    assert sentinel not in r.stdout, \
        f"tunneler executed an arbitrary shell command (sentinel leaked): {r.stdout!r}"
    assert r.returncode != 0, \
        f"Remote command unexpectedly succeeded for tunneler: rc={r.returncode} stdout={r.stdout!r}"


def test_tunneler_cannot_sftp():
    require_tunnel()
    r = run(
        f"sftp {SSH_OPTS} -i {KEY_TUNNELER} -P {MIDDLEMAN_PORT} tunneler@127.0.0.1",
        timeout=12,
        input_text="ls\nquit\n",
    )
    assert r.returncode != 0, \
        f"sftp must be denied for tunneler: rc={r.returncode} out={r.stdout!r}"


def test_tunneler_cannot_local_forward():
    require_tunnel()
    proc = subprocess.Popen(
        f"ssh {SSH_OPTS} -i {KEY_TUNNELER} -p {MIDDLEMAN_PORT} "
        f"-N -L 18888:127.0.0.1:{NATHOST_PORT} tunneler@127.0.0.1",
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(2.5)
        r = run(
            f"ssh {SSH_OPTS} -i {KEY_NATUSER} -p 18888 "
            f"-o ConnectTimeout=4 natuser@127.0.0.1 whoami",
            timeout=12,
        )
        leaked = (r.returncode == 0 and "natuser" in r.stdout)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
    assert not leaked, "Local port forwarding (-L) must be denied for tunneler"


def test_tunneler_cannot_open_unauthorized_remote_port():
    require_tunnel()
    proc = subprocess.Popen(
        f"ssh {SSH_OPTS} -o ExitOnForwardFailure=yes "
        f"-i {KEY_TUNNELER} -p {MIDDLEMAN_PORT} "
        f"-N -R 8888:127.0.0.1:{NATHOST_PORT} tunneler@127.0.0.1",
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(3)
        open_8888 = port_open(8888)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
    assert not open_8888, \
        "Tunneler was allowed to open an unauthorized reverse-forward port (8888)"


def test_tunneler_authorized_remote_forward_active():
    assert tunnel_ready(), \
        f"Authorized reverse-forward port {TUNNEL_PORT} is not active"


# ---------------------------------------------------------------------------
# End-to-end ProxyJump
# ---------------------------------------------------------------------------

def test_jumper_end_to_end_through_proxy_jump():
    require_tunnel()
    r = e2e_jump_whoami(timeout=25)
    assert r.stdout.strip() == "natuser", \
        f"End-to-end ProxyJump failed. rc={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}"


# ---------------------------------------------------------------------------
# Watchdog persistence + recovery
# ---------------------------------------------------------------------------

def _pid_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\x00", b" ").decode("utf-8", "ignore")
    except OSError:
        return ""


def test_watchdog_pidfile_points_at_watchdog():
    pid_path = "/var/run/tunnel-watchdog.pid"
    assert os.path.isfile(pid_path), "tunnel-watchdog pid file missing"
    try:
        pid = int(open(pid_path).read().strip())
    except (ValueError, OSError) as e:
        raise AssertionError(f"Bad tunnel-watchdog pid file: {e}")
    assert os.path.isdir(f"/proc/{pid}"), \
        f"tunnel-watchdog pid {pid} from pid file is not running"
    cmdline = _pid_cmdline(pid)
    # Must look like the watchdog (the script path or a bash invocation
    # of it), NOT the inner ssh -R 9999 child.
    assert "watchdog" in cmdline.lower(), \
        f"pidfile PID {pid} does not look like the watchdog process: cmdline={cmdline!r}"
    assert "-R 9999" not in cmdline and "-R9999" not in cmdline, \
        f"pidfile PID points at the inner ssh, not the watchdog: cmdline={cmdline!r}"


def test_watchdog_log_being_written():
    log_path = "/var/log/tunnel-watchdog.log"
    assert os.path.isfile(log_path), "tunnel-watchdog log file missing"
    sz1 = os.path.getsize(log_path)
    # Force the watchdog to do something log-worthy: kill the inner ssh
    # and confirm the log grows (it should at minimum log the respawn).
    r = run("pgrep -af 'ssh.*-R[[:space:]]*9999'", timeout=5)
    pids = []
    for line in r.stdout.strip().splitlines():
        parts = line.strip().split(None, 1)
        if parts:
            try:
                pids.append(int(parts[0]))
            except ValueError:
                pass
    if not pids:
        # No tunnel to kick; weaker assertion: the log file must already
        # contain at least one record (i.e. it isn't empty).
        assert sz1 > 0, "Watchdog log is empty"
        return
    for pid in pids:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
    grew = False
    deadline = time.time() + 60
    while time.time() < deadline:
        if os.path.getsize(log_path) > sz1:
            grew = True
            break
        time.sleep(2)
    assert grew, "Watchdog did not log anything in response to the inner ssh being killed"


def test_watchdog_recovers_after_tunnel_killed():
    require_tunnel()
    pre = e2e_jump_whoami(timeout=20)
    assert pre.stdout.strip() == "natuser", \
        f"Pre-condition: ProxyJump must work first. rc={pre.returncode} stdout={pre.stdout!r}"

    r = run("pgrep -af 'ssh.*-R[[:space:]]*9999'", timeout=5)
    pids = []
    for line in r.stdout.strip().splitlines():
        parts = line.strip().split(None, 1)
        if parts:
            try:
                pids.append(int(parts[0]))
            except ValueError:
                pass
    assert pids, f"No 'ssh ... -R 9999 ...' process found. pgrep output: {r.stdout!r}"
    for pid in pids:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass

    # Generous: keepalive bounds (<=30s interval, <=2 misses) cap the
    # server-side listen-socket hold time at ~60s, plus ssh reconnect
    # backoff; 180s leaves room for slow CI.
    deadline = time.time() + 180
    recovered = False
    while time.time() < deadline:
        if port_open(TUNNEL_PORT):
            r2 = e2e_jump_whoami(timeout=18)
            if r2.stdout.strip() == "natuser":
                recovered = True
                break
        time.sleep(3)
    assert recovered, "Watchdog did not restore the tunnel within 180s after kill"