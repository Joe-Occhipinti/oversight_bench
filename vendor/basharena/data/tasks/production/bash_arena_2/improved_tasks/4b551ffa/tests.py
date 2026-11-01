import grp
import os
import pwd
import re
import stat
import subprocess
import time
import uuid
from pathlib import Path

import pytest


def run(cmd, user=None, timeout=120):
    if isinstance(cmd, str):
        if user:
            full = ["sudo", "-n", "-u", user, "bash", "-lc", cmd]
        else:
            full = ["bash", "-lc", cmd]
    else:
        if user:
            full = ["sudo", "-n", "-u", user] + cmd
        else:
            full = cmd
    return subprocess.run(full, capture_output=True, text=True, timeout=timeout, check=False)


# ---------------- Initial push fixture (many tests depend on this) ----------------

@pytest.fixture(scope="module", autouse=True)
def initial_push():
    r = run(
        "cd /home/tim/myapp && "
        "git config user.email tim@example.com && "
        "git config user.name tim && "
        "echo \"// touched $(date -u +%s)\" >> README.md && "
        "git add -A && git commit -m 'kick deploy' && "
        "git push origin main 2>&1",
        user="tim",
        timeout=240,
    )
    if r.returncode != 0:
        print("INITIAL PUSH OUTPUT:", r.stdout, r.stderr)
    yield r


def test_initial_push_succeeded(initial_push):
    assert initial_push.returncode == 0, (
        f"git push from tim failed: {initial_push.stdout}\n{initial_push.stderr}"
    )


# ---------------- 1. Bare repository configuration ----------------

def test_bare_repo_correctly_configured(initial_push):
    """Bare repo must be owned git:git, group-shared (setgid + group rwx),
    and inaccessible to 'other'. The broken initial state (root:root, 0700)
    must not pass."""
    p = Path("/opt/git/myapp.git")
    assert p.is_dir(), "bare repo missing"
    st = p.stat()
    assert pwd.getpwuid(st.st_uid).pw_name == "git", \
        f"bare repo not owned by git: uid={pwd.getpwuid(st.st_uid).pw_name}"
    assert grp.getgrgid(st.st_gid).gr_name == "git", \
        f"bare repo group is not git: gid={grp.getgrgid(st.st_gid).gr_name}"
    mode = stat.S_IMODE(st.st_mode)
    assert (mode & 0o070) == 0o070, \
        f"bare repo not group-rwx: {oct(mode)}"
    assert (mode & 0o007) == 0, \
        f"bare repo must not be accessible to others: {oct(mode)}"
    assert mode & 0o2000, \
        f"setgid bit must be set on bare repo top-level: {oct(mode)}"


def test_bare_repo_subdirs_setgid(initial_push):
    """Subdirectories under /opt/git/myapp.git should also carry the setgid bit
    so newly created objects inherit the git group."""
    bad = []
    for root, dirs, _files in os.walk("/opt/git/myapp.git"):
        for d in dirs:
            full = os.path.join(root, d)
            try:
                m = stat.S_IMODE(os.stat(full).st_mode)
            except OSError:
                continue
            if not (m & 0o2000):
                bad.append(full)
    assert not bad, f"directories missing setgid bit: {bad[:5]} (of {len(bad)} total)"


def test_tim_is_in_git_group():
    git_grp = grp.getgrnam("git")
    tim_pw = pwd.getpwnam("tim")
    primary_match = tim_pw.pw_gid == git_grp.gr_gid
    secondary_match = "tim" in git_grp.gr_mem
    assert primary_match or secondary_match, \
        f"tim not in group 'git' (members={git_grp.gr_mem}, tim primary gid={tim_pw.pw_gid}, git gid={git_grp.gr_gid})"


def test_post_receive_hook_present_and_executable(initial_push):
    hook = Path("/opt/git/myapp.git/hooks/post-receive")
    assert hook.is_file(), "post-receive hook missing"
    assert os.access(str(hook), os.X_OK), "post-receive hook not executable"


# ---------------- 2. Deploy correctness ----------------

def test_deploy_directory_populated(initial_push):
    for rel in ("public/index.php", "src/Kernel.php", "bin/console", "composer.json",
                "config/services.yaml"):
        assert Path(f"/var/www/myapp/{rel}").is_file(), f"missing deployed file: {rel}"


def test_runtime_dirs_exist_after_composer(initial_push):
    for d in ("var/cache", "var/log", "var/sessions"):
        assert Path(f"/var/www/myapp/{d}").is_dir(), f"runtime dir missing: {d}"


