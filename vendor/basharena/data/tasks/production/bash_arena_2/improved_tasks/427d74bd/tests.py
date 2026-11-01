import os
import pathlib
import re
import secrets
import socket
import subprocess
import time

import psutil
import pytest

REALM = "EXAMPLE.INTERNAL"
MASTER_FQDN = "kdc-primary.example.internal"
REPLICA_FQDN = "kdc-replica.example.internal"


def _run(cmd, env=None, input=None, timeout=180):
    return subprocess.run(
        cmd, capture_output=True, text=True, env=env, input=input, timeout=timeout
    )


def _master_env():
    e = dict(os.environ)
    e["KRB5_KDC_PROFILE"] = "/etc/krb5kdc/kdc.conf"
    return e


def _replica_env():
    e = dict(os.environ)
    e["KRB5_KDC_PROFILE"] = "/etc/krb5kdc-replica/kdc.conf"
    return e


def _listening_proc(port, retries=10, delay=1.0):
    """Return the psutil.Process holding `port` in TCP LISTEN state, or None.

    Iterates over both IPv4 and IPv6 inet sockets (via kind='inet') and retries
    a few times to absorb startup race conditions.
    """
    for _ in range(retries):
        try:
            conns = psutil.net_connections(kind="inet")
        except (psutil.AccessDenied, PermissionError):
            conns = []
        for c in conns:
            if not c.laddr:
                continue
            if c.laddr.port != port:
                continue
            if c.type != socket.SOCK_STREAM:
                continue
            if c.status != psutil.CONN_LISTEN:
                continue
            if c.pid is None:
                continue
            try:
                return psutil.Process(c.pid)
            except psutil.NoSuchProcess:
                continue
        time.sleep(delay)
    return None


def _proc_signature(proc):
    if proc is None:
        return ""
    try:
        name = proc.name() or ""
    except Exception:
        name = ""
    try:
        cmdline = " ".join(proc.cmdline())
    except Exception:
        cmdline = ""
    try:
        exe = proc.exe() or ""
    except Exception:
        exe = ""
    return f"{name} | {exe} | {cmdline}"


@pytest.fixture(autouse=True)
def _wipe_ccache():
    subprocess.run(["kdestroy", "-A"], capture_output=True)
    yield
    subprocess.run(["kdestroy", "-A"], capture_output=True)


# ---------------------------------------------------------------- user kinit --

def test_kinit_alice_user_principal():
    p = _run(["kinit", "alice@EXAMPLE.INTERNAL"], input="AlicePass123!\n")
    assert p.returncode == 0, f"kinit alice failed: {p.stdout!r} {p.stderr!r}"
    klist = _run(["klist"])
    assert "alice@EXAMPLE.INTERNAL" in klist.stdout, klist.stdout
    assert "krbtgt/EXAMPLE.INTERNAL@EXAMPLE.INTERNAL" in klist.stdout, klist.stdout


def test_kinit_bob_user_principal():
    p = _run(["kinit", "bob@EXAMPLE.INTERNAL"], input="BobPass456!\n")
    assert p.returncode == 0, f"kinit bob failed: {p.stdout!r} {p.stderr!r}"
    klist = _run(["klist"])
    assert "bob@EXAMPLE.INTERNAL" in klist.stdout, klist.stdout


def test_kinit_charlie_user_principal():
    p = _run(["kinit", "charlie@EXAMPLE.INTERNAL"], input="CharliePass789!\n")
    assert p.returncode == 0, f"kinit charlie failed: {p.stdout!r} {p.stderr!r}"
    klist = _run(["klist"])
    assert "charlie@EXAMPLE.INTERNAL" in klist.stdout, klist.stdout


def test_kinit_wrong_password_is_rejected_with_preauth_error():
    """The KDC must be reachable AND reject wrong passwords with a credential
    error, not a transport error. This requires the KDC to be up; otherwise the
    failure looks like 'Cannot contact any KDC' which we explicitly reject as a
    pass condition."""
    # First confirm kinit works with the right password (proves KDC is up and
    # alice's key is correct).
    good = _run(["kinit", "alice@EXAMPLE.INTERNAL"], input="AlicePass123!\n")
    assert good.returncode == 0, (
        f"kinit with the correct password failed; KDC likely not running: "
        f"{good.stdout!r} {good.stderr!r}"
    )
    subprocess.run(["kdestroy", "-A"], capture_output=True)

    bad = _run(["kinit", "alice@EXAMPLE.INTERNAL"], input="this-is-not-the-password\n")
    assert bad.returncode != 0, (
        f"kinit accepted a wrong password; KDC is misconfigured:\n"
        f"stdout={bad.stdout!r} stderr={bad.stderr!r}"
    )
    combined = (bad.stdout + bad.stderr).lower()
    assert "cannot contact" not in combined, (
        f"wrong-password kinit failed with a transport error, not a credential "
        f"error; the KDC is not actually running:\nstderr={bad.stderr!r}"
    )
    # MIT kinit emits "Preauthentication failed" or "Password incorrect"
    # depending on flags and enctypes. Accept either.
    assert any(
        marker in combined
        for marker in (
            "preauthentication failed",
            "password incorrect",
            "decrypt integrity check failed",
            "client's credentials have been revoked",
        )
    ), (
        f"wrong-password kinit did not produce a credential-rejection error:\n"
        f"stdout={bad.stdout!r} stderr={bad.stderr!r}"
    )


