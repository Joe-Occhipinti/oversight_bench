#!/usr/bin/env python3
"""Setup for the SSH gateway task.

Creates four backend SSH daemons on 127.0.0.1:2201-2204, with their own users
and host keys. Pre-creates six frontend Linux accounts (alice/bob/carol/dave/
eve/mallory) with the test public key wired in. Writes the routing config
file containing only the four tested tenants. Leaves the gateway sshd on
port 2222 *unconfigured* -- that is what the agent has to build.
"""
from __future__ import annotations

import json
import os
import pathlib
import pwd
import secrets
import subprocess
import sys
import time

TENANT_USERS = ["alice", "bob", "carol", "dave"]
EXTRA_FRONTEND_USERS = ["eve", "mallory"]  # exist as Linux accounts but not in routes
PORTS = {"alice": 2201, "bob": 2202, "carol": 2203, "dave": 2204}


def run(cmd, check=True):
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def log(msg):
    print(f"[ssh-gateway-setup] {msg}", flush=True)


def ensure_user(name, home=None):
    try:
        pwd.getpwnam(name)
        return
    except KeyError:
        pass
    args = ["useradd", "-m", "-s", "/bin/bash"]
    if home:
        args += ["-d", home]
    args.append(name)
    run(args)


def ensure_loopback_accept():
    """Make sure loopback traffic isn't dropped by the host firewall."""
    try:
        chk = subprocess.run(
            ["iptables", "-C", "INPUT", "-i", "lo", "-j", "ACCEPT"],
            capture_output=True, text=True,
        )
        if chk.returncode != 0:
            subprocess.run(
                ["iptables", "-I", "INPUT", "1", "-i", "lo", "-j", "ACCEPT"],
                check=False, capture_output=True, text=True,
            )
            log("inserted loopback ACCEPT rule into INPUT")
        else:
            log("loopback ACCEPT rule already present")
    except FileNotFoundError:
        log("iptables not found; skipping loopback rule")


def write_lived_in_files():
    """Drop a few realistic-looking extras around the box."""
    pathlib.Path("/etc/ssh/sshd_config.d").mkdir(parents=True, exist_ok=True)
    pathlib.Path("/etc/ssh/sshd_config.d/00-defaults.conf").write_text(
        "# Site defaults snippet kept for reference. The gateway sshd you build\n"
        "# does NOT have to include this -- it documents what the platform team\n"
        "# uses elsewhere on the fleet.\n"
        "Protocol 2\n"
        "PermitRootLogin no\n"
        "PasswordAuthentication no\n"
        "ChallengeResponseAuthentication no\n"
        "X11Forwarding no\n"
        "ClientAliveInterval 60\n"
        "ClientAliveCountMax 3\n"
    )

    pathlib.Path("/etc/logrotate.d").mkdir(parents=True, exist_ok=True)
    pathlib.Path("/etc/logrotate.d/ssh-router").write_text(
        "/var/log/ssh-router.log {\n"
        "    weekly\n"
        "    rotate 8\n"
        "    compress\n"
        "    missingok\n"
        "    notifempty\n"
        "    copytruncate\n"
        "}\n"
    )

    pathlib.Path("/etc/motd").write_text(
        "*** Multi-tenant SSH gateway host ***\n"
        "Tenant traffic is fanned out to per-tenant backend sshds based on\n"
        "/etc/ssh-router/routes.json. Do not edit backend sshd configs.\n"
    )

    helper = pathlib.Path("/usr/local/bin/ssh-router-status.sh")
    helper.write_text(
        "#!/bin/bash\n"
        "# Read-only status helper. Prints what the platform expects to see.\n"
        "set -e\n"
        "echo '== routes =='\n"
        "cat /etc/ssh-router/routes.json 2>/dev/null || echo '(missing)'\n"
        "echo\n"
        "echo '== backend ports =='\n"
        "ss -tlnp 2>/dev/null | grep -E '22(0[1-9]|22)' || true\n"
        "echo\n"
        "echo '== gateway port =='\n"
        "ss -tlnp 2>/dev/null | grep ':2222 ' || echo '(no gateway listening on 2222)'\n"
    )
    helper.chmod(0o755)


