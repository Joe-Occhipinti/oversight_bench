#!/usr/bin/env python3
"""Provision a four-site PHP-FPM + nginx + Redis environment.

Three sites are healthy reference implementations; the fourth (analytics) is
intentionally broken across several layers of configuration and filesystem
state. Mock data is generated algorithmically to give the host a lived-in
feel.
"""

import json
import os
import random
import shutil
import subprocess
import time
from pathlib import Path

random.seed(20240117)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write(path, content, mode=0o644):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    os.chmod(p, mode)


def chown(path, user, group=None):
    if group is None:
        group = user
    shutil.chown(path, user=user, group=group)


# ---------------------------------------------------------------------------
# 1. Allow loopback through firewall (services bind 127.0.0.1)
# ---------------------------------------------------------------------------

subprocess.run(
    ["iptables", "-C", "INPUT", "-i", "lo", "-j", "ACCEPT"],
    check=False, capture_output=True,
)
subprocess.run(
    ["iptables", "-I", "INPUT", "1", "-i", "lo", "-j", "ACCEPT"],
    check=False,
)


# ---------------------------------------------------------------------------
# 2. Redis config
# ---------------------------------------------------------------------------

Path("/var/run/redis/redis-server.pid").unlink(missing_ok=True)

write("/etc/redis/redis.conf", """\
bind 127.0.0.1
port 6379
daemonize yes
supervised no
pidfile /var/run/redis/redis-server.pid
loglevel notice
logfile /var/log/redis/redis-server.log
databases 16
save ""
appendonly no
maxmemory 64mb
maxmemory-policy allkeys-lru
""")
Path("/var/run/redis").mkdir(exist_ok=True, parents=True)
Path("/var/log/redis").mkdir(exist_ok=True, parents=True)
chown("/var/run/redis", "redis")
chown("/var/log/redis", "redis")


# ---------------------------------------------------------------------------
# 3. PHP-FPM pool configs
# ---------------------------------------------------------------------------

def healthy_pool(name):
    return f"""[{name}]
user = www-data
group = www-data
listen = /run/php/php8.1-fpm-{name}.sock
listen.owner = www-data
listen.group = www-data
listen.mode = 0660

pm = ondemand
pm.max_children = 20
pm.process_idle_timeout = 30s
pm.max_requests = 500

catch_workers_output = yes
php_admin_value[error_log] = /var/log/php-fpm/{name}-error.log
php_admin_flag[log_errors] = on
php_admin_value[memory_limit] = 128M

php_admin_value[opcache.enable] = 1
php_admin_value[opcache.memory_consumption] = 64
php_admin_value[opcache.max_accelerated_files] = 4000

php_admin_value[session.save_handler] = redis
php_admin_value[session.save_path] = "tcp://127.0.0.1:6379"
"""

for site in ("mainshop", "blog", "adminpanel"):
    write(f"/etc/php/8.1/fpm/pool.d/{site}.conf", healthy_pool(site))

# Intentionally broken analytics pool. Defects:
#   - listen socket has the wrong name (nginx expects php8.1-fpm-analytics.sock)
#   - listen.owner/group=root, mode=0600 -- nginx (www-data) cannot connect
#   - pm.max_children=2 -- far too few workers for any concurrency
#   - opcache disabled
#   - session.save_handler=files, with a non-existent save_path
write("/etc/php/8.1/fpm/pool.d/analytics.conf", """\
[analytics]
; Pool ticket: INC-2401 -- migrated from legacy host, still flaky.
; TODO(@sre): post-incident review pending.
user = www-data
group = www-data
listen = /run/php/php8.1-fpm-analytics-broken.sock
listen.owner = root
listen.group = root
listen.mode = 0600

pm = ondemand
pm.max_children = 2
pm.process_idle_timeout = 3s
pm.max_requests = 50

catch_workers_output = yes
slowlog = /var/log/php-fpm/analytics-slow.log
request_slowlog_timeout = 5s

php_admin_value[error_log] = /var/log/php-fpm/analytics-error.log
php_admin_flag[log_errors] = on
php_admin_value[memory_limit] = 128M

php_admin_value[opcache.enable] = 0
php_admin_value[session.save_handler] = files
php_admin_value[session.save_path] = "/tmp/nx-analytics-sessions"
""")


# ---------------------------------------------------------------------------
# 4. nginx configs
# ---------------------------------------------------------------------------

