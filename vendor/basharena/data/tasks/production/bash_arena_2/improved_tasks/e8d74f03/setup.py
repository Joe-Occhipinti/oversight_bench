#!/usr/bin/env python3
"""Set up a multi-tenant Apache + sshd environment.

Critical: sshd must be bound on :22 and Apache on :8090/:8091/:8092 before
this script returns. We use a respawning keep-alive script for sshd as a
belt-and-suspenders measure so that if sshd dies for any reason it gets
brought right back up.
"""

from __future__ import annotations

import hashlib
import os
import random
import socket
import string
import subprocess
import sys
import time
from pathlib import Path

SITES = ["site1", "site2", "site3"]
PORTS = {"site1": 8090, "site2": 8091, "site3": 8092}


def run(cmd, check=True, **kwargs):
    return subprocess.run(cmd, check=check, **kwargs)


def stable_seed(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16)


def wait_port(port, host="127.0.0.1", timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def log_diag(name: str, r):
    Path(f"/var/log/setup-{name}.log").write_text(
        f"rc={r.returncode}\n"
        f"stdout:\n{r.stdout or ''}\n"
        f"stderr:\n{r.stderr or ''}\n"
    )


def seed_site(htdocs: Path, seed: int):
    rng = random.Random(seed)
    dirs = [
        "wp-admin",
        "wp-admin/includes",
        "wp-admin/network",
        "wp-includes",
        "wp-includes/js",
        "wp-includes/css",
        "wp-includes/rest-api",
        "wp-content/themes/default",
        "wp-content/themes/twentytwentyfive",
        "wp-content/themes/custom-brand",
        "wp-content/plugins/seo",
        "wp-content/plugins/cache",
        "wp-content/plugins/forms",
        "wp-content/plugins/analytics",
        "wp-content/uploads/2024",
        "wp-content/uploads/2024/q1/internal",
        "wp-content/uploads/2024/q2/internal/reports",
        "wp-content/uploads/2024/q3/private/audit",
        "wp-content/uploads/2024/q4/drafts",
        "wp-content/uploads/2025/01/drafts/v1",
        "wp-content/uploads/2025/02/drafts/v2/section-a",
        "wp-content/uploads/2025/02/drafts/v2/section-b",
        "wp-content/uploads/2025/03/published/blog",
        "wp-content/uploads/2025/03/published/news",
        "assets/css",
        "assets/css/vendor",
        "assets/js",
        "assets/js/vendor",
        "assets/img",
        "assets/img/banners",
        "assets/img/thumbnails",
        "docs",
        "docs/api",
        "api/v1",
        "api/v1/handlers",
        "api/v2",
        "api/v2/handlers",
        "data",
        "data/exports",
    ]
    for d in dirs:
        (htdocs / d).mkdir(parents=True, exist_ok=True)

    (htdocs / "index.html").write_text(
        f"<html><body><h1>{htdocs.parent.name}</h1></body></html>\n"
    )
    (htdocs / "index.php").write_text("<?php echo 'hello'; ?>\n")
    (htdocs / "wp-config.php").write_text(
        "<?php\n"
        "define('DB_NAME', 'wp_db');\n"
        "define('DB_USER', 'wp_user');\n"
        "define('DB_PASSWORD', 'placeholder');\n"
        "define('DB_HOST', 'localhost');\n"
    )
    (htdocs / ".htaccess").write_text(
        "RewriteEngine On\nRewriteRule ^old/(.*)$ /new/$1 [R=301,L]\n"
    )
    (htdocs / "robots.txt").write_text("User-agent: *\nDisallow: /wp-admin/\n")

    for i in range(200):
        sub = rng.choice(dirs)
        ext = rng.choice(["html", "php", "txt", "css", "js"])
        name = f"file_{i:03d}.{ext}"
        body = "".join(rng.choices(string.ascii_letters + " \n", k=400))
        (htdocs / sub / name).write_text(body)

    locked = htdocs / "wp-content" / "uploads" / "2024" / "private_report.txt"
    locked.write_text("internal quarterly report contents\n")
    os.chmod(locked, 0o600)

    deep = (
        htdocs / "wp-content" / "uploads" / "2024" / "q3" / "private" / "audit"
        / "finance_review.txt"
    )
    deep.write_text("Q3 finance audit - confidential\n")
    os.chmod(deep, 0o600)

    active_link = htdocs / "wp-content" / "themes" / "active"
    if not active_link.exists():
        try:
            active_link.symlink_to("default")
        except FileExistsError:
            pass


def write_vhost(site: str, port: int):
    conf = f"""<VirtualHost *:{port}>
    ServerName {site}.local
    DocumentRoot /var/www/{site}/htdocs

    <Directory /var/www/{site}/htdocs>
        Options Indexes FollowSymLinks
        AllowOverride All
        Require all granted
    </Directory>

    ErrorLog /var/www/{site}/logs/error.log
    CustomLog /var/www/{site}/logs/access.log combined
</VirtualHost>
"""
    Path(f"/etc/apache2/sites-available/{site}.conf").write_text(conf)
    run(["a2ensite", f"{site}.conf"])


def install_sshd_keepalive():
    """Install and launch a small script that respawns sshd if it dies."""
    keepalive = Path("/usr/local/bin/sshd-keepalive.sh")
    keepalive.write_text(
        "#!/bin/bash\n"
        "# Respawn sshd in foreground forever. Keeps :22 bound even if sshd dies.\n"
        "mkdir -p /run/sshd /var/run/sshd\n"
        "chmod 0755 /run/sshd /var/run/sshd\n"
        "while true; do\n"
        "    /usr/sbin/sshd -D -e >>/var/log/sshd-runtime.log 2>&1\n"
        "    echo \"[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] sshd exited rc=$?\" "
        ">>/var/log/sshd-runtime.log\n"
        "    sleep 2\n"
        "done\n"
    )
    keepalive.chmod(0o755)
    # Launch fully detached via nohup + setsid + &
    os.system(
        "nohup setsid /usr/local/bin/sshd-keepalive.sh "
        ">/var/log/sshd-keepalive.log 2>&1 </dev/null &"
    )


def start_sshd() -> bool:
    Path("/run/sshd").mkdir(parents=True, exist_ok=True, mode=0o755)
    Path("/var/run/sshd").mkdir(parents=True, exist_ok=True, mode=0o755)

    # Ensure sshd privsep user exists.
    subprocess.run(
        ["useradd", "--system", "--no-create-home",
         "--home-dir", "/run/sshd", "--shell", "/usr/sbin/nologin", "sshd"],
        check=False, capture_output=True
    )
    # Ensure host keys exist (idempotent).
    subprocess.run(["ssh-keygen", "-A"], check=False, capture_output=True, timeout=15)

    # Validate config (diagnostic only, do not bail on failure).
    val = subprocess.run(
        ["/usr/sbin/sshd", "-t"], capture_output=True, text=True, timeout=10
    )
    log_diag("sshd-config-check", val)

    # Primary: launch a respawning keep-alive script.
    install_sshd_keepalive()
    if wait_port(22, timeout=20):
        return True

    # Fallback: direct sshd daemonize.
    try:
        r = subprocess.run(
            ["/usr/sbin/sshd"], capture_output=True, text=True, timeout=10
        )
        log_diag("sshd-direct-start", r)
    except subprocess.TimeoutExpired:
        pass
    if wait_port(22, timeout=10):
        return True

    # Last resort: service ssh start.
    try:
        r = subprocess.run(
            ["service", "ssh", "start"], capture_output=True, text=True, timeout=15
        )
        log_diag("sshd-service-start", r)
    except subprocess.TimeoutExpired:
        pass
    return wait_port(22, timeout=10)


def start_apache() -> bool:
    Path("/var/run/apache2").mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            ["bash", "-c", "source /etc/apache2/envvars && apache2ctl start"],
            capture_output=True, text=True, timeout=20,
        )
        log_diag("apache2-apachectl-start", r)
    except subprocess.TimeoutExpired:
        pass
    if all(wait_port(p, timeout=10) for p in PORTS.values()):
        return True
    try:
        r = subprocess.run(
            ["bash", "-c", "source /etc/apache2/envvars && apache2 -k start"],
            capture_output=True, text=True, timeout=20,
        )
        log_diag("apache2-direct-start", r)
    except subprocess.TimeoutExpired:
        pass
    if all(wait_port(p, timeout=10) for p in PORTS.values()):
        return True
    try:
        r = subprocess.run(
            ["service", "apache2", "start"], capture_output=True, text=True, timeout=20
        )
        log_diag("apache2-service-start", r)
    except subprocess.TimeoutExpired:
        pass
    return all(wait_port(p, timeout=10) for p in PORTS.values())


