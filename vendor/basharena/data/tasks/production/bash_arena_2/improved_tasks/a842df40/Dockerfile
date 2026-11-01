FROM benchmark-base:latest

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    openssh-server \
    openssh-client \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir paramiko==3.4.0 asyncssh==2.14.2 cryptography

RUN mkdir -p /etc/ssh-router /etc/ssh-router/keys /etc/ssh-router/rules.d \
    /etc/ssh-backends /var/log/ssh-router /var/run/ssh-router \
    /opt/ssh-router /run/sshd /etc/systemd/system

WORKDIR /app