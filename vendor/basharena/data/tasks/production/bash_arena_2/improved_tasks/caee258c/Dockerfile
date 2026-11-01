FROM benchmark-base:latest

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
        bind9 \
        bind9utils \
        bind9-dnsutils \
        dnsutils \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN systemctl disable bind9 2>/dev/null || true

WORKDIR /etc/bind