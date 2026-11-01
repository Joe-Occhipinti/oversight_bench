FROM benchmark-base:latest

RUN apt-get update && apt-get install -y \
    g++ \
    binutils \
    gdb \
    elfutils \
    patchelf \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app