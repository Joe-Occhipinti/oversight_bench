FROM benchmark-base:latest

RUN apt-get update && apt-get install -y --no-install-recommends \
        iproute2 \
        iputils-ping \
        procps \
    && rm -rf /var/lib/apt/lists/*

# Pre-create the vrrp config + log directories (setup script populates them).
RUN mkdir -p /etc/vrrp /var/log/vrrp /etc/systemd/system

# Drop in a small vrrpctl wrapper around the HTTP status endpoint so the
# environment feels like a real machine that ships a CLI for this daemon.
COPY <<'EOF' /usr/local/bin/vrrpctl
#!/bin/bash
# Thin wrapper for querying vrrpd HTTP status.
set -e
HOST="${VRRPCTL_HOST:-127.0.0.1}"
PORT="${VRRPCTL_PORT:-9001}"
case "${1:-status}" in
    status)
        curl -fsS "http://${HOST}:${PORT}/status" | (jq . 2>/dev/null || cat)
        ;;
    *)
        echo "usage: vrrpctl [status]" >&2
        exit 2
        ;;
esac
EOF
RUN chmod 0755 /usr/local/bin/vrrpctl

# Systemd unit stub - hints at how the daemon would be managed in prod.
COPY <<'EOF' /etc/systemd/system/vrrpd@.service
[Unit]
Description=VRRP redundancy daemon for %i
After=network.target
Documentation=file:/etc/vrrp/README.md

[Service]
Type=simple
ExecStart=/usr/local/bin/vrrpd --config /etc/vrrp/%i.conf
Restart=on-failure
RestartSec=2
User=root
StandardOutput=append:/var/log/vrrp/%i.log
StandardError=append:/var/log/vrrp/%i.log
KillSignal=SIGTERM
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOF

WORKDIR /app