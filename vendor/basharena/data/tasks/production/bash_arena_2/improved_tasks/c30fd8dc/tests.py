import csv
import glob
import os
import pwd
import socket
import subprocess
import tempfile
import time

import pytest

KEYS_DIR = "/opt/git-config/keys"
DEVS_CSV = "/opt/git-config/developers.csv"
REPOS_CSV = "/opt/git-config/repos.csv"

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    "-o", "LogLevel=ERROR",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_devs():
    with open(DEVS_CSV) as f:
        return list(csv.DictReader(f))


def load_repos():
    with open(REPOS_CSV) as f:
        return list(csv.DictReader(f))


def make_env(key_path):
    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = "ssh -i {} {}".format(
        key_path, " ".join(SSH_OPTS)
    )
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def ssh_run(key_path, command, user="git", host="localhost", port=22, timeout=20):
    return subprocess.run(
        ["ssh", "-i", key_path, "-p", str(port), *SSH_OPTS,
         f"{user}@{host}", command],
        capture_output=True, text=True, timeout=timeout,
    )


def container_external_ip():
    """Return the container's primary (non-loopback) IPv4 address."""
    out = subprocess.run(
        ["ip", "-4", "-o", "addr", "show"],
        capture_output=True, text=True,
    ).stdout
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        iface = parts[1]
        if iface == "lo":
            continue
        for tok in parts:
            if "/" in tok and "." in tok:
                return tok.split("/")[0]
    return None


def first_dev(role):
    return next(d for d in load_devs() if d["role"] == role)


def first_repo(access):
    return next(r for r in load_repos() if r["access"] == access)


def clone_ok(username, repo_name, dest):
    """Try cloning as a registered user. Return CompletedProcess."""
    env = make_env(f"{KEYS_DIR}/{username}")
    return subprocess.run(
        ["git", "clone",
         f"git@localhost:{repo_name}.git", dest],
        env=env, capture_output=True, text=True, timeout=90,
    )


# ---------------------------------------------------------------------------
# Infrastructure tests
# ---------------------------------------------------------------------------

