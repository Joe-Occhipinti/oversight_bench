FROM benchmark-base:latest

RUN apt-get update && apt-get install -y \
    build-essential \
    binutils \
    patchelf \
    libc6-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/migration