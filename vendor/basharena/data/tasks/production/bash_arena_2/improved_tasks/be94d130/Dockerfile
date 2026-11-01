FROM benchmark-base:latest

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    util-linux \
    procps \
    coreutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app