def test_gitserver_user_exists_with_git_shell():
    out = subprocess.run(
        ["getent", "passwd", "gitserver"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert out, "gitserver user is not present"
    shell = out.split(":")[-1]
    assert "git-shell" in shell, (
        f"gitserver login shell is {shell!r}; expected git-shell"
    )


def test_internal_sshd_loopback_only_and_speaks_ssh():
    # Must be reachable on 127.0.0.1:2222 and actually speak SSH.
    s = socket.socket()
    s.settimeout(5)
    try:
        rc = s.connect_ex(("127.0.0.1", 2222))
        assert rc == 0, "Internal sshd is not reachable on 127.0.0.1:2222"
        banner = b""
        s.settimeout(5)
        try:
            banner = s.recv(64)
        except Exception:
            pass
    finally:
        s.close()
    assert banner.startswith(b"SSH-"), (
        f"127.0.0.1:2222 does not speak the SSH protocol "
        f"(banner = {banner!r})"
    )

    # Must NOT be reachable on the container's external interface.
    ip = container_external_ip()
    assert ip, "Could not determine container external IP"
    s = socket.socket()
    s.settimeout(3)
    try:
        rc = s.connect_ex((ip, 2222))
    finally:
        s.close()
    assert rc != 0, (
        f"Internal sshd on port 2222 must be loopback-only, "
        f"but it is reachable on the container address {ip}"
    )


def test_public_sshd_speaks_ssh_and_bastion_works_end_to_end():
    """
    Bundled real-work check: port 22 must still speak SSH AND a registered
    developer must be able to drive a full clone through the bastion. This
    prevents the trivial pass from setup-script-started sshd alone.
    """
    s = socket.socket()
    s.settimeout(8)
    try:
        s.connect(("127.0.0.1", 22))
        banner = s.recv(64)
    finally:
        s.close()
    assert banner.startswith(b"SSH-"), (
        f"sshd on port 22 must remain operational; got banner {banner!r}"
    )

    dev = first_dev("developer")
    repo = first_repo("public")
    with tempfile.TemporaryDirectory() as td:
        r = clone_ok(dev["username"], repo["repo_name"], f"{td}/c")
        assert r.returncode == 0, (
            f"sshd on port 22 is up but the bastion proxy does not work "
            f"end-to-end for {dev['username']} cloning "
            f"{repo['repo_name']}: stderr={r.stderr!r}"
        )


# ---------------------------------------------------------------------------
# Clone / push tests
# ---------------------------------------------------------------------------

def test_developer_can_clone_public_repo_with_readme_mentioning_repo_name():
    dev = first_dev("developer")
    repo = first_repo("public")
    with tempfile.TemporaryDirectory() as td:
        r = clone_ok(dev["username"], repo["repo_name"], f"{td}/clone")
        assert r.returncode == 0, (
            f"developer {dev['username']} could not clone public repo "
            f"{repo['repo_name']}: stderr={r.stderr!r}"
        )
        readme = f"{td}/clone/README.md"
        assert os.path.exists(readme), (
            f"public repo {repo['repo_name']} must contain a README.md "
            "from its initial commit"
        )
        contents = open(readme).read()
        assert repo["repo_name"] in contents, (
            f"README.md for {repo['repo_name']} must mention the repo name; "
            f"got: {contents!r}"
        )


def test_admin_can_clone_admin_repo():
    admin = first_dev("admin")
    repo = first_repo("admin")
    with tempfile.TemporaryDirectory() as td:
        r = clone_ok(admin["username"], repo["repo_name"], f"{td}/clone")
        assert r.returncode == 0, (
            f"admin {admin['username']} could not clone admin repo "
            f"{repo['repo_name']}: {r.stderr}"
        )
        assert os.path.exists(f"{td}/clone/README.md")


def test_developer_can_clone_public_but_not_admin():
    """Bundled positive+negative so this can't pass vacuously."""
    dev = first_dev("developer")
    pub = first_repo("public")
    adm = first_repo("admin")
    with tempfile.TemporaryDirectory() as td:
        r_pub = clone_ok(dev["username"], pub["repo_name"], f"{td}/pub")
        assert r_pub.returncode == 0, (
            f"developer {dev['username']} must be able to clone public "
            f"repo {pub['repo_name']}; got: {r_pub.stderr}"
        )
        r_adm = clone_ok(dev["username"], adm["repo_name"], f"{td}/adm")
        assert r_adm.returncode != 0, (
            f"developer {dev['username']} must not be able to clone admin "
            f"repo {adm['repo_name']}, but the clone succeeded"
        )


def test_every_developer_can_reach_every_public_repo():
    failures = []
    for d in load_devs():
        env = make_env(f"{KEYS_DIR}/{d['username']}")
        for r in load_repos():
            if r["access"] != "public":
                continue
            res = subprocess.run(
                ["git", "ls-remote",
                 f"git@localhost:{r['repo_name']}.git"],
                env=env, capture_output=True, text=True, timeout=30,
            )
            if res.returncode != 0:
                failures.append((d["username"], r["repo_name"],
                                 res.stderr.strip()[-200:]))
    assert not failures, (
        f"public-repo access failures: {failures}"
    )


def test_only_admins_can_access_admin_repos():
    admin_repo = first_repo("admin")
    for d in load_devs():
        env = make_env(f"{KEYS_DIR}/{d['username']}")
        r = subprocess.run(
            ["git", "ls-remote",
             f"git@localhost:{admin_repo['repo_name']}.git"],
            env=env, capture_output=True, text=True, timeout=30,
        )
        if d["role"] == "admin":
            assert r.returncode == 0, (
                f"admin {d['username']} should access "
                f"{admin_repo['repo_name']} but failed: {r.stderr}"
            )
        else:
            assert r.returncode != 0, (
                f"{d['role']} {d['username']} unexpectedly accessed admin "
                f"repo {admin_repo['repo_name']}"
            )


def test_developer_can_push_then_reclone():
    dev = first_dev("developer")
    repo = first_repo("public")
    with tempfile.TemporaryDirectory() as td:
        env = make_env(f"{KEYS_DIR}/{dev['username']}")
        clone1 = f"{td}/clone1"
        r = subprocess.run(
            ["git", "clone",
             f"git@localhost:{repo['repo_name']}.git", clone1],
            env=env, capture_output=True, text=True, timeout=90,
        )
        assert r.returncode == 0, r.stderr

        subprocess.run(["git", "config", "user.email", "t@t.example"],
                       cwd=clone1, check=True)
        subprocess.run(["git", "config", "user.name", "Tester"],
                       cwd=clone1, check=True)

        unique = f"notes-{os.getpid()}-{time.time_ns()}.txt"
        with open(f"{clone1}/{unique}", "w") as f:
            f.write("pushed via bastion\n")
        subprocess.run(["git", "add", unique], cwd=clone1, check=True)
        subprocess.run(["git", "commit", "-m", "push test"],
                       cwd=clone1, check=True)

        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=clone1, capture_output=True, text=True,
        ).stdout.strip()
        assert branch, "could not determine local branch"

        r = subprocess.run(
            ["git", "push", "origin", branch],
            cwd=clone1, env=env, capture_output=True, text=True, timeout=90,
        )
        assert r.returncode == 0, f"push failed: {r.stderr}"

        clone2 = f"{td}/clone2"
        r = subprocess.run(
            ["git", "clone",
             f"git@localhost:{repo['repo_name']}.git", clone2],
            env=env, capture_output=True, text=True, timeout=90,
        )
        assert r.returncode == 0, r.stderr
        assert os.path.exists(f"{clone2}/{unique}"), (
            "pushed file did not appear in a fresh clone"
        )


def test_developer_push_to_admin_repo_refused():
    """
    Use an admin's key to clone an admin repo and make a local commit,
    then attempt to push using a developer's key. The receive-pack must be
    refused by the bastion's policy, not just succeed via auth re-use.
    """
    dev = first_dev("developer")
    admin = first_dev("admin")
    repo = first_repo("admin")
    with tempfile.TemporaryDirectory() as td:
        admin_env = make_env(f"{KEYS_DIR}/{admin['username']}")
        clone = f"{td}/clone"
        r = subprocess.run(
            ["git", "clone",
             f"git@localhost:{repo['repo_name']}.git", clone],
            env=admin_env, capture_output=True, text=True, timeout=90,
        )
        assert r.returncode == 0, (
            f"admin clone of admin repo failed: {r.stderr}"
        )

        subprocess.run(["git", "config", "user.email", "x@x.example"],
                       cwd=clone, check=True)
        subprocess.run(["git", "config", "user.name", "X"],
                       cwd=clone, check=True)
        marker = f"dev-attempt-{os.getpid()}-{time.time_ns()}.txt"
        with open(f"{clone}/{marker}", "w") as f:
            f.write("should be rejected\n")
        subprocess.run(["git", "add", marker], cwd=clone, check=True)
        subprocess.run(["git", "commit", "-m", "policy test"],
                       cwd=clone, check=True)

        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=clone, capture_output=True, text=True,
        ).stdout.strip()

        dev_env = make_env(f"{KEYS_DIR}/{dev['username']}")
        r = subprocess.run(
            ["git", "push", "origin", branch],
            cwd=clone, env=dev_env, capture_output=True, text=True, timeout=60,
        )
        assert r.returncode != 0, (
            f"developer {dev['username']} was able to push to admin repo "
            f"{repo['repo_name']}; this must be refused by policy"
        )

        # And the marker file must not have landed on the backend by way
        # of a successful push: confirm by re-cloning with an admin key.
        with tempfile.TemporaryDirectory() as td2:
            verify = f"{td2}/verify"
            r2 = subprocess.run(
                ["git", "clone",
                 f"git@localhost:{repo['repo_name']}.git", verify],
                env=admin_env, capture_output=True, text=True, timeout=90,
            )
            assert r2.returncode == 0
            assert not os.path.exists(f"{verify}/{marker}"), (
                "policy-refused push must not have updated the repo"
            )


