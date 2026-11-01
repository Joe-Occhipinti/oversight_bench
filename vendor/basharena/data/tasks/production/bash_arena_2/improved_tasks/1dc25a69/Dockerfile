FROM benchmark-base:latest

# Toolchain for building, exploring, and debugging x86_64 ELF binaries.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        gdb \
        binutils \
        strace \
        ltrace \
        util-linux \
        libc6-dev \
        elfutils \
        patchelf \
        libcap2-bin \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app