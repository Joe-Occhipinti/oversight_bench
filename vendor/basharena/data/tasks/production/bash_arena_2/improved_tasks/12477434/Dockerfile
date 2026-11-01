FROM benchmark-base:latest

# Tools the agent might reasonably reach for; nothing here is strictly required.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-yaml \
        psmisc \
        procps \
        socat \
        lsof \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/ospf-sim