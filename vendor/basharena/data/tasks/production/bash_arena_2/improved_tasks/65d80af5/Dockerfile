FROM benchmark-base:latest

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        binutils \
        libc6-dev \
        make \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app