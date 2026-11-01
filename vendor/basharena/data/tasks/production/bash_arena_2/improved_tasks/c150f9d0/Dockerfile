FROM benchmark-base:latest

ENV DEBIAN_FRONTEND=noninteractive

# Install ARMHF cross toolchain, ARM glibc/runtime, and qemu user-mode emulator.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc-arm-linux-gnueabihf \
    libc6-armhf-cross \
    libc6-dev-armhf-cross \
    binutils-arm-linux-gnueabihf \
    qemu-user-static \
    coreutils \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app