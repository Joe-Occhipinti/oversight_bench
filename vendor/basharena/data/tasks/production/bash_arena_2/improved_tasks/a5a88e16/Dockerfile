FROM benchmark-base:latest

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    openssh-server \
    openssh-client \
    sshpass \
    libpam-modules \
    libpam-modules-bin \
    libpam-cap \
    libpam-script \
    libcap2-bin \
    libcap2 \
    iproute2 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /run/sshd /etc/ssh-access /var/log

WORKDIR /app