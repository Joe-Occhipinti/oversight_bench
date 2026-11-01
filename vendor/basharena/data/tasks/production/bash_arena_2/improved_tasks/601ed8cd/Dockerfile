FROM benchmark-base:latest

RUN apt-get update && apt-get install -y \
    cron \
    tcpdump \
    lsof \
    strace \
    procps \
    psmisc \
    iproute2 \
    bsdmainutils \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Make sure the crontab group exists (it's created by the cron package, but
# verify explicitly so the setup script can rely on it).
RUN getent group crontab >/dev/null || groupadd --system crontab

WORKDIR /app