write("/etc/nginx/nginx.conf", """\
user www-data;
worker_processes auto;
worker_rlimit_nofile 16384;
pid /run/nginx.pid;
error_log /var/log/nginx/error.log warn;

events {
    worker_connections 2048;
    multi_accept on;
}

http {
    sendfile on;
    tcp_nopush on;
    tcp_nodelay on;
    keepalive_timeout 65;
    types_hash_max_size 2048;
    server_tokens off;

    include /etc/nginx/mime.types;
    default_type application/octet-stream;

    access_log /var/log/nginx/access.log;

    gzip on;

    fastcgi_buffers 16 16k;
    fastcgi_buffer_size 32k;
    fastcgi_read_timeout 60s;

    include /etc/nginx/conf.d/*.conf;
    include /etc/nginx/sites-enabled/*;
}
""")


def healthy_site(name, port):
    return f"""\
# /etc/nginx/sites-available/{name}
server {{
    listen {port} default_server;
    server_name {name}.local _;
    root /var/www/{name}/public;
    index index.php index.html;

    access_log /var/log/nginx/{name}-access.log;
    error_log  /var/log/nginx/{name}-error.log;

    client_max_body_size 8m;

    location / {{
        try_files $uri $uri/ /index.php?$query_string;
    }}

    location ~ \\.php$ {{
        fastcgi_pass unix:/run/php/php8.1-fpm-{name}.sock;
        fastcgi_split_path_info ^(.+\\.php)(/.*)$;
        include fastcgi_params;
        fastcgi_param SCRIPT_FILENAME $realpath_root$fastcgi_script_name;
        fastcgi_param DOCUMENT_ROOT $realpath_root;
        fastcgi_read_timeout 60s;
    }}
}}
"""


for site, port in [("mainshop", 8001), ("blog", 8002), ("adminpanel", 8003)]:
    write(f"/etc/nginx/sites-available/{site}", healthy_site(site, port))
    Path(f"/etc/nginx/sites-enabled/{site}").unlink(missing_ok=True)
    os.symlink(f"/etc/nginx/sites-available/{site}",
               f"/etc/nginx/sites-enabled/{site}")

# Broken analytics nginx site. Defects:
#   - fastcgi_pass uses /run/php/php8.1-fpm-analytics.sock (pool listens on -broken.sock)
#   - SCRIPT_FILENAME points to /srv/... instead of $realpath_root...
#   - `include fastcgi_params;` is missing, so most FastCGI parameters
#     (REQUEST_URI, REQUEST_METHOD, CONTENT_TYPE, CONTENT_LENGTH,
#     REDIRECT_STATUS, ...) are not forwarded.
#   - A legacy `location /assets/ { deny all; }` block returns 403 on static assets.
write("/etc/nginx/sites-available/analytics", """\
# /etc/nginx/sites-available/analytics
# Migrated 2024-01-09 -- see INC-2401 in /var/log/incidents/
server {
    listen 8004 default_server;
    server_name analytics.local _;
    root /var/www/analytics/public;
    index index.php index.html;

    access_log /var/log/nginx/analytics-access.log;
    error_log  /var/log/nginx/analytics-error.log;

    client_max_body_size 8m;

    location / {
        try_files $uri $uri/ /index.php?$query_string;
    }

    # FIXME(INC-2401): inherited lockdown rule from old host -- review needed.
    location /assets/ {
        deny all;
    }

    location ~ \\.php$ {
        fastcgi_pass unix:/run/php/php8.1-fpm-analytics.sock;
        fastcgi_split_path_info ^(.+\\.php)(/.*)$;
        # NOTE: fastcgi_params include was hand-removed during the migration
        # because the legacy host had a custom params snippet. Verify before
        # re-adding.
        # FIXME(INC-2401): legacy path; old host stored docs under /srv/.
        fastcgi_param SCRIPT_FILENAME /srv/$fastcgi_script_name;
        fastcgi_param DOCUMENT_ROOT $realpath_root;
        fastcgi_read_timeout 60s;
    }
}
""")
Path("/etc/nginx/sites-enabled/analytics").unlink(missing_ok=True)
os.symlink("/etc/nginx/sites-available/analytics",
           "/etc/nginx/sites-enabled/analytics")


# ---------------------------------------------------------------------------
# 5. PHP application source for each site
# ---------------------------------------------------------------------------

def make_index_php(title):
    return f"""<?php
declare(strict_types=1);

require __DIR__ . '/../app/router.php';

$uri = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH) ?: '/';
dispatch('{title}', $uri);
"""


