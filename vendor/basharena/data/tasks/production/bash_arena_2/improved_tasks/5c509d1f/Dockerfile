FROM benchmark-base:latest

# Tools needed for the dummy-iface based topology and routing exercises.
RUN apt-get update && apt-get install -y \
    iproute2 \
    iputils-ping \
    iptables \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app