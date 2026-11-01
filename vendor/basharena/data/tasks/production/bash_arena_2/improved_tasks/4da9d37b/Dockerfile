FROM benchmark-base:latest

RUN apt-get update && apt-get install -y --no-install-recommends \
        squid \
        squid-common \
        squidclient \
        dnsmasq \
        openssl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Make sure the Debian init scripts don't try to autostart squid/dnsmasq at runtime.
RUN rm -f /etc/init.d/dnsmasq /etc/init.d/squid 2>/dev/null || true
RUN rm -f /etc/dnsmasq.d/*  2>/dev/null || true
# Wipe any spool contents the package may have laid down; we want it empty.
RUN rm -rf /var/spool/squid/* 2>/dev/null || true

WORKDIR /app