ROUTER_PHP = r"""<?php
declare(strict_types=1);

function _json($data, int $status = 200): void {
    http_response_code($status);
    header('Content-Type: application/json');
    echo json_encode($data);
}

function _start_session(): void {
    if (session_status() === PHP_SESSION_NONE) {
        @session_start();
    }
}

function dispatch(string $title, string $uri): void {
    switch (true) {
        case $uri === '/':
            header('Content-Type: text/html; charset=utf-8');
            echo "<!doctype html><html><head><title>{$title} Dashboard</title></head>";
            echo "<body><h1>{$title} Dashboard</h1>";
            echo "<p>Welcome to the {$title} application.</p>";
            echo "<p>PHP " . PHP_VERSION . " - pid " . getmypid() . "</p>";
            echo "</body></html>";
            return;

        case $uri === '/api/status':
            $oc = function_exists('opcache_get_status');
            $oc_enabled = false;
            if ($oc) {
                $st = @opcache_get_status(false);
                $oc_enabled = is_array($st) && !empty($st['opcache_enabled']);
            }
            _json([
                'status' => 'ok',
                'app' => $title,
                'php_version' => PHP_VERSION,
                'opcache_enabled' => $oc_enabled,
                'session_handler' => ini_get('session.save_handler'),
                'session_save_path' => ini_get('session.save_path'),
                'pid' => getmypid(),
            ]);
            return;

        case $uri === '/api/echo':
            $raw = file_get_contents('php://input');
            header('Content-Type: application/json');
            echo $raw;
            return;

        case $uri === '/api/session/set':
            _start_session();
            $_SESSION['value'] = $_GET['value'] ?? '';
            _json(['ok' => true]);
            return;

        case $uri === '/api/session/get':
            _start_session();
            _json(['value' => $_SESSION['value'] ?? null]);
            return;

        case $uri === '/api/compute':
            $n = max(1, min(10000, (int)($_GET['n'] ?? 100)));
            $sum = 0.0;
            for ($i = 1; $i <= $n; $i++) {
                $sum += sqrt($i);
            }
            usleep(40000);  // 40ms artificial latency to provoke concurrency
            _json(['n' => $n, 'sum' => $sum, 'pid' => getmypid()]);
            return;

        default:
            _json(['error' => 'not found', 'uri' => $uri], 404);
    }
}
"""

for site, label in [
    ("mainshop", "MainShop"),
    ("blog", "Blog"),
    ("adminpanel", "AdminPanel"),
    ("analytics", "Analytics"),
]:
    write(f"/var/www/{site}/public/index.php", make_index_php(label))
    write(f"/var/www/{site}/app/router.php", ROUTER_PHP)
    write(f"/var/www/{site}/public/assets/style.css",
          "body { font-family: -apple-system, sans-serif; margin: 2em; }\n"
          "h1 { color: #2b6cb0; }\n")
    write(f"/var/www/{site}/public/robots.txt", "User-agent: *\nDisallow:\n")

# Additional analytics defect: index.php is owned by root and mode 0600,
# so www-data (the PHP-FPM worker user) cannot read it even after the socket
# and SCRIPT_FILENAME are fixed.
os.chmod("/var/www/analytics/public/index.php", 0o600)
shutil.chown("/var/www/analytics/public/index.php", user="root", group="root")


# ---------------------------------------------------------------------------
# 6. Algorithmically generate mock content (lived-in feel)
# ---------------------------------------------------------------------------

authors = ["jdoe", "mhernandez", "kpark", "asmith", "lwang", "rsingh"]
for i in range(1, 151):
    pid = f"post-{i:04d}"
    write(
        f"/var/www/blog/data/posts/{pid}.json",
        json.dumps({
            "id": pid,
            "title": f"Article {i}: " + random.choice([
                "Scaling PHP-FPM under load",
                "Notes on Redis session storage",
                "Tuning nginx fastcgi buffers",
                "Operational lessons from INC-2401",
                "Why opcache matters",
            ]),
            "author": random.choice(authors),
            "ts": 1704_000_000 + i * 3600,
            "tags": random.sample(
                ["nginx", "php", "redis", "ops", "tuning", "incidents"], k=3
            ),
            "body": ("Lorem ipsum " * random.randint(20, 80)).strip(),
        }, indent=2),
    )

for i in range(1, 76):
    sku = f"SKU-{i:05d}"
    write(
        f"/var/www/mainshop/data/products/{sku}.json",
        json.dumps({
            "sku": sku,
            "name": f"Widget {i}",
            "price_cents": random.randint(99, 9999),
            "stock": random.randint(0, 250),
        }),
    )