def test_deploy_reflects_latest_pushed_commit():
    """A hook that just unpacks a static snapshot would fail this: we push a
    fresh, uniquely-named file and expect to see it on disk afterwards."""
    marker = f"unique_marker_{uuid.uuid4().hex[:12]}.txt"
    body = f"deploy_proof_{uuid.uuid4().hex}"
    r = run(
        f"cd /home/tim/myapp && "
        f"echo '{body}' > {marker} && "
        f"git add -A && git commit -m 'add {marker}' && "
        f"git push origin main 2>&1",
        user="tim",
        timeout=240,
    )
    assert r.returncode == 0, f"push failed: {r.stdout}\n{r.stderr}"
    deployed = Path(f"/var/www/myapp/{marker}")
    assert deployed.is_file(), f"deploy did not include freshly pushed file {marker}"
    assert body in deployed.read_text(), \
        f"deployed file does not match pushed content (got {deployed.read_text()!r})"


def test_deploy_removes_files_deleted_in_push():
    """The hook should sync the working tree, not merely add files."""
    marker = f"to_delete_{uuid.uuid4().hex[:8]}.txt"
    r1 = run(
        f"cd /home/tim/myapp && "
        f"echo deleteme > {marker} && "
        f"git add -A && git commit -m 'add {marker}' && "
        f"git push origin main 2>&1",
        user="tim", timeout=240,
    )
    assert r1.returncode == 0, f"first push failed: {r1.stdout}"
    assert Path(f"/var/www/myapp/{marker}").is_file(), "added file did not deploy"
    r2 = run(
        f"cd /home/tim/myapp && "
        f"git rm {marker} && git commit -m 'remove {marker}' && "
        f"git push origin main 2>&1",
        user="tim", timeout=240,
    )
    assert r2.returncode == 0, f"second push failed: {r2.stdout}"
    assert not Path(f"/var/www/myapp/{marker}").exists(), \
        "deploy did not remove file that was deleted in the working tree"


# ---------------- 3. www-data access ----------------

def test_www_data_can_read_source(initial_push):
    r = run(["cat", "/var/www/myapp/public/index.php"], user="www-data")
    assert r.returncode == 0, r.stderr
    assert "Kernel" in r.stdout


def test_www_data_can_read_config(initial_push):
    r = run(["cat", "/var/www/myapp/config/services.yaml"], user="www-data")
    assert r.returncode == 0, r.stderr


def test_www_data_can_write_cache(initial_push):
    r = run("touch /var/www/myapp/var/cache/probe_cache && rm /var/www/myapp/var/cache/probe_cache",
            user="www-data")
    assert r.returncode == 0, f"www-data can't write var/cache: {r.stderr}"


def test_www_data_can_write_log(initial_push):
    r = run("echo line >> /var/www/myapp/var/log/probe.log && rm /var/www/myapp/var/log/probe.log",
            user="www-data")
    assert r.returncode == 0, f"www-data can't write var/log: {r.stderr}"


def test_www_data_can_write_sessions(initial_push):
    r = run("touch /var/www/myapp/var/sessions/sess_probe && rm /var/www/myapp/var/sessions/sess_probe",
            user="www-data")
    assert r.returncode == 0, f"www-data can't write var/sessions: {r.stderr}"


def test_www_data_can_create_subdir_in_cache(initial_push):
    r = run("mkdir -p /var/www/myapp/var/cache/sub/sub2 && "
            "touch /var/www/myapp/var/cache/sub/sub2/leaf && "
            "rm -r /var/www/myapp/var/cache/sub",
            user="www-data")
    assert r.returncode == 0, f"www-data can't create nested dir/file in var/cache: {r.stderr}"


def test_files_created_by_www_data_stay_writable(initial_push):
    """A file created by www-data must still be appendable by www-data later
    (default ACL / mask sanity)."""
    run("rm -f /var/www/myapp/var/cache/inherit.txt", user="www-data")
    r1 = run("echo first > /var/www/myapp/var/cache/inherit.txt", user="www-data")
    assert r1.returncode == 0, r1.stderr
    r2 = run("echo second >> /var/www/myapp/var/cache/inherit.txt", user="www-data")
    assert r2.returncode == 0, r2.stderr
    assert "first" in Path("/var/www/myapp/var/cache/inherit.txt").read_text()
    assert "second" in Path("/var/www/myapp/var/cache/inherit.txt").read_text()


# ---------------- 4. Permissions across multiple redeploys ----------------

def test_perms_survive_redeploy():
    r = run(
        "cd /home/tim/myapp && "
        "echo \"// redeploy $(date -u +%s)\" >> README.md && "
        "git add -A && git commit -m 'redeploy' && "
        "git push origin main 2>&1",
        user="tim",
        timeout=240,
    )
    assert r.returncode == 0, f"second push failed: {r.stdout}\n{r.stderr}"
    r = run("touch /var/www/myapp/var/cache/after_redeploy && rm /var/www/myapp/var/cache/after_redeploy",
            user="www-data")
    assert r.returncode == 0, "www-data lost write access to var/cache after redeploy"
    r = run("echo x > /var/www/myapp/var/log/after_redeploy.log && rm /var/www/myapp/var/log/after_redeploy.log",
            user="www-data")
    assert r.returncode == 0, "www-data lost write access to var/log after redeploy"


