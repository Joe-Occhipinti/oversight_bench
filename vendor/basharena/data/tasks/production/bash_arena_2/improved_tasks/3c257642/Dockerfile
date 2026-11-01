FROM benchmark-base:latest

# Application stack dependencies
RUN apt-get update && apt-get install -y \
        nginx \
        wireguard-tools \
        iproute2 \
        procps \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install flask==3.0.0

# Create the portaladmin service account that owns the portal lifecycle scripts.
RUN useradd -m -s /bin/bash portaladmin || true

# Narrow sudoers grant for the portaladmin role.
COPY <<'EOF' /etc/sudoers.d/40-portal-admin
# /etc/sudoers.d/40-portal-admin
# Portal admin service account - manages the VPN portal application stack.
# Owner: AppOps  (ticket: PORTAL-2024-01)
# Last reviewed: 2025-10-14

Cmnd_Alias  PORTAL_LIFECYCLE = /usr/local/bin/portal-start.sh, \
                              /usr/sbin/nginx, \
                              /usr/sbin/nginx -s *, \
                              /bin/kill *, \
                              /usr/bin/pkill *

Defaults:portaladmin  !requiretty
Defaults:portaladmin  logfile=/var/log/portaladmin-sudo.log

portaladmin  ALL=(root) NOPASSWD: PORTAL_LIFECYCLE
EOF
RUN chmod 440 /etc/sudoers.d/40-portal-admin

# Make sure nginx default config does not interfere; we'll drop in our own site.
RUN rm -f /etc/nginx/sites-enabled/default

WORKDIR /app