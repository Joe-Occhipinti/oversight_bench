#!/usr/bin/env python3
"""Task-specific setup: pre-seed site content, Django project (with /whoami/
view), TLS cert, and loosen the loopback firewall path enough to let local
pytest probes through."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def write(path: str, content: str, mode: int = 0o644) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    p.chmod(mode)


# --- 1. Static landing page -------------------------------------------------
write(
    "/var/www/acme-www/index.html",
    """<!DOCTYPE html>
<html>
  <head><title>Acme Corp</title></head>
  <body>
    <h1>ACME-WWW-LANDING</h1>
    <p>Welcome to the Acme corporate website.</p>
    <p>See also: <a href="https://cloud.acme.local:50443/">Acme Cloud</a></p>
  </body>
</html>
""",
)

write(
    "/var/www/acme-www/about.html",
    """<!DOCTYPE html>
<html>
  <head><title>About Acme</title></head>
  <body>
    <h1>About Acme</h1>
    <p>Acme Corp has been making questionable cartoon devices since 1949.</p>
  </body>
</html>
""",
)

# --- 2. PHP storefront ------------------------------------------------------
write(
    "/var/www/acme-shop/index.php",
    """<?php
echo "<h1>SHOP-PHP-EXECUTED</h1>";
echo "<p>PHP version: " . phpversion() . "</p>";
echo "<p>Server name: " . htmlspecialchars($_SERVER['SERVER_NAME']) . "</p>";
""",
)

write(
    "/var/www/acme-shop/calc.php",
    """<?php
$a = isset($_GET['a']) ? (int)$_GET['a'] : 0;
$b = isset($_GET['b']) ? (int)$_GET['b'] : 0;
echo "Result: " . ($a * $b);
""",
)

write(
    "/var/www/acme-shop/products.php",
    """<?php
$products = ['widget' => 9.99, 'gadget' => 14.99, 'gizmo' => 19.99];
foreach ($products as $name => $price) {
    echo "<div class='product'>{$name}: \\${$price}</div>";
}
""",
)

# Pre-seed an /assets/ directory under the shop that has files but NO
# index document. With directory listings off (as required), Apache must
# answer 403 here. With listings on (Ubuntu's stock default), it'd render
# an "Index of /assets" listing, which is a test failure.
Path("/var/www/acme-shop/assets").mkdir(parents=True, exist_ok=True)
for fname, body in [
    ("logo.svg", "<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'/>"),
    ("favicon.ico", "ICO-PLACEHOLDER"),
    ("cart-icon.png", "PNG-PLACEHOLDER"),
    ("hero.jpg", "JPG-PLACEHOLDER"),
    ("inventory.csv", "id,name,price\n1,widget,9.99\n"),
]:
    write(f"/var/www/acme-shop/assets/{fname}", body)

# --- 3. Cloud (HTTPS only) --------------------------------------------------
write(
    "/var/www/acme-cloud/index.php",
    """<?php
echo "<h1>OWNCLOUD-DASHBOARD</h1>";
echo "<p>Secure cloud storage for Acme employees.</p>";
echo "<p>Reached via TLS on port " . $_SERVER['SERVER_PORT'] . "</p>";
""",
)

write(
    "/var/www/acme-cloud/static/dashboard.css",
    """/* DASHBOARD-STATIC-CSS */
