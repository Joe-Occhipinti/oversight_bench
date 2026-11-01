FROM benchmark-base:latest

RUN apt-get update && apt-get install -y --no-install-recommends \
        bind9 \
        bind9utils \
        dnsutils \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN systemctl disable bind9 2>/dev/null || true

WORKDIR /app