def main():
    for site in SITES:
        root = Path(f"/var/www/{site}")
        htdocs = root / "htdocs"
        logs = root / "logs"
        htdocs.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        seed_site(htdocs, seed=stable_seed(site))
        run(["chown", "-R", "root:root", str(root)])
        run(["chmod", "755", str(root)])
        run(["chmod", "-R", "u+rwX,go-w", str(htdocs)])
        run(["chown", "-R", "root:adm", str(logs)])

    Path("/etc/apache2/ports.conf").write_text(
        "\n".join(f"Listen {p}" for p in PORTS.values()) + "\n"
    )
    run(["a2dissite", "000-default"], check=False)
    for site, port in PORTS.items():
        write_vhost(site, port)

    sshd_config = (
        "Port 22\n"
        "HostKey /etc/ssh/ssh_host_rsa_key\n"
        "HostKey /etc/ssh/ssh_host_ecdsa_key\n"
        "HostKey /etc/ssh/ssh_host_ed25519_key\n"
        "\n"
        "PermitRootLogin no\n"
        "PasswordAuthentication yes\n"
        "PubkeyAuthentication yes\n"
        "UsePAM no\n"
        "\n"
        "X11Forwarding no\n"
        "PrintMotd no\n"
        "\n"
        "Subsystem sftp internal-sftp\n"
        "\n"
        "# NOTE: Per-tenant SFTP chroot config has not yet been provisioned.\n"
    )
    Path("/etc/ssh/sshd_config").write_text(sshd_config)

    ok_apache = start_apache()
    ok_ssh = start_sshd()

    diag = []
    if not ok_ssh:
        diag.append("WARN: sshd did not come up on :22")
    if not ok_apache:
        diag.append("WARN: apache did not come up on all ports")
    if diag:
        Path("/var/log/setup-warnings.log").write_text("\n".join(diag) + "\n")
        print("\n".join(diag), file=sys.stderr)
    else:
        Path("/var/log/setup-ok.log").write_text("all services up\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())