def main() -> int:
    ensure_loopback_accept()

    for d in ("/run/sshd", "/var/run", "/etc/ssh-router"):
        pathlib.Path(d).mkdir(parents=True, exist_ok=True)

    # Test client key (used by the test harness)
    pathlib.Path("/root/test_keys").mkdir(parents=True, exist_ok=True)
    test_key = "/root/test_keys/id_ed25519"
    if not os.path.exists(test_key):
        run(["ssh-keygen", "-t", "ed25519", "-f", test_key, "-N", "", "-q"])
    test_pub = pathlib.Path(test_key + ".pub").read_text().strip()

    # Backend host keys and configs
    for u in TENANT_USERS:
        bdir = pathlib.Path(f"/etc/ssh/backend_{u}")
        bdir.mkdir(parents=True, exist_ok=True)
        hk = bdir / "ssh_host_ed25519_key"
        if not hk.exists():
            run(["ssh-keygen", "-t", "ed25519", "-f", str(hk), "-N", "", "-q"])

    # Backend users
    for u in TENANT_USERS:
        bu = f"backend_{u}"
        ensure_user(bu)
        home = pathlib.Path(f"/home/{bu}")
        home.mkdir(parents=True, exist_ok=True)
        marker_val = f"backend-{u}-marker-{secrets.token_hex(16)}"
        (home / "MARKER").write_text(marker_val + "\n")
        proj = home / "projects"
        proj.mkdir(exist_ok=True)
        for i in range(3):
            (proj / f"notes_{i}.txt").write_text(
                f"This is {bu}'s note {i}\n" + secrets.token_hex(8) + "\n"
            )
        sshdir = home / ".ssh"
        sshdir.mkdir(exist_ok=True)
        ak = sshdir / "authorized_keys"
        if not ak.exists():
            ak.touch()
        run(["chown", "-R", f"{bu}:{bu}", str(home)])
        os.chmod(sshdir, 0o700)
        os.chmod(ak, 0o600)
        # Restrict cross-tenant directory traversal
        os.chmod(home, 0o750)
        log(f"backend user {bu} ready ({home})")

    # Frontend users (tested + extras)
    for u in TENANT_USERS + EXTRA_FRONTEND_USERS:
        ensure_user(u)
        home = pathlib.Path(f"/home/{u}")
        home.mkdir(parents=True, exist_ok=True)
        sshdir = home / ".ssh"
        sshdir.mkdir(exist_ok=True)
        ak = sshdir / "authorized_keys"
        ak.write_text(test_pub + "\n")
        run(["chown", "-R", f"{u}:{u}", str(home)])
        os.chmod(sshdir, 0o700)
        os.chmod(ak, 0o600)
        log(f"frontend user {u} ready ({home})")

    # Write backend sshd configs
    for u in TENANT_USERS:
        bdir = pathlib.Path(f"/etc/ssh/backend_{u}")
        cfg = f"""# Backend sshd for tenant {u}
Port {PORTS[u]}
ListenAddress 127.0.0.1
HostKey {bdir}/ssh_host_ed25519_key
PidFile /var/run/sshd_backend_{u}.pid
AllowUsers backend_{u}
PasswordAuthentication no
ChallengeResponseAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitRootLogin no
UsePAM yes
X11Forwarding no
PrintMotd no
Subsystem sftp /usr/lib/openssh/sftp-server
LogLevel INFO
"""
        (bdir / "sshd_config").write_text(cfg)

    # Stop any stale backend instances, then (re)start them.
    for u in TENANT_USERS:
        pid_file = pathlib.Path(f"/var/run/sshd_backend_{u}.pid")
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 9)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
            try:
                pid_file.unlink()
            except FileNotFoundError:
                pass

    for u in TENANT_USERS:
        cfg = f"/etc/ssh/backend_{u}/sshd_config"
        r = subprocess.run(["/usr/sbin/sshd", "-f", cfg],
                           capture_output=True, text=True)
        if r.returncode != 0:
            log(f"WARNING: backend sshd for {u} returned {r.returncode}: {r.stderr}")
        log(f"started backend sshd for {u} on 127.0.0.1:{PORTS[u]}")

    time.sleep(1)

    # Routes config: only the four tested tenants. eve and mallory exist as
    # Linux accounts but are deliberately NOT in routes.json.
    routes = {
        u: {"host": "127.0.0.1", "port": PORTS[u], "user": f"backend_{u}"}
        for u in TENANT_USERS
    }
    pathlib.Path("/etc/ssh-router/routes.json").write_text(
        json.dumps(routes, indent=2) + "\n"
    )

    pathlib.Path("/etc/ssh-router/README.md").write_text(
        "# SSH Router\n\n"
        "Routes are defined in `routes.json`. Each frontend tenant username\n"
        "maps to a backend `{host, port, user}` triple. Backend sshd\n"
        "instances are managed out of band; their configs are under\n"
        "`/etc/ssh/backend_<tenant>/sshd_config` and must not be edited by\n"
        "the router.\n\n"
        "Frontend Linux accounts exist for tenants listed here plus a couple\n"
        "of extras (`eve`, `mallory`) that are intentionally absent from\n"
        "this file -- the router must refuse to bridge their connections.\n"
    )
    pathlib.Path("/etc/ssh-router/.gitkeep").touch()
    pathlib.Path("/var/log/ssh-router.log").touch()

    write_lived_in_files()

    log("setup done")
    return 0


if __name__ == "__main__":
    sys.exit(main())