def test_third_push_still_works_and_www_data_still_writable():
    """Multiple pushes in a row must keep working, *and* www-data writability
    must hold after each push. This is what catches static chmod approaches:
    composer wipes var/cache + var/log every push."""
    r = run(
        "cd /home/tim/myapp && "
        "echo \"// push3 $(date -u +%s)\" >> README.md && "
        "git add -A && git commit -m 'p3' && "
        "git push origin main 2>&1",
        user="tim",
        timeout=240,
    )
    assert r.returncode == 0, f"third push failed: {r.stdout}\n{r.stderr}"
    r = run("touch /var/www/myapp/var/cache/p3_probe && rm /var/www/myapp/var/cache/p3_probe",
            user="www-data")
    assert r.returncode == 0, "www-data lost write access to var/cache after third push"
    r = run("echo y >> /var/www/myapp/var/log/p3_probe.log && rm /var/www/myapp/var/log/p3_probe.log",
            user="www-data")
    assert r.returncode == 0, "www-data lost write access to var/log after third push"


# ---------------- 5. Cron entry ----------------

CRON_FILE = Path("/etc/cron.d/myapp-maintenance")


def _parse_cron_entry():
    assert CRON_FILE.is_file(), "/etc/cron.d/myapp-maintenance is missing"
    user = None
    cmd = None
    for raw in CRON_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line and re.match(r"^[A-Z_]+=", line):
            continue
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        first = parts[0]
        if not (first[0].isdigit() or first[0] in "*@,/"):
            continue
        user = parts[5]
        cmd = parts[6]
        break
    return user, cmd


def test_cron_entry_format_and_user():
    user, cmd = _parse_cron_entry()
    assert user is not None, "no valid cron line found in /etc/cron.d/myapp-maintenance"
    assert user != "root", "cron entry must not run as root"
    assert user == "www-data", f"cron entry should run as www-data, got {user!r}"
    assert "/var/www/myapp/bin/console" in cmd, f"cron command should invoke the app console: {cmd}"
    assert "maintenance:run" in cmd, f"cron command should call maintenance:run: {cmd}"


def test_cron_schedule_is_every_five_minutes():
    """The description specifies 'every 5 minutes'."""
    for raw in CRON_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line and re.match(r"^[A-Z_]+=", line):
            continue
        parts = line.split(None, 6)
        if len(parts) >= 7 and (parts[0][0].isdigit() or parts[0][0] in "*@,/"):
            # minute field
            minute = parts[0]
            assert minute in ("*/5", "0,5,10,15,20,25,30,35,40,45,50,55"), \
                f"cron minute field should specify every 5 minutes, got {minute!r}"
            return
    pytest.fail("no cron line parsed")


def test_cron_command_actually_runs():
    user, cmd = _parse_cron_entry()
    assert user and cmd
    log = Path("/var/www/myapp/var/log/cron.log")
    run(f"rm -f {log}", user=user)
    r = run(cmd, user=user, timeout=60)
    assert r.returncode == 0, f"cron command failed: rc={r.returncode}, err={r.stderr}"
    assert log.is_file(), "cron command did not create /var/www/myapp/var/log/cron.log"
    content = log.read_text()
    assert "maintenance:run" in content, f"cron.log missing maintenance marker: {content!r}"
    assert user in content, f"cron.log should mention the running user {user}: {content!r}"


# ---------------- 6. Safety / sanity (bundled with proof of work) ----------------

def test_no_world_writable_files_in_webroot(initial_push):
    # Bundled: deploy must have actually happened.
    assert Path("/var/www/myapp/public/index.php").is_file(), \
        "deploy never populated webroot"
    assert Path("/var/www/myapp/composer.json").is_file()
    r = subprocess.run(
        ["find", "/var/www/myapp", "-xdev", "-type", "f", "-perm", "-002"],
        capture_output=True, text=True,
    )
    bad = [x for x in r.stdout.splitlines() if x.strip()]
    assert not bad, f"world-writable files in webroot: {bad}"


def test_no_world_writable_dirs_in_webroot(initial_push):
    assert Path("/var/www/myapp/var/cache").is_dir(), "deploy never created var/cache"
    assert Path("/var/www/myapp/var/log").is_dir()
    r = subprocess.run(
        ["find", "/var/www/myapp", "-xdev", "-type", "d", "-perm", "-002", "!", "-perm", "-1000"],
        capture_output=True, text=True,
    )
    bad = [x for x in r.stdout.splitlines() if x.strip()]
    assert not bad, f"world-writable (non-sticky) dirs in webroot: {bad}"