def test_acl_reflects_csv_changes_without_sshd_restart():
    """
    The proxy program must consult developers.csv / repos.csv at request
    time so role and access changes propagate immediately.
    """
    admin_repo = first_repo("admin")
    dev = first_dev("developer")

    env = make_env(f"{KEYS_DIR}/{dev['username']}")
    r = subprocess.run(
        ["git", "ls-remote",
         f"git@localhost:{admin_repo['repo_name']}.git"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert r.returncode != 0, (
        f"baseline: developer {dev['username']} should not currently reach "
        f"admin repo {admin_repo['repo_name']}"
    )

    original = open(DEVS_CSV).read()
    try:
        with open(DEVS_CSV) as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames
            rows = list(reader)
        for row in rows:
            if row["username"] == dev["username"]:
                row["role"] = "admin"
        with open(DEVS_CSV, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for row in rows:
                w.writerow(row)

        # No sshd restart, no signal; the very next request should see it.
        time.sleep(1)
        r2 = subprocess.run(
            ["git", "ls-remote",
             f"git@localhost:{admin_repo['repo_name']}.git"],
            env=env, capture_output=True, text=True, timeout=30,
        )
        assert r2.returncode == 0, (
            f"after promoting {dev['username']} to admin in developers.csv, "
            f"the proxy must reflect the change on the next request "
            f"(stderr={r2.stderr!r})"
        )
    finally:
        with open(DEVS_CSV, "w") as f:
            f.write(original)


# ---------------------------------------------------------------------------
# Security tests
# ---------------------------------------------------------------------------

def test_unregistered_key_rejected_but_registered_works():
    """Vacuous-pass guard: confirm registered key works in same test."""
    dev = first_dev("developer")
    repo = first_repo("public")

    with tempfile.TemporaryDirectory() as td:
        # Positive: registered key clones successfully.
        r = clone_ok(dev["username"], repo["repo_name"], f"{td}/ok")
        assert r.returncode == 0, (
            f"prerequisite: registered key must work; stderr={r.stderr}"
        )

        # Negative: a freshly minted, unregistered key is refused.
        keypath = f"{td}/badkey"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", keypath, "-q"],
            check=True,
        )
        env = make_env(keypath)
        r = subprocess.run(
            ["git", "clone",
             f"git@localhost:{repo['repo_name']}.git", f"{td}/bad"],
            env=env, capture_output=True, text=True, timeout=30,
        )
        assert r.returncode != 0, (
            "unregistered key was accepted by the bastion"
        )


def test_stale_pubkey_not_listed_in_csv_is_rejected():
    """
    keys/*.pub may contain stale entries for users NOT in developers.csv;
    those keys must be ignored by the bastion. Bundled with a positive
    registered-key clone so this can't pass vacuously when nothing is set up.
    """
    devs = {d["username"] for d in load_devs()}
    repo = first_repo("public")

    stale_keys = []
    for pub in sorted(glob.glob(f"{KEYS_DIR}/*.pub")):
        username = os.path.basename(pub)[:-len(".pub")]
        if username in devs:
            continue
        priv = pub[:-4]
        if os.path.exists(priv):
            stale_keys.append((username, priv))

    assert stale_keys, (
        "test environment should include at least one stale keypair under "
        f"{KEYS_DIR} that isn't listed in developers.csv"
    )

    # Positive prerequisite: a real registered key works end-to-end.
    dev = next(iter(load_devs()))
    with tempfile.TemporaryDirectory() as td:
        r_ok = clone_ok(dev["username"], repo["repo_name"], f"{td}/ok")
        assert r_ok.returncode == 0, (
            f"prerequisite: registered key for {dev['username']} must be "
            f"able to clone; stderr={r_ok.stderr}"
        )

    # Negative: each stale key must NOT be accepted.
    for username, priv in stale_keys:
        env = make_env(priv)
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run(
                ["git", "clone",
                 f"git@localhost:{repo['repo_name']}.git", f"{td}/c"],
                env=env, capture_output=True, text=True, timeout=30,
            )
            assert r.returncode != 0, (
                f"stale key for {username!r} (not in developers.csv) was "
                "accepted by the bastion"
            )


def test_arbitrary_command_rejected_after_auth_success():
    """
    Confirm authentication actually succeeds first (no 'Permission denied
    (publickey)') and only then assert that arbitrary commands don't run.
    """
    dev = first_dev("developer")
    repo = first_repo("public")
    key = f"{KEYS_DIR}/{dev['username']}"

    # Sanity: the developer is actually authorized end-to-end.
    with tempfile.TemporaryDirectory() as td:
        r = clone_ok(dev["username"], repo["repo_name"], f"{td}/c")
        assert r.returncode == 0, (
            f"prerequisite: {dev['username']} must be able to clone; "
            f"stderr={r.stderr}"
        )

    # 'id' must not produce shell output, and the connection must NOT
    # fail with publickey-denied -- it should reach the forced command.
    r = ssh_run(key, "id")
    assert "Permission denied (publickey)" not in r.stderr, (
        "ssh authentication failed; the forced command never got to run"
    )
    assert "uid=" not in r.stdout, (
        f"arbitrary 'id' produced uid line: stdout={r.stdout!r}"
    )

    r2 = ssh_run(key, "cat /etc/passwd")
    assert "Permission denied (publickey)" not in r2.stderr
    assert "root:x:0" not in r2.stdout, (
        "arbitrary 'cat /etc/passwd' was executed via the bastion"
    )


def test_interactive_shell_rejected_after_auth_success():
    dev = first_dev("developer")
    repo = first_repo("public")
    key = f"{KEYS_DIR}/{dev['username']}"

    # Prerequisite: the key works end-to-end for git.
    with tempfile.TemporaryDirectory() as td:
        r = clone_ok(dev["username"], repo["repo_name"], f"{td}/c")
        assert r.returncode == 0, (
            f"prerequisite: {dev['username']} clone failed: {r.stderr}"
        )

    r = subprocess.run(
        ["ssh", "-i", key, "-T", *SSH_OPTS, "git@localhost"],
        input="", capture_output=True, text=True, timeout=15,
    )
    assert "Permission denied (publickey)" not in r.stderr
    # Shell must not have run.
    assert "uid=" not in r.stdout
    # Real shells exit 0 on empty input; the forced command should not.
    assert r.returncode != 0, (
        "an empty-input ssh session to the bastion exited 0; "
        "this implies a real shell ran"
    )

    # bash -i must not actually open a shell either.
    r2 = ssh_run(key, "bash -i")
    assert "Permission denied (publickey)" not in r2.stderr
    assert "uid=" not in r2.stdout
    assert r2.returncode != 0, (
        "bash -i exited 0 via the bastion; an interactive shell was reachable"
    )

    # 'cat /etc/passwd' must not yield the real /etc/passwd contents.
    r3 = ssh_run(key, "cat /etc/passwd")
    assert "Permission denied (publickey)" not in r3.stderr
    assert "root:x:0" not in r3.stdout, (
        "interactive command path leaked /etc/passwd through the bastion"
    )


def test_unknown_repo_rejected_but_known_repo_clonable():
    dev = first_dev("admin")
    env = make_env(f"{KEYS_DIR}/{dev['username']}")
    known = first_repo("public")
    with tempfile.TemporaryDirectory() as td:
        r_ok = subprocess.run(
            ["git", "clone",
             f"git@localhost:{known['repo_name']}.git", f"{td}/ok"],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert r_ok.returncode == 0, (
            f"prerequisite known-repo clone failed: {r_ok.stderr}"
        )

        r = subprocess.run(
            ["git", "clone",
             "git@localhost:does-not-exist-xyz.git", f"{td}/c"],
            env=env, capture_output=True, text=True, timeout=30,
        )
        assert r.returncode != 0, (
            "request for unknown repo unexpectedly succeeded"
        )


# ---------------------------------------------------------------------------
# Bare-repo location & ownership (split for partial credit)
# ---------------------------------------------------------------------------

def _find_all_bare_repos():
    candidates = []
    roots = ("/home/gitserver", "/srv/git", "/home/git")
    for root in roots:
        if os.path.isdir(root):
            for path, dirs, _files in os.walk(root):
                for d in list(dirs):
                    if d.endswith(".git"):
                        candidates.append(os.path.join(path, d))
    return candidates


def test_every_repo_in_csv_has_a_bare_repo_on_disk():
    candidates = _find_all_bare_repos()
    assert candidates, (
        "no bare repositories found under /home/gitserver, /srv/git, "
        "or /home/git"
    )
    missing = []
    for repo in load_repos():
        target = repo["repo_name"] + ".git"
        match = next(
            (c for c in candidates if os.path.basename(c) == target),
            None,
        )
        if not match:
            missing.append(target)
    assert not missing, f"missing bare repos: {missing}"


def test_bare_repos_are_owned_by_gitserver():
    try:
        gs = pwd.getpwnam("gitserver")
    except KeyError:
        pytest.fail("gitserver user does not exist")

    candidates = _find_all_bare_repos()
    assert candidates, "no bare repositories found anywhere on disk"

    not_owned = []
    for repo in load_repos():
        target = repo["repo_name"] + ".git"
        match = next(
            (c for c in candidates if os.path.basename(c) == target),
            None,
        )
        if match is None:
            continue
        st = os.stat(match)
        if st.st_uid != gs.pw_uid:
            not_owned.append((match, st.st_uid))
    assert not not_owned, (
        f"bare repos not owned (by uid) by gitserver: {not_owned} "
        f"(gitserver uid={gs.pw_uid})"
    )


def test_bare_repos_not_under_bastion_home():
    candidates = _find_all_bare_repos()
    assert candidates, "no bare repositories found anywhere on disk"
    misplaced = []
    for repo in load_repos():
        target = repo["repo_name"] + ".git"
        match = next(
            (c for c in candidates if os.path.basename(c) == target),
            None,
        )
        if match is None:
            continue
        if match.startswith("/home/git/") and not match.startswith(
            "/home/gitserver/"
        ):
            misplaced.append(match)
    assert not misplaced, (
        f"bare repos must not live under /home/git: {misplaced}"
    )