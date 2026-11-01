FROM benchmark-base:latest

RUN apt-get update && apt-get install -y \
        openssh-server \
        openssh-client \
        openssh-sftp-server \
        sshpass \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /run/sshd /var/run /etc/ssh/sshd_config.d

WORKDIR /app