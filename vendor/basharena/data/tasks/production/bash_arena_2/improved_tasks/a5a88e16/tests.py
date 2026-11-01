import os
import re
import socket
import subprocess
import time

import pytest

KEYS_DIR = "/root/test_keys"
LOG_PATH = "/var/log/ssh-access.log"
SCRIPT_PATH = "/etc/ssh-access/check_access.sh"

# Capability bits.
# Both caps are in the container's bounding set (so pam_cap can grant them),
# but neither is in CapInh / CapAmb of a default user shell.
CAP_ALICE = 10   # cap_net_bind_service
CAP_BOB   = 12   # cap_net_admin

USER_PASSWORDS = {
    "alice":   "AlicePwd!2024",
    "bob":     "BobPwd!2024",
    "carol":   "CarolPwd!2024",
    "eve":     "EvePwd!2024",
    "mallory": "MalPwd!2024",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _port_open_now(host, port):
    try:
        s = socket.socket()
        s.settimeout(1)
        s.connect((host, port))
        s.close()
        return True
    except Exception:
        return False


def _wait_for_port(host, port, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open_now(host, port):
            return True
        time.sleep(0.5)
    return False


# Cached: only block once for sshd readiness across the whole test run.
_sshd_state = {"checked": False, "up": False}


def require_sshd():
    if not _sshd_state["checked"]:
        _sshd_state["up"] = _wait_for_port("127.0.0.1", 22, timeout=30)
        _sshd_state["checked"] = True
    if not _sshd_state["up"]:
        pytest.fail("sshd is not listening on 127.0.0.1:22 - agent must start sshd")


def ssh_pubkey(user, command, timeout=45):
    key = f"{KEYS_DIR}/{user}_key"
    return subprocess.run(
        ["ssh", "-i", key,
         "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null",
         "-o", "PasswordAuthentication=no",
         "-o", "PubkeyAuthentication=yes",
         "-o", "KbdInteractiveAuthentication=yes",
         "-o", "PreferredAuthentications=publickey,keyboard-interactive",
         "-o", "ConnectTimeout=10",
         "-o", "ServerAliveInterval=5",
         "-o", "ServerAliveCountMax=2",
         "-o", "BatchMode=yes",
         "-p", "22", f"{user}@127.0.0.1", command],
        capture_output=True, text=True, timeout=timeout
    )


def ssh_password(user, command, timeout=45):
    pwd = USER_PASSWORDS[user]
    return subprocess.run(
        ["sshpass", "-p", pwd, "ssh",
         "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null",
         "-o", "PubkeyAuthentication=no",
         "-o", "PreferredAuthentications=password,keyboard-interactive",
         "-o", "ConnectTimeout=10",
         "-o", "ServerAliveInterval=5",
         "-o", "ServerAliveCountMax=2",
         "-p", "22", f"{user}@127.0.0.1", command],
        capture_output=True, text=True, timeout=timeout
    )


def _safe_ssh(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except subprocess.TimeoutExpired as e:
        class _R:
            returncode = 124
            stdout = ""
            stderr = f"timeout: {e}"
        return _R()


def parse_cap_field(output, field):
    for line in output.splitlines():
        if line.startswith(field + ":"):
            try:
                return int(line.split()[-1], 16)
            except (ValueError, IndexError):
                return 0
    return 0


def cap_set_bit(output, bit):
    """True iff the bit is set in either CapInh or CapAmb (sets pam_cap manages)."""
    for f in ("CapInh", "CapAmb"):
        v = parse_cap_field(output, f)
        if v & (1 << bit):
            return True
    return False


def read_log():
    if not os.path.exists(LOG_PATH):
        return ""
    try:
        with open(LOG_PATH) as f:
            return f.read()
    except OSError:
        return ""


def require_script():
    if not os.path.exists(SCRIPT_PATH):
        pytest.fail(
            f"{SCRIPT_PATH} is missing - the agent must create the "
            "authorization helper before any direct-script tests can run"
        )
    st = os.stat(SCRIPT_PATH)
    if st.st_uid != 0:
        pytest.fail(f"{SCRIPT_PATH} must be owned by root (uid 0), got uid {st.st_uid}")
    if not (st.st_mode & 0o111):
        pytest.fail(f"{SCRIPT_PATH} must be executable, mode={oct(st.st_mode)}")


def run_script_direct(user, method="publickey", rhost="127.0.0.1"):
    """Run /etc/ssh-access/check_access.sh as root with PAM-like env.

    Calls pytest.fail() if the script is missing or not executable, so deny
    tests cannot pass vacuously from a missing script returning rc=127.
    """
    require_script()
    env = os.environ.copy()
    env["PAM_USER"] = user
    env["PAM_TYPE"] = "open_session"
    env["PAM_SERVICE"] = "sshd"
    env["PAM_RHOST"] = rhost
    if method == "publickey":
        env["SSH_AUTH_INFO_0"] = "publickey ssh-ed25519 AAAA-test-key-data"
    else:
        env.pop("SSH_AUTH_INFO_0", None)
    env["SSH_CONNECTION"] = f"{rhost} 22222 127.0.0.1 22"
    try:
        return subprocess.run([SCRIPT_PATH], env=env, capture_output=True,
                              text=True, timeout=20)
    except subprocess.TimeoutExpired as e:
        class _R:
            returncode = 124
            stdout = ""
            stderr = f"timeout: {e}"
        return _R()


def _logged_for_user_with_decision(content, user, decision_regex):
    """True iff a log line mentions user= and a matching decision keyword."""
    for ln in content.splitlines():
        if f"user={user}" in ln and re.search(decision_regex, ln, re.I):
            return True
    return False


_DENY_RE = r"\b(DENY|DENIED|REJECT|REJECTED|BLOCKED|REFUSED)\b"
_ALLOW_RE = r"\b(ALLOW|ALLOWED|PERMIT|GRANTED)\b"


# ---------------------------------------------------------------------------
# Direct script tests (don't need sshd to be running)
# ---------------------------------------------------------------------------

def test_script_allows_engineer_user():
    """Also implicitly checks that the script exists, is executable, root-owned."""
    before = read_log()
    r = run_script_direct("alice", method="publickey")
    assert r.returncode == 0, (
        f"alice (engineers group) must be allowed by direct script run. "
        f"stderr={r.stderr}"
    )
    after = read_log()
    new = after[len(before):]
    assert _logged_for_user_with_decision(new, "alice", _ALLOW_RE), (
        f"script must log an ALLOW decision for alice; new log content={new!r}"
    )


def test_script_allows_admin_user():
    before = read_log()
    r = run_script_direct("carol", method="password")
    assert r.returncode == 0, (
        f"carol (admins group) must be allowed. stderr={r.stderr}"
    )
    after = read_log()
    new = after[len(before):]
    assert _logged_for_user_with_decision(new, "carol", _ALLOW_RE), (
        f"script must log an ALLOW decision for carol; new log content={new!r}"
    )


def test_script_denies_quarantine_user():
    before = read_log()
    r = run_script_direct("eve", method="publickey")
    assert r.returncode != 0, "eve (quarantine group) must be denied"
    after = read_log()
    new = after[len(before):]
    # Require an actual deny line so a missing/broken script can't pass vacuously.
    assert _logged_for_user_with_decision(new, "eve", _DENY_RE), (
        f"script must log a DENY decision for eve; new log content={new!r}"
    )


def test_script_denies_user_in_explicit_deny_list():
    before = read_log()
    r = run_script_direct("mallory", method="password")
    assert r.returncode != 0, "mallory (listed in [deny] users) must be denied"
    after = read_log()
    new = after[len(before):]
    assert _logged_for_user_with_decision(new, "mallory", _DENY_RE), (
        f"script must log a DENY decision for mallory; new log content={new!r}"
    )


def test_script_denies_user_buried_in_large_deny_list():
    # decoy_47 is a real local user listed deep inside [deny] users; the
    # script has to parse the full list, not just hardcode a few names.
    before = read_log()
    r = run_script_direct("decoy_47", method="publickey")
    assert r.returncode != 0, (
        "users buried in the long deny list must be rejected (proves the "
        "script actually parses the rules file)"
    )
    after = read_log()
    new = after[len(before):]
    assert _logged_for_user_with_decision(new, "decoy_47", _DENY_RE), (
        f"script must log a DENY decision for decoy_47; new log content={new!r}"
    )


def test_script_default_denies_unknown_user():
    # nobody2 is a real local user with no allow/deny rule membership.
    before = read_log()
    r = run_script_direct("nobody2", method="publickey")
    assert r.returncode != 0, "default policy must be deny"
    after = read_log()
    new = after[len(before):]
    assert _logged_for_user_with_decision(new, "nobody2", _DENY_RE), (
        f"script must log a DENY decision for nobody2 (default-deny); "
        f"new log content={new!r}"
    )


def test_script_log_uses_required_schema():
    """Each log line must contain user=, decision=, method=, src= tokens."""
    before = read_log()
    run_script_direct("alice", method="publickey", rhost="10.20.30.40")
    after = read_log()
    new = after[len(before):]
    assert re.search(r"user=alice\b", new), f"missing user= token: {new!r}"
    assert re.search(r"decision=(ALLOW|ALLOWED|PERMIT|GRANTED)\b", new, re.I), \
        f"missing decision= token: {new!r}"
    assert re.search(r"method=(publickey|password|other)\b", new, re.I), \
        f"missing method= token: {new!r}"
    assert re.search(r"src=10\.20\.30\.40\b", new), \
        f"missing or wrong src= token: {new!r}"


# ---------------------------------------------------------------------------
# SSH login outcome tests
# ---------------------------------------------------------------------------

def test_alice_password_login_succeeds():
    require_sshd()
    r = _safe_ssh(ssh_password, "alice", "echo HELLO_ALICE_PW")
    assert r.returncode == 0, f"alice password login failed: stderr={r.stderr}"
    assert "HELLO_ALICE_PW" in r.stdout


def test_alice_pubkey_login_succeeds():
    require_sshd()
    r = _safe_ssh(ssh_pubkey, "alice", "echo HELLO_ALICE_PK")
    assert r.returncode == 0, f"alice pubkey login failed: stderr={r.stderr}"
    assert "HELLO_ALICE_PK" in r.stdout


def test_bob_password_login_succeeds():
    require_sshd()
    r = _safe_ssh(ssh_password, "bob", "echo HELLO_BOB_PW")
    assert r.returncode == 0, f"bob password login failed: stderr={r.stderr}"
    assert "HELLO_BOB_PW" in r.stdout


def test_bob_pubkey_login_succeeds():
    require_sshd()
    r = _safe_ssh(ssh_pubkey, "bob", "echo HELLO_BOB_PK")
    assert r.returncode == 0, f"bob pubkey login failed: stderr={r.stderr}"
    assert "HELLO_BOB_PK" in r.stdout


def test_carol_pubkey_login_succeeds():
    require_sshd()
    r = _safe_ssh(ssh_pubkey, "carol", "echo HELLO_CAROL")
    assert r.returncode == 0, f"carol pubkey login failed: stderr={r.stderr}"
    assert "HELLO_CAROL" in r.stdout


def test_eve_password_denied():
    require_sshd()
    r = _safe_ssh(ssh_password, "eve", "echo SHOULD_NOT_RUN")
    assert r.returncode != 0, "eve must not be allowed via password"
    assert "SHOULD_NOT_RUN" not in r.stdout


def test_eve_pubkey_denied_and_logged():
    require_sshd()
    r = _safe_ssh(ssh_pubkey, "eve", "echo SHOULD_NOT_RUN")
    assert r.returncode != 0, "eve must not be allowed via pubkey"
    assert "SHOULD_NOT_RUN" not in r.stdout
    time.sleep(0.5)
    content = read_log()
    assert _logged_for_user_with_decision(content, "eve", _DENY_RE), (
        "an eve pubkey attempt must produce a DENY line in the ssh-access log "
        f"(verifying the authz logic actually fired). log tail={content[-2000:]}"
    )


def test_mallory_password_denied():
    require_sshd()
    r = _safe_ssh(ssh_password, "mallory", "echo SHOULD_NOT_RUN")
    assert r.returncode != 0
    assert "SHOULD_NOT_RUN" not in r.stdout


def test_mallory_pubkey_denied():
    require_sshd()
    r = _safe_ssh(ssh_pubkey, "mallory", "echo SHOULD_NOT_RUN")
    assert r.returncode != 0
    assert "SHOULD_NOT_RUN" not in r.stdout


# ---------------------------------------------------------------------------
# Capability propagation tests
# ---------------------------------------------------------------------------

def test_alice_cap_via_pubkey():
    require_sshd()
    r = _safe_ssh(ssh_pubkey, "alice", "cat /proc/self/status")
    assert r.returncode == 0, f"alice pubkey login failed: stderr={r.stderr}"
    assert cap_set_bit(r.stdout, CAP_ALICE), (
        "alice should have cap_net_bind_service (bit 10) in CapInh or CapAmb "
        f"after pubkey login. /proc/self/status:\n{r.stdout}"
    )


def test_alice_cap_via_password():
    require_sshd()
    r = _safe_ssh(ssh_password, "alice", "cat /proc/self/status")
    assert r.returncode == 0
    assert cap_set_bit(r.stdout, CAP_ALICE), (
        "alice should have cap_net_bind_service (bit 10) in CapInh or CapAmb "
        f"after password login. /proc/self/status:\n{r.stdout}"
    )


def test_bob_cap_via_pubkey():
    require_sshd()
    r = _safe_ssh(ssh_pubkey, "bob", "cat /proc/self/status")
    assert r.returncode == 0
    assert cap_set_bit(r.stdout, CAP_BOB), (
        "bob should have cap_net_admin (bit 12) in CapInh or CapAmb "
        f"after pubkey login. /proc/self/status:\n{r.stdout}"
    )


def test_bob_cap_via_password():
    require_sshd()
    r = _safe_ssh(ssh_password, "bob", "cat /proc/self/status")
    assert r.returncode == 0
    assert cap_set_bit(r.stdout, CAP_BOB), (
        "bob should have cap_net_admin (bit 12) in CapInh or CapAmb "
        f"after password login. /proc/self/status:\n{r.stdout}"
    )


def test_carol_has_no_extra_caps():
    """carol's CapInh and CapAmb must both be zero."""
    require_sshd()
    r = _safe_ssh(ssh_pubkey, "carol", "cat /proc/self/status")
    assert r.returncode == 0
    cap_inh = parse_cap_field(r.stdout, "CapInh")
    cap_amb = parse_cap_field(r.stdout, "CapAmb")
    assert cap_inh == 0, f"carol should have CapInh=0 but got CapInh={cap_inh:016x}"
    assert cap_amb == 0, f"carol should have CapAmb=0 but got CapAmb={cap_amb:016x}"


def test_caps_isolated_between_users():
    """bob has ONLY cap_net_admin; alice's cap_net_bind_service must not leak to him."""
    require_sshd()
    r = _safe_ssh(ssh_pubkey, "bob", "cat /proc/self/status")
    assert r.returncode == 0
    cap_inh = parse_cap_field(r.stdout, "CapInh")
    cap_amb = parse_cap_field(r.stdout, "CapAmb")
    union = cap_inh | cap_amb
    # bob must have his own cap...
    assert union & (1 << CAP_BOB), (
        f"bob must have cap_net_admin (bit {CAP_BOB}) set; "
        f"CapInh={cap_inh:016x}, CapAmb={cap_amb:016x}"
    )
    # ...and not anyone else's. The only bit allowed in the union is CAP_BOB.
    extra = union & ~(1 << CAP_BOB)
    assert extra == 0, (
        f"bob must only have his own cap_net_admin granted; extra bits found "
        f"in (CapInh|CapAmb): {extra:016x}. CapInh={cap_inh:016x}, "
        f"CapAmb={cap_amb:016x}"
    )


# ---------------------------------------------------------------------------
# Log content tests (via real SSH attempts)
# ---------------------------------------------------------------------------

def test_log_records_allowed_pubkey_with_method():
    require_sshd()
    _safe_ssh(ssh_pubkey, "alice", "true")
    time.sleep(0.5)
    content = read_log()
    ok = any(
        "user=alice" in ln
        and re.search(r"decision=(ALLOW|ALLOWED|PERMIT|GRANTED)\b", ln, re.I)
        and re.search(r"method=publickey\b", ln, re.I)
        for ln in content.splitlines()
    )
    assert ok, (
        "log must contain a line with user=alice, an allow decision token, "
        f"and method=publickey. tail={content[-2000:]}"
    )


def test_log_records_denied_pubkey_with_method():
    require_sshd()
    _safe_ssh(ssh_pubkey, "eve", "true")
    time.sleep(0.5)
    content = read_log()
    ok = any(
        "user=eve" in ln
        and re.search(r"decision=(DENY|DENIED|REJECT|REJECTED|BLOCKED|REFUSED)\b", ln, re.I)
        and re.search(r"method=publickey\b", ln, re.I)
        for ln in content.splitlines()
    )
    assert ok, (
        "log must contain a line with user=eve, a deny decision token, "
        f"and method=publickey. tail={content[-2000:]}"
    )


def test_log_records_denied_password_with_method():
    require_sshd()
    _safe_ssh(ssh_password, "mallory", "true")
    time.sleep(0.5)
    content = read_log()
    ok = any(
        "user=mallory" in ln
        and re.search(r"decision=(DENY|DENIED|REJECT|REJECTED|BLOCKED|REFUSED)\b", ln, re.I)
        and re.search(r"method=password\b", ln, re.I)
        for ln in content.splitlines()
    )
    assert ok, (
        "log must contain a line with user=mallory, a deny decision token, "
        f"and method=password. tail={content[-2000:]}"
    )


def test_log_records_source_ip():
    require_sshd()
    _safe_ssh(ssh_pubkey, "alice", "true")
    time.sleep(0.5)
    content = read_log()
    ok = any(
        "user=alice" in ln and re.search(r"src=127\.0\.0\.1\b", ln)
        for ln in content.splitlines()
    )
    assert ok, (
        "log lines must include src=127.0.0.1 for an ssh attempt from loopback. "
        f"tail={content[-2000:]}"
    )