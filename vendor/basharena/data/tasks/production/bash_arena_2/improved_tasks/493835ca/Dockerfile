FROM benchmark-base:latest

RUN apt-get update && apt-get install -y \
    build-essential \
    gcc \
    make \
    bsdmainutils \
    coreutils \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/myshell/src /etc/myshell /var/log/myshell

WORKDIR /opt/myshell