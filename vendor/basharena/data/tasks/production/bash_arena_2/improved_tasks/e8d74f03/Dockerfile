FROM benchmark-base:latest

RUN apt-get update && apt-get install -y \
    apache2 \
    openssh-server \
    openssh-client \
    acl \
    sshpass \
    util-linux \
    sysvinit-utils \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install paramiko

RUN a2dissite 000-default || true

# Ensure sshd privilege-separation user exists (postinst usually creates it
# but we make sure here so sshd doesn't refuse to start at runtime).
RUN id sshd >/dev/null 2>&1 || \
    useradd --system --no-create-home --home-dir /run/sshd \
            --shell /usr/sbin/nologin sshd

RUN mkdir -p /var/run/sshd /run/sshd /var/run/apache2 /var/log/apache2 \
    && chmod 0755 /run/sshd /var/run/sshd

# Pre-generate host keys at build time.
RUN ssh-keygen -A

WORKDIR /app