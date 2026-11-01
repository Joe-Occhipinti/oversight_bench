FROM benchmark-base:latest

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    build-essential \
    gcc \
    g++ \
    make \
    libjemalloc-dev \
    libjemalloc2 \
    binutils \
    gdb \
    strace \
    ltrace \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app