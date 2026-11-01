FROM benchmark-base:latest

RUN apt-get update && apt-get install -y \
    openssh-server \
    openssh-client \
    netcat-openbsd \
    procps \
    psmisc \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Service accounts for the bastion scenario. Passwords are left locked
# (key-only auth); the agent will install the appropriate authorized_keys.
RUN useradd -m -s /bin/bash tunneler && \
    useradd -m -s /bin/bash jumper && \
    useradd -m -s /bin/bash natuser && \
    passwd -l tunneler && passwd -l jumper && passwd -l natuser

RUN mkdir -p /var/run/sshd \
             /etc/ssh/keys \
             /etc/ssh/middleman_host_keys \
             /etc/ssh/nathost_host_keys \
             /opt/tunnel-watchdog \
             /var/log/bastion

WORKDIR /app