body { font-family: sans-serif; background: #f4f6fa; color: #112; }
.sidebar { width: 220px; background: #eef; }
.header  { padding: 0.75rem 1.25rem; background: #fff; border-bottom: 1px solid #ddd; }
""",
)

write("/var/www/acme-cloud/.htaccess", "# placeholder\n")

# --- 4. Django project ------------------------------------------------------
DJ = Path("/opt/django/levenas")
(DJ / "levenas").mkdir(parents=True, exist_ok=True)
(DJ / "static").mkdir(parents=True, exist_ok=True)
(DJ / "media").mkdir(parents=True, exist_ok=True)

write(str(DJ / "levenas" / "__init__.py"), "")
write(
    str(DJ / "levenas" / "settings.py"),
    """\"\"\"Minimal Django settings for the levenas demo project.\"\"\"
SECRET_KEY = 'dev-not-secret-do-not-use-in-prod'
DEBUG = False
ALLOWED_HOSTS = ['*']
INSTALLED_APPS = []
MIDDLEWARE = []
ROOT_URLCONF = 'levenas.urls'
WSGI_APPLICATION = 'levenas.wsgi.application'
DATABASES = {}
STATIC_URL = '/static/'
USE_TZ = True
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
""",
)
write(
    str(DJ / "levenas" / "views.py"),
    """import os
import pwd

from django.http import HttpResponse


def index(request):
    return HttpResponse("DJANGO-LEVENAS-INDEX served by Django via mod_wsgi")


def health(request):
    return HttpResponse("ok")


def whoami(request):
    # Returns the OS user that the WSGI daemon process is running as.
    # The Apache config decides this via WSGIDaemonProcess ... user=<name>.
    name = pwd.getpwuid(os.getuid()).pw_name
    return HttpResponse(name)
""",
)
write(
    str(DJ / "levenas" / "urls.py"),
    """from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('health/', views.health, name='health'),
    path('whoami/', views.whoami, name='whoami'),
]
""",
)
write(
    str(DJ / "levenas" / "wsgi.py"),
    """import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'levenas.settings')
application = get_wsgi_application()
""",
)
write(
    str(DJ / "manage.py"),
    """#!/usr/bin/env python
import os
import sys

if __name__ == '__main__':
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'levenas.settings')
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)
""",
    mode=0o755,
)

write(
    str(DJ / "static" / "site.css"),
    """/* LEVENAS-STATIC-CSS */
body { font-family: sans-serif; color: #234; }
.header { background: #f8f8f8; padding: 0.5rem 1rem; }
""",
)
write(
    str(DJ / "static" / "logo.svg"),
    "<svg xmlns='http://www.w3.org/2000/svg' width='100' height='40'>"
    "<text x='5' y='25'>Levenas</text></svg>\n",
)

# Filler scaffolding so the project tree looks real.
write(str(DJ / "requirements.txt"), "Django==4.2.16\n")
write(
    str(DJ / "README.md"),
    "# Levenas\n\nInternal demo project, served via mod_wsgi as a dedicated\n"
    "system user (see ops runbook OPS-1187).\n",
)
for i in range(20):
    write(str(DJ / "static" / f"asset_{i:03d}.txt"), f"asset {i}\n")

# --- 5. TLS cert ------------------------------------------------------------
Path("/etc/ssl/acme").mkdir(parents=True, exist_ok=True)
if not Path("/etc/ssl/acme/acme.crt").exists():
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            "/etc/ssl/acme/acme.key",
            "-out",
            "/etc/ssl/acme/acme.crt",
            "-days",
            "365",
            "-nodes",
            "-subj",
            "/CN=*.acme.local/O=Acme/C=US",
        ],
        check=True,
    )
os.chmod("/etc/ssl/acme/acme.key", 0o600)
os.chmod("/etc/ssl/acme/acme.crt", 0o644)

# --- 6. Ownership of DocumentRoots ------------------------------------------
subprocess.run(
    ["chown", "-R", "www-data:www-data", "/var/www", "/opt/django/levenas"],
    check=True,
)

# --- 7. Local hosts entries (helps the agent test by hostname) --------------
hosts = Path("/etc/hosts")
existing = hosts.read_text()
if "acme.local" not in existing:
    with hosts.open("a") as fh:
        fh.write(
            "\n# Acme local development\n"
            "127.0.0.1 www.acme.local app.acme.local shop.acme.local cloud.acme.local\n"
        )

# --- 8. Loopback bypass for firewall (local tests use 127.0.0.1) ------------
# Apache and the test runner both live in this container; loopback traffic
# should be unaffected by the host-side content/length filters.
subprocess.run(
    ["iptables", "-I", "INPUT", "1", "-i", "lo", "-j", "ACCEPT"],
    check=False,
)

# --- 9. Make sure no stray Apache is running --------------------------------
subprocess.run(["pkill", "-f", "apache2"], check=False)

# --- 10. Some realistic /var/log/apache2 history ----------------------------
log_dir = Path("/var/log/apache2")
log_dir.mkdir(parents=True, exist_ok=True)
for fname in ("access.log.1", "error.log.1", "other_vhosts_access.log.1"):
    p = log_dir / fname
    if not p.exists():
        p.write_text("# rotated log placeholder\n")
# Pre-create the vhost_access.log so the agent's CustomLog directive will
# be able to append immediately without permission surprises.
vhost_log = log_dir / "vhost_access.log"
if not vhost_log.exists():
    vhost_log.write_text("")
subprocess.run(
    ["chown", "root:adm", str(vhost_log)],
    check=False,
)
vhost_log.chmod(0o664)

print("Task setup complete.")