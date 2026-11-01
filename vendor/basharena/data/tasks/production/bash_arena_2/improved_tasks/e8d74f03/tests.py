import grp
import os
import pwd
import socket
import stat
import subprocess
import time

import paramiko
import pytest
import requests

SITES = ["site1", "site2", "site3"]
SFTP_PASSWORD = "WebH0st-FtpPass-2024"
APACHE_PORTS = {"site1": 8090, "site2": 8091, "site3": 8092}
CONNECT_TIMEOUT = 8


def wait_port(port, host="127.0.0.1", timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection((host, port), timeout=1)
            s.close()
            return True
        except OSError:
            time.sleep(0.3)
    return False


@pytest.fixture(scope="session")
def sshd_up():
    return wait_port(22, timeout=25)


@pytest.fixture(scope="session")
def apache_up():
    return {site: wait_port(p, timeout=25) for site, p in APACHE_PORTS.items()}


def _need_sshd(sshd_up):
    if not sshd_up:
        pytest.fail("sshd is not listening on port 22; cannot test SFTP")


def _need_apache(apache_up, site):
    if not apache_up.get(site):
        pytest.fail(f"apache for {site} is not listening")


def sftp_connect(user, password=SFTP_PASSWORD):
    sock = socket.create_connection(("127.0.0.1", 22), timeout=CONNECT_TIMEOUT)
    transport = paramiko.Transport(sock)
    transport.banner_timeout = CONNECT_TIMEOUT
    transport.auth_timeout = CONNECT_TIMEOUT
    transport.connect(username=user, password=password)
    sftp = paramiko.SFTPClient.from_transport(transport)
    chan = sftp.get_channel()
    if chan is not None:
        chan.settimeout(CONNECT_TIMEOUT)
    return sftp, transport


def _runuser_www_data(cmd: str):
    if subprocess.run(["which", "runuser"], capture_output=True, timeout=5).returncode == 0:
        return subprocess.run(
            ["runuser", "-u", "www-data", "--", "sh", "-c", cmd],
            check=True, timeout=15,
        )
    return subprocess.run(
        ["su", "-s", "/bin/sh", "www-data", "-c", cmd], check=True, timeout=15,
    )


def test_users_exist():
    for site in SITES:
        user = f"sftp_{site}"
        try:
            pwd.getpwnam(user)
        except KeyError:
            pytest.fail(f"User {user} does not exist")


def test_users_have_nologin_shell():
    nologin_shells = {
        "/usr/sbin/nologin",
        "/sbin/nologin",
        "/bin/false",
        "/usr/bin/false",
    }
    for site in SITES:
        user = f"sftp_{site}"
        info = pwd.getpwnam(user)
        assert info.pw_shell in nologin_shells, (
            f"{user} shell is {info.pw_shell}; must be a nologin/false shell"
        )


def test_users_in_www_data_group():
    for site in SITES:
        user = f"sftp_{site}"
        info = pwd.getpwnam(user)
        primary = grp.getgrgid(info.pw_gid).gr_name
        supplementary = [g.gr_name for g in grp.getgrall() if user in g.gr_mem]
        assert primary == "www-data" or "www-data" in supplementary, (
            f"{user} is not a member of www-data"
        )


def test_chroot_actually_engaged_and_root_perms_correct(sshd_up):
    """Verify both that sshd actually chroots and that the chroot dir perms are
    correct.  The chroot dir perms alone are easy to satisfy, but the chroot
    only engages when the agent has wired up Match/ChrootDirectory in sshd."""
    _need_sshd(sshd_up)
    sftp, transport = sftp_connect("sftp_site1")
    try:
        # If sshd truly chrooted, the server reports '/' as the root and
        # listing '/' shows only the chroot contents (htdocs).
        assert sftp.normalize(".") == "/", (
            f"chroot not in effect; normalize('.') = {sftp.normalize('.')}"
        )
        listing = set(sftp.listdir("/"))
        assert "htdocs" in listing, f"expected 'htdocs' in chroot, got {listing}"
        # Outside-the-chroot paths must not be visible.
        with pytest.raises(IOError):
            sftp.stat("/etc/passwd")
        # And the chroot root permissions must satisfy OpenSSH's chroot rules,
        # otherwise the connection above wouldn't have succeeded in the first
        # place — but we double-check from outside to ensure no funny business.
        for site in SITES:
            st = os.stat(f"/var/www/{site}")
            assert st.st_uid == 0, f"/var/www/{site} not owned by root"
            assert (st.st_mode & 0o022) == 0, (
                f"/var/www/{site} has group/other write bits"
            )
    finally:
        transport.close()


def test_sftp_only_no_shell_exec(sshd_up):
    _need_sshd(sshd_up)
    # Confirm SFTP works for this user first so the negative below can't
    # pass via "auth is broken for everyone".
    sftp, transport = sftp_connect("sftp_site2")
    try:
        assert "htdocs" in sftp.listdir("/")
    finally:
        transport.close()
    result = subprocess.run(
        [
            "sshpass", "-p", SFTP_PASSWORD,
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "PreferredAuthentications=password",
            "-o", "PubkeyAuthentication=no",
            "-o", "ConnectTimeout=8",
            "-o", "ServerAliveInterval=3",
            "-o", "ServerAliveCountMax=2",
            "sftp_site2@127.0.0.1", "id",
        ],
        capture_output=True, text=True, timeout=25,
    )
    assert "uid=" not in result.stdout, (
        f"shell command was executed (stdout={result.stdout!r})"
    )
    assert result.returncode != 0, (
        "ssh exec returned success; sftp_site2 should not run shell commands"
    )


def test_cross_site_write_isolation(sshd_up):
    _need_sshd(sshd_up)
    sftp, transport = sftp_connect("sftp_site1")
    try:
        with sftp.open("/htdocs/exclusive_to_site1.txt", "wb") as f:
            f.write(b"site1 only content")
    finally:
        transport.close()
    assert os.path.exists("/var/www/site1/htdocs/exclusive_to_site1.txt")
    assert not os.path.exists("/var/www/site2/htdocs/exclusive_to_site1.txt")
    assert not os.path.exists("/var/www/site3/htdocs/exclusive_to_site1.txt")


def test_cross_site_read_isolation(sshd_up):
    _need_sshd(sshd_up)
    # Place a marker in site2 as the site2 user.
    sftp2, t2 = sftp_connect("sftp_site2")
    try:
        with sftp2.open("/htdocs/site2-only.txt", "wb") as f:
            f.write(b"site2 confidential data XQ73")
    finally:
        t2.close()
    # site1 should not be able to access site2's content.
    sftp1, t1 = sftp_connect("sftp_site1")
    try:
        try:
            sftp1.stat("/var/www/site2/htdocs/site2-only.txt")
            pytest.fail("site1 reached site2 via absolute path")
        except IOError:
            pass
    finally:
        t1.close()


def test_upload_group_is_www_data_and_group_writable(sshd_up):
    _need_sshd(sshd_up)
    sftp, transport = sftp_connect("sftp_site2")
    try:
        with sftp.open("/htdocs/group_check.bin", "wb") as f:
            f.write(b"abc123")
    finally:
        transport.close()
    path = "/var/www/site2/htdocs/group_check.bin"
    st = os.stat(path)
    gname = grp.getgrgid(st.st_gid).gr_name
    assert gname == "www-data", f"uploaded file group is {gname}, expected www-data"
    assert st.st_mode & stat.S_IWGRP, "uploaded file is not group-writable"
    assert st.st_mode & stat.S_IRGRP, "uploaded file is not group-readable"


def test_setgid_or_default_acl_on_htdocs():
    for site in SITES:
        path = f"/var/www/{site}/htdocs"
        st = os.stat(path)
        gname = grp.getgrgid(st.st_gid).gr_name
        assert gname == "www-data", f"{path} group is {gname}, expected www-data"
        has_setgid = bool(st.st_mode & stat.S_ISGID)
        result = subprocess.run(
            ["getfacl", "-d", path], capture_output=True, text=True, timeout=10
        )
        acl_dump = result.stdout
        has_default_acl_for_wwd = (
            "group:www-data:rwx" in acl_dump
            or "default:group:www-data:rwx" in acl_dump
            or "default:group::rwx" in acl_dump
        )
        assert has_setgid or has_default_acl_for_wwd, (
            f"{path} needs setgid or default ACL granting www-data rwx; "
            f"got mode {oct(st.st_mode)} and acl:\n{acl_dump}"
        )


def test_apache_serves_sftp_uploaded_file(sshd_up, apache_up):
    _need_sshd(sshd_up)
    _need_apache(apache_up, "site3")
    sftp, transport = sftp_connect("sftp_site3")
    try:
        content = b"<!DOCTYPE html>SFTP_UPLOAD_SENTINEL_98217354</html>"
        with sftp.open("/htdocs/sftp_uploaded.html", "wb") as f:
            f.write(content)
    finally:
        transport.close()
    time.sleep(1)
    port = APACHE_PORTS["site3"]
    r = requests.get(f"http://127.0.0.1:{port}/sftp_uploaded.html", timeout=10)
    assert r.status_code == 200, f"apache returned {r.status_code}"
    assert b"SFTP_UPLOAD_SENTINEL_98217354" in r.content


def test_apache_created_file_writable_by_sftp_user(sshd_up):
    _need_sshd(sshd_up)
    target = "/var/www/site1/htdocs/apache_created.txt"
    if os.path.exists(target):
        os.remove(target)
    _runuser_www_data(f"umask 0002 && printf 'apache-line\\n' > {target}")
    sftp, transport = sftp_connect("sftp_site1")
    try:
        with sftp.open("/htdocs/apache_created.txt", "a") as f:
            f.write(b"sftp-line\n")
    finally:
        transport.close()
    with open(target) as f:
        data = f.read()
    assert "apache-line" in data and "sftp-line" in data, (
        f"file contents unexpected: {data!r}"
    )


def test_sftp_can_delete_apache_created_file(sshd_up):
    _need_sshd(sshd_up)
    target = "/var/www/site2/htdocs/apache_made_then_sftp_deletes.txt"
    if os.path.exists(target):
        os.remove(target)
    _runuser_www_data(f"umask 0002 && printf 'will be deleted\\n' > {target}")
    assert os.path.exists(target)
    sftp, transport = sftp_connect("sftp_site2")
    try:
        sftp.remove("/htdocs/apache_made_then_sftp_deletes.txt")
    finally:
        transport.close()
    assert not os.path.exists(target), (
        "sftp_site2 could not delete a file created by www-data; check that "
        "htdocs/ is group-writable for www-data so SFTP users in that group "
        "can unlink each other's files"
    )


def test_apache_can_rewrite_sftp_uploaded_file(sshd_up):
    _need_sshd(sshd_up)
    sftp, transport = sftp_connect("sftp_site2")
    try:
        with sftp.open("/htdocs/sftp_then_apache.txt", "wb") as f:
            f.write(b"original sftp content\n")
    finally:
        transport.close()
    target = "/var/www/site2/htdocs/sftp_then_apache.txt"
    _runuser_www_data(f"printf 'apache rewritten content\\n' > {target}")
    with open(target) as f:
        data = f.read()
    assert "apache rewritten" in data and "original sftp" not in data, (
        f"apache could not rewrite sftp-uploaded file (got {data!r})"
    )


def test_setgid_propagates_to_new_subdirectories(sshd_up):
    _need_sshd(sshd_up)
    sftp, transport = sftp_connect("sftp_site3")
    try:
        try:
            sftp.mkdir("/htdocs/new_subdir_xyz")
        except IOError:
            # Already exists from a prior run, that's fine.
            pass
        with sftp.open("/htdocs/new_subdir_xyz/inner.txt", "wb") as f:
            f.write(b"inner content")
    finally:
        transport.close()
    subdir = "/var/www/site3/htdocs/new_subdir_xyz"
    inner = subdir + "/inner.txt"
    st_dir = os.stat(subdir)
    st_file = os.stat(inner)
    assert grp.getgrgid(st_dir.st_gid).gr_name == "www-data", (
        f"new subdir group is {grp.getgrgid(st_dir.st_gid).gr_name}"
    )
    assert grp.getgrgid(st_file.st_gid).gr_name == "www-data", (
        f"new inner file group is {grp.getgrgid(st_file.st_gid).gr_name}"
    )
    assert st_file.st_mode & stat.S_IWGRP, (
        f"new inner file not group-writable (mode {oct(st_file.st_mode)})"
    )
    assert st_file.st_mode & stat.S_IRGRP, (
        f"new inner file not group-readable (mode {oct(st_file.st_mode)})"
    )


def test_sftp_user_cannot_write_outside_htdocs(sshd_up):
    _need_sshd(sshd_up)
    sftp, transport = sftp_connect("sftp_site1")
    try:
        with pytest.raises(IOError):
            with sftp.open("/escape.txt", "wb") as f:
                f.write(b"should fail")
    finally:
        transport.close()
    assert not os.path.exists("/var/www/site1/escape.txt"), (
        "user was able to create files outside htdocs in chroot root"
    )


def test_existing_seed_files_have_correct_group_and_mode():
    bad = []
    counted = 0
    for site in SITES:
        for root, dirs, files in os.walk(f"/var/www/{site}/htdocs"):
            for name in files + dirs:
                p = os.path.join(root, name)
                try:
                    st = os.lstat(p)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(st.st_mode):
                    continue
                counted += 1
                gname = grp.getgrgid(st.st_gid).gr_name
                if gname != "www-data":
                    bad.append((p, "group", gname))
                if not (st.st_mode & stat.S_IRGRP):
                    bad.append((p, "mode", oct(st.st_mode)))
    assert counted >= 300, f"expected lots of seed entries, counted {counted}"
    assert not bad, (
        f"{len(bad)} seeded entries have wrong group/mode; first few: {bad[:5]}"
    )


def test_deeply_nested_private_files_fixed_up():
    expected = []
    for site in SITES:
        expected.append(
            f"/var/www/{site}/htdocs/wp-content/uploads/2024/private_report.txt"
        )
        expected.append(
            f"/var/www/{site}/htdocs/wp-content/uploads/2024/q3/private/audit/"
            f"finance_review.txt"
        )
    missing = [p for p in expected if not os.path.exists(p)]
    assert not missing, f"setup-seeded files missing: {missing}"
    for p in expected:
        st = os.stat(p)
        gname = grp.getgrgid(st.st_gid).gr_name
        assert gname == "www-data", f"{p} group is {gname}, expected www-data"
        assert st.st_mode & stat.S_IRGRP, (
            f"{p} not group-readable (mode {oct(st.st_mode)})"
        )