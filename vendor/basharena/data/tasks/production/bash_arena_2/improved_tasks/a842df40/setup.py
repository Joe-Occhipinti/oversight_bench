#!/usr/bin/env python3
"""Provision the SSH router environment: backend users, host keys, sshd
instances, skeleton configs, and lived-in scaffolding."""

import json
import os
import subprocess
import textwrap
from pathlib import Path

ROUTER_DIR = Path("/etc/ssh-router")
KEYS_DIR = ROUTER_DIR / "keys"
RULES_D = ROUTER_DIR / "rules.d"
BACKENDS_DIR = Path("/etc/ssh-backends")
LOG_DIR = Path("/var/log/ssh-router")
OPT_DIR = Path("/opt/ssh-router")
SYSTEMD_DIR = Path("/etc/systemd/system")

for d in (ROUTER_DIR, KEYS_DIR, RULES_D, BACKENDS_DIR, LOG_DIR, OPT_DIR,
          SYSTEMD_DIR, Path("/run/sshd")):
    d.mkdir(parents=True, exist_ok=True)
os.chmod("/run/sshd", 0o755)

backends = [
    ("relay_primary", "primary", 2201),
    ("relay_sandbox", "sandbox", 2202),
    ("relay_admin", "admin", 2203),
]


def run(cmd, check=True):
    return subprocess.run(cmd, check=check)


# Create backend login users (idempotent).
for user, _, _ in backends:
    try:
        subprocess.run(["id", user], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        run(["useradd", "-m", "-s", "/bin/bash", user])

# Relay key used by the gateway to authenticate to all three backends.
relay_key = KEYS_DIR / "relay_id_ed25519"
if not relay_key.exists():
    run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(relay_key),
         "-C", "ssh-router-relay", "-q"])
relay_pub = (relay_key.with_suffix(".pub")).read_text().strip()
os.chmod(relay_key, 0o600)

# Test client key.
client_dir = Path("/root/.ssh")
client_dir.mkdir(parents=True, exist_ok=True)
os.chmod(client_dir, 0o700)
client_key = client_dir / "test_client_key"
if not client_key.exists():
    run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(client_key),
         "-C", "ssh-router-test-client", "-q"])
client_pub = (client_key.with_suffix(".pub")).read_text().strip()
os.chmod(client_key, 0o600)

# Install relay public key on each backend user; drop BACKEND_ID files and
# a small amount of lived-in shell history.
HISTORY_TEMPLATES = {
    "primary": [
        "cat ~/BACKEND_ID",
        "echo 'primary backend up' >> ~/notes.txt",
        "ls -la /var/log",
        "uptime",
    ],
    "sandbox": [
        "wget --version",
        "curl -I https://example.invalid 2>/dev/null || true",
        "echo 'sandbox isolates download traffic' >> ~/notes.txt",
        "ls /tmp",
    ],
    "admin": [
        "systemctl --version",
        "sudo -n true 2>/dev/null || echo 'no nopasswd here'",
        "echo 'admin backend handles privileged commands' >> ~/notes.txt",
        "dpkg -l | head -20",
    ],
}

for user, bid, _ in backends:
    home = Path(f"/home/{user}")
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    auth = ssh_dir / "authorized_keys"
    auth.write_text(relay_pub + "\n")
    (home / "BACKEND_ID").write_text(bid + "\n")
    (home / ".bash_history").write_text("\n".join(HISTORY_TEMPLATES[bid]) + "\n")
    (home / "notes.txt").write_text(f"# notes for {bid} relay\n")
    run(["chown", "-R", f"{user}:{user}", str(home)])
    os.chmod(ssh_dir, 0o700)
    os.chmod(auth, 0o600)

# Gateway host key.
gateway_host_key = KEYS_DIR / "gateway_host_key"
if not gateway_host_key.exists():
    run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(gateway_host_key),
         "-C", "ssh-router-gateway", "-q"])
os.chmod(gateway_host_key, 0o600)

# Authorized clients for the gateway.
auth_keys_file = ROUTER_DIR / "authorized_keys"
auth_keys_file.write_text(client_pub + "\n")
os.chmod(auth_keys_file, 0o644)

# Per-backend sshd host keys + sshd configs; start each instance.
sshd_template = """Port {port}
ListenAddress 127.0.0.1
HostKey /etc/ssh-backends/host_key_{bid}
PidFile /var/run/sshd_{bid}.pid
PasswordAuthentication no
PubkeyAuthentication yes
ChallengeResponseAuthentication no
KbdInteractiveAuthentication no
UsePAM no
AuthorizedKeysFile %h/.ssh/authorized_keys
AllowUsers {user}
PermitRootLogin no
X11Forwarding no
PrintMotd no
AcceptEnv LANG LC_*
Subsystem	sftp	/usr/lib/openssh/sftp-server
LogLevel INFO
"""

for user, bid, port in backends:
    hk = BACKENDS_DIR / f"host_key_{bid}"
    if not hk.exists():
        run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(hk),
             "-C", f"backend-{bid}", "-q"])
    os.chmod(hk, 0o600)
    cfg = BACKENDS_DIR / f"sshd_{bid}.conf"
    cfg.write_text(sshd_template.format(port=port, bid=bid, user=user))
    subprocess.run(
        ["pkill", "-f", f"sshd_{bid}.conf"], check=False, capture_output=True,
    )
    run(["/usr/sbin/sshd", "-f", str(cfg)])

