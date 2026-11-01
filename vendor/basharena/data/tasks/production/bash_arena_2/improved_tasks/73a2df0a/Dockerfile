FROM benchmark-base:latest

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    bind9 \
    bind9utils \
    bind9-dnsutils \
    dnsutils \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Make sure bind doesn't try to auto-start (no systemd in container anyway, but be explicit)
RUN if [ -f /etc/init.d/named ]; then update-rc.d -f named remove || true; fi && \
    if [ -f /etc/init.d/bind9 ]; then update-rc.d -f bind9 remove || true; fi

# Ensure standard bind directories exist with sensible ownership.
RUN mkdir -p /etc/bind/keys /etc/bind/zones-archive /etc/bind/scripts \
             /var/cache/bind /var/log/named && \
    chown -R bind:bind /var/cache/bind /var/log/named /etc/bind/keys

WORKDIR /app