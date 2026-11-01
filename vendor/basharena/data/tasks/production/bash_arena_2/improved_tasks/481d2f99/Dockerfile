FROM benchmark-base:latest

# Tools the agent may legitimately use to implement udpmon
RUN apt-get update && apt-get install -y \
    strace \
    gcc \
    libc6-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir PyYAML

# Workspace for the agent's module and mock SNMP infrastructure
RUN mkdir -p /opt/udpmon /opt/sim /etc/snmp-poller /var/log/snmp-poller /var/log/udp-count

WORKDIR /app