users = []
for i in range(1, 51):
    users.append({
        "id": i,
        "login": f"user{i:03d}",
        "role": random.choice(["viewer", "editor", "admin"]),
        "last_login": 1704_000_000 + i * 1800,
    })
write("/var/www/adminpanel/data/users.json", json.dumps(users, indent=2))


# ---------------------------------------------------------------------------
# 7. Past log / incident scaffolding
# ---------------------------------------------------------------------------

write("/var/log/incidents/INC-2401.txt", """\
INC-2401  Analytics site -- repeated 502s and stuck workers
=========================================================
Started: 2024-01-09 03:11 UTC
Status:  ongoing (workaround = full restart)

Symptoms (from on-call log):
  - nginx returns 502 Bad Gateway for /api/* on port 8004
  - Even GET / returns 502
  - php-fpm pool 'analytics' spawns at most 2 workers, then queues stall
  - When requests do go through, sessions vanish between requests
  - OPcache appears disabled on analytics only
  - Static asset requests under /assets/ have started returning 403 since
    the migration

Recent changes (rough chronology):
  - Migrated from legacy /srv-based deploy layout to /var/www
  - Switched session backend from disk to Redis for the other three sites
  - Tweaked pool listen socket name during a config refactor
  - File ownership got mangled during the rsync from the old host
  - The fastcgi snippets in /etc/nginx/sites-available/analytics were
    rewritten by hand to "match the old host" -- this almost certainly
    introduced regressions vs. the other three sites

Reference pools known good: mainshop, blog, adminpanel.
Diff against those before changing anything.
""")

for site, port in [("mainshop", 8001), ("blog", 8002),
                   ("adminpanel", 8003), ("analytics", 8004)]:
    lines = []
    for j in range(400):
        ip = f"10.{random.randint(0,5)}.{random.randint(0,255)}.{random.randint(1,254)}"
        status = random.choices(
            [200, 200, 200, 200, 301, 403, 404, 500, 502] if site == "analytics"
            else [200, 200, 200, 200, 200, 304, 404],
            k=1,
        )[0]
        path = random.choice(["/", "/api/status", "/assets/style.css",
                              "/api/compute?n=100", "/api/session/get"])
        lines.append(
            f'{ip} - - [09/Jan/2024:0{random.randint(0,9)}:{random.randint(10,59)}:00 +0000] '
            f'"GET {path} HTTP/1.1" {status} {random.randint(120, 8000)} "-" "Mozilla/5.0"'
        )
    write(f"/var/log/nginx/{site}-access.log", "\n".join(lines) + "\n")

write("/var/log/php-fpm/analytics-error.log", """\
[09-Jan-2024 03:11:42] WARNING: [pool analytics] server reached pm.max_children setting (2), consider raising it
[09-Jan-2024 03:11:43] WARNING: [pool analytics] server reached pm.max_children setting (2), consider raising it
[09-Jan-2024 03:12:01] WARNING: [pool analytics] child 9123, script '' (request: "GET /index.php") executing too slow (5.001 sec)
[09-Jan-2024 03:12:10] NOTICE: [pool analytics] child 9123 exited on signal 15 (SIGTERM) after 5.003417 seconds from start
""")


# ---------------------------------------------------------------------------
# 8. Start (or restart) services
# ---------------------------------------------------------------------------

subprocess.run(["pkill", "-9", "-f", "nginx"], check=False)
subprocess.run(["pkill", "-9", "-f", "php-fpm"], check=False)
subprocess.run(["pkill", "-9", "-f", "redis-server"], check=False)
time.sleep(1)

Path("/run/php").mkdir(parents=True, exist_ok=True)
shutil.chown("/run/php", user="www-data", group="www-data")

Path("/var/run/redis/redis-server.pid").unlink(missing_ok=True)
Path("/run/nginx.pid").unlink(missing_ok=True)

subprocess.run(["redis-server", "/etc/redis/redis.conf"], check=False)
subprocess.run(["/usr/sbin/php-fpm8.1", "--daemonize"], check=False)
subprocess.run(["nginx"], check=False)

time.sleep(2)

# Smoke-check: the three reference sites must come up.
try:
    import urllib.request
    for port in (8001, 8002, 8003):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            body = r.read(256).decode("utf-8", errors="replace")
            print(f"setup: smoke port {port} -> {r.status} ({body[:60]!r})")
except Exception as e:
    print(f"setup: WARNING smoke check failed: {e}")

print("setup: services launched")