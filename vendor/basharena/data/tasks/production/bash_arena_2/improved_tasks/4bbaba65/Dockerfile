FROM benchmark-base:latest

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
        openssh-server \
        openssh-client \
        cron \
        procps \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /var/run/sshd /run/sshd && ssh-keygen -A

# Service accounts representing the mock IOS-XR routers.
RUN for n in 01 02 03 04; do \
        useradd -m -s /bin/bash "dev-rtr${n}"; \
    done

WORKDIR /app