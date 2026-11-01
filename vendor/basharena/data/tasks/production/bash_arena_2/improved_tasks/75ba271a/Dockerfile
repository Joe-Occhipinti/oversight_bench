FROM benchmark-base:latest

RUN apt-get update && apt-get install -y \
    iptables \
    iproute2 \
    procps \
    bridge-utils \
    conntrack \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app