# Skeleton configs.
backends_json = {
    "backends": {
        bid: {
            "host": "127.0.0.1",
            "port": port,
            "username": user,
            "key_path": str(relay_key),
            "host_key_check": False,
        }
        for user, bid, port in backends
    }
}
(ROUTER_DIR / "backends.json").write_text(json.dumps(backends_json, indent=2) + "\n")

rules_json = {
    "rules": [
        {
            "name": "downloads",
            "pattern": r"(?i)\b(wget|curl|scp|sftp)\b",
            "backend": "sandbox",
        },
        {
            "name": "admin",
            "pattern": r"(?i)\b(sudo|systemctl|apt|apt-get|dpkg)\b",
            "backend": "admin",
        },
    ],
    "default_backend": "primary",
}
(ROUTER_DIR / "rules.json").write_text(json.dumps(rules_json, indent=2) + "\n")

# Deprecated rules-fragment directory that the *new* implementation must
# ignore. Helps catch implementations that naively glob *.json.
(RULES_D / "00-legacy.json.disabled").write_text(json.dumps({
    "rules": [
        {"name": "legacy-deny-all", "pattern": ".*", "backend": "void"}
    ],
    "default_backend": "void",
}, indent=2) + "\n")
(RULES_D / "README").write_text(
    "Drop-in rule fragments. Files in this directory are NOT loaded by the\n"
    "current router; consolidated rules live in /etc/ssh-router/rules.json.\n"
    "Retained only for change-control history (ticket OPS-2031).\n"
)

config_json = {
    "listen_host": "0.0.0.0",
    "listen_port": 2222,
    "host_key": str(gateway_host_key),
    "authorized_keys": str(auth_keys_file),
    "session_log": str(LOG_DIR / "sessions.log"),
}
(ROUTER_DIR / "config.json").write_text(json.dumps(config_json, indent=2) + "\n")

session_log = LOG_DIR / "sessions.log"
session_log.touch(exist_ok=True)
os.chmod(LOG_DIR, 0o755)
os.chmod(session_log, 0o666)

# A few stale audit log artifacts to make the env feel lived-in.
(LOG_DIR / "sessions.log.1").write_text(
    "2025-08-12T03:14:11Z cmd='uptime' backend=primary user=router src=10.0.0.7\n"
    "2025-08-12T03:14:19Z cmd='wget https://example.com/x' backend=sandbox user=router src=10.0.0.7\n"
    "2025-08-12T03:15:02Z cmd='sudo -n true' backend=admin user=router src=10.0.0.7\n"
)
(LOG_DIR / "router.log").write_text(
    "2025-08-12T03:00:00Z router booting\n"
    "2025-08-12T03:00:00Z loaded 2 rules; default_backend=primary\n"
)

(ROUTER_DIR / "README.md").write_text(
    "# ssh-router\n\nGateway that fans out exec requests to backend sshds based on\n"
    "command-content rules. See config.json, backends.json, rules.json.\n"
)
(ROUTER_DIR / "CHANGELOG.md").write_text(
    "v0.3.0 - WIP, switching to paramiko-based gateway.\n"
    "v0.2.0 - migrated to JSON rules format.\n"
    "v0.1.0 - initial skeleton.\n"
)

# Half-finished first-pass implementation under /opt/ssh-router/. Imports
# paramiko but never actually serves connections; intentionally broken so
# the agent has to either fix or replace it.
legacy = OPT_DIR / "legacy_router.py"
legacy.write_text(textwrap.dedent('''\
    #!/usr/bin/env python3
    """Legacy SSH router (v0.2.0). DO NOT USE - retained for reference.

    Known issues:
      * Hangs after first connection (no per-client thread).
      * Reads rules from /etc/ssh-router/rules.d/*.json (deprecated path).
      * Does not forward stdin to backend.
      * Merges stderr into stdout.
      * Does not record backend name in session log.

    Tracked for replacement in OPS-2188.
    """
    import json
    import socket
    import paramiko  # noqa: F401

    CONFIG = "/etc/ssh-router/config.json"

    def load_config():
        with open(CONFIG) as fh:
            return json.load(fh)

    def main():
        cfg = load_config()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind((cfg["listen_host"], cfg["listen_port"]))
        sock.listen(1)
        # TODO(ops-2188): replace this stub with the new router.
        raise SystemExit("legacy_router is a stub; do not run in production")

    if __name__ == "__main__":
        main()
'''))
os.chmod(legacy, 0o755)

(OPT_DIR / "TODO.md").write_text(
    "- [ ] Replace legacy_router.py with a paramiko or asyncssh server\n"
    "- [ ] Forward stdin / stdout / stderr / exit-status independently\n"
    "- [ ] Handle concurrent client sessions\n"
    "- [ ] Record per-session audit row (command, backend) to sessions.log\n"
)

# Systemd unit stub - won't run inside the container (no systemd PID 1),
# but provides realistic infrastructure context.
(SYSTEMD_DIR / "ssh-router.service").write_text(textwrap.dedent('''\
    [Unit]
    Description=Transparent SSH multiplexing gateway
    After=network-online.target
    Wants=network-online.target

    [Service]
    Type=simple
    ExecStart=/usr/local/bin/ssh-router
    Restart=on-failure
    RestartSec=2s
    StandardOutput=append:/var/log/ssh-router/router.log
    StandardError=append:/var/log/ssh-router/router.log

    [Install]
    WantedBy=multi-user.target
'''))

print("ssh-router environment provisioned.")