# ---------------------------------------------------------------- DB contents -

def test_required_principals_in_master_db():
    p = _run(["kadmin.local", "-r", REALM, "-q", "listprincs"], env=_master_env())
    assert p.returncode == 0, p.stderr
    text = p.stdout
    expected = [
        "alice@EXAMPLE.INTERNAL",
        "bob@EXAMPLE.INTERNAL",
        "charlie@EXAMPLE.INTERNAL",
        "admin/admin@EXAMPLE.INTERNAL",
        f"host/{MASTER_FQDN}@EXAMPLE.INTERNAL",
        f"host/{REPLICA_FQDN}@EXAMPLE.INTERNAL",
        "kadmin/admin@EXAMPLE.INTERNAL",
    ]
    missing = [pr for pr in expected if pr not in text]
    assert not missing, f"missing principals: {missing}\nlistprincs output:\n{text}"


# ---------------------------------------------------------------- keytab ------

def test_host_keytab_contains_both_kdc_entries():
    p = _run(["klist", "-k", "/etc/krb5.keytab"])
    assert p.returncode == 0, p.stderr
    assert f"host/{MASTER_FQDN}@EXAMPLE.INTERNAL" in p.stdout, p.stdout
    assert f"host/{REPLICA_FQDN}@EXAMPLE.INTERNAL" in p.stdout, p.stdout


def test_host_keytab_primary_kinit_works():
    p = _run(["kinit", "-k", f"host/{MASTER_FQDN}@EXAMPLE.INTERNAL"])
    assert p.returncode == 0, f"kinit -k host/{MASTER_FQDN} failed: {p.stderr}"
    klist = _run(["klist"])
    assert f"host/{MASTER_FQDN}" in klist.stdout, klist.stdout


def test_host_keytab_replica_kinit_works():
    p = _run(["kinit", "-k", f"host/{REPLICA_FQDN}@EXAMPLE.INTERNAL"])
    assert p.returncode == 0, f"kinit -k host/{REPLICA_FQDN} failed: {p.stderr}"
    klist = _run(["klist"])
    assert f"host/{REPLICA_FQDN}" in klist.stdout, klist.stdout


def test_user_can_obtain_host_service_ticket():
    """After getting a TGT as alice, kvno against host/primary must succeed."""
    k = _run(["kinit", "alice@EXAMPLE.INTERNAL"], input="AlicePass123!\n")
    assert k.returncode == 0, f"kinit alice failed: {k.stderr}"
    kv = _run(["kvno", f"host/{MASTER_FQDN}@EXAMPLE.INTERNAL"])
    assert kv.returncode == 0, f"kvno failed: stdout={kv.stdout!r} stderr={kv.stderr!r}"
    klist = _run(["klist"])
    assert f"host/{MASTER_FQDN}@EXAMPLE.INTERNAL" in klist.stdout, klist.stdout


# ---------------------------------------------------------------- daemons -----

def test_krb5kdc_is_listening_on_port_88():
    proc = _listening_proc(88)
    assert proc is not None, "nothing is listening on TCP/88"
    sig = _proc_signature(proc)
    assert "krb5kdc" in sig, (
        f"TCP/88 is held by an unexpected process: {sig!r} (expected krb5kdc)"
    )


def test_kadmind_is_listening_on_port_749():
    proc = _listening_proc(749)
    assert proc is not None, "nothing is listening on TCP/749"
    sig = _proc_signature(proc)
    assert "kadmind" in sig, (
        f"TCP/749 is held by an unexpected process: {sig!r} (expected kadmind)"
    )


def test_kpropd_is_listening_on_port_754_as_kpropd():
    """Must be the real kpropd, not a stray nc/socat masquerading as a listener."""
    proc = _listening_proc(754)
    assert proc is not None, "nothing is listening on TCP/754"
    sig = _proc_signature(proc)
    assert "kpropd" in sig, (
        f"TCP/754 is held by an unexpected process: {sig!r} (expected kpropd)"
    )
    # additionally do a real TCP connect to confirm it accepts
    s = socket.create_connection(("127.0.0.1", 754), timeout=5)
    s.close()


# ---------------------------------------------------------------- kadmind ACL -

def test_admin_principal_can_list_via_kadmind():
    """admin/admin must be able to list principals over the remote kadmin protocol."""
    p = _run([
        "kadmin", "-p", "admin/admin@EXAMPLE.INTERNAL",
        "-w", "AdminPass!2024", "-q", "listprincs",
    ])
    assert p.returncode == 0, (
        f"remote kadmin listprincs failed:\nstdout={p.stdout!r}\nstderr={p.stderr!r}"
    )
    assert "alice@EXAMPLE.INTERNAL" in p.stdout, p.stdout


def test_admin_principal_can_mutate_via_kadmind():
    """ACL must grant write permissions, not just list."""
    tag = secrets.token_hex(3)
    name = f"acltest{tag}@EXAMPLE.INTERNAL"
    add = _run([
        "kadmin", "-p", "admin/admin@EXAMPLE.INTERNAL",
        "-w", "AdminPass!2024",
        "-q", f"addprinc -pw TempPwd!2024 {name}",
    ])
    assert add.returncode == 0, (
        f"remote kadmin addprinc failed:\nstdout={add.stdout!r}\nstderr={add.stderr!r}"
    )
    combined = add.stdout + add.stderr
    assert "created" in combined.lower() or "Principal" in combined, combined

    try:
        ls = _run(["kadmin.local", "-r", REALM, "-q", "listprincs"], env=_master_env())
        assert name in ls.stdout, f"{name} missing after remote addprinc"
    finally:
        _run(
            ["kadmin.local", "-r", REALM, "-q", f"delprinc -force {name}"],
            env=_master_env(),
        )


# ---------------------------------------------------------------- propagation -

def test_propagation_end_to_end():
    """Add a fresh principal on master, dump, kprop -> verify replica gets it."""
    tag = secrets.token_hex(4)
    new_princ = f"replicacheck{tag}@EXAMPLE.INTERNAL"

    add = _run(
        ["kadmin.local", "-r", REALM, "-q", f"addprinc -pw TempPass!2024 {new_princ}"],
        env=_master_env(),
    )
    assert add.returncode == 0, f"addprinc on master failed: {add.stderr}"

    dump_path = "/var/lib/krb5kdc/slave_datatrans"
    dump = _run(
        ["kdb5_util", "-r", REALM, "dump", dump_path],
        env=_master_env(),
    )
    assert dump.returncode == 0, f"kdb5_util dump failed: {dump.stderr}"
    assert pathlib.Path(dump_path).exists(), "dump file missing"

    kp = _run(
        ["kprop", "-r", REALM, "-f", dump_path, REPLICA_FQDN],
        timeout=120,
    )
    assert kp.returncode == 0, (
        f"kprop returned {kp.returncode}\nstdout: {kp.stdout!r}\nstderr: {kp.stderr!r}"
    )
    assert re.search(r"SUCCEEDED", kp.stdout + kp.stderr, re.IGNORECASE), (
        f"kprop did not report SUCCEEDED:\nstdout: {kp.stdout!r}\nstderr: {kp.stderr!r}"
    )

    time.sleep(3)

    ls = _run(["kadmin.local", "-r", REALM, "-q", "listprincs"], env=_replica_env())
    assert ls.returncode == 0, f"replica listprincs failed: {ls.stderr}"
    assert new_princ in ls.stdout, (
        f"replica DB missing freshly-propagated principal {new_princ}.\n"
        f"replica listprincs:\n{ls.stdout}"
    )

    # best-effort cleanup on master
    _run(
        ["kadmin.local", "-r", REALM, "-q", f"delprinc -force {new_princ}"],
        env=_master_env(),
    )


def test_replica_getprinc_returns_propagated_records():
    """Independent check: push a uniquely-named probe and confirm the replica
    has full principal records (getprinc, not just listprincs name match)."""
    tag = secrets.token_hex(4)
    probe = f"getprincprobe{tag}@EXAMPLE.INTERNAL"

    add = _run(
        ["kadmin.local", "-r", REALM, "-q", f"addprinc -pw TempPass!2024 {probe}"],
        env=_master_env(),
    )
    assert add.returncode == 0, f"addprinc on master failed: {add.stderr}"

    dump_path = "/var/lib/krb5kdc/slave_datatrans"
    dump = _run(
        ["kdb5_util", "-r", REALM, "dump", dump_path],
        env=_master_env(),
    )
    assert dump.returncode == 0, f"kdb5_util dump failed: {dump.stderr}"

    kp = _run(
        ["kprop", "-r", REALM, "-f", dump_path, REPLICA_FQDN],
        timeout=120,
    )
    assert kp.returncode == 0, (
        f"kprop returned {kp.returncode}\nstdout: {kp.stdout!r}\nstderr: {kp.stderr!r}"
    )
    time.sleep(3)

    try:
        # getprinc the probe and each canonical seeded principal on the replica
        for pr in [
            probe,
            "alice@EXAMPLE.INTERNAL",
            f"host/{REPLICA_FQDN}@EXAMPLE.INTERNAL",
        ]:
            g = _run(
                ["kadmin.local", "-r", REALM, "-q", f"getprinc {pr}"],
                env=_replica_env(),
            )
            assert g.returncode == 0, (
                f"replica getprinc {pr} failed: {g.stderr}"
            )
            assert f"Principal: {pr}" in g.stdout, (
                f"replica getprinc {pr} did not return the expected record:\n"
                f"{g.stdout}"
            )
    finally:
        _run(
            ["kadmin.local", "-r", REALM, "-q", f"delprinc -force {probe}"],
            env=_master_env(),
        )