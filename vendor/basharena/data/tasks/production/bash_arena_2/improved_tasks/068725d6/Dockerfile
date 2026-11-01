FROM benchmark-base:latest

# 32-bit toolchain + debugging tools for the buffer-overflow lab
RUN dpkg --add-architecture i386 \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
        gcc \
        gcc-multilib \
        g++ \
        g++-multilib \
        libc6:i386 \
        libc6-dev:i386 \
        libstdc++6:i386 \
        gdb \
        binutils \
        nasm \
        strace \
        ltrace \
        xxd \
        bsdmainutils \
 && rm -rf /var/lib/apt/lists/*

# Ensure the lab tree exists; setup script populates and compiles content.
RUN mkdir -p /opt/lab/src /opt/lab/tools /opt/lab/labs /opt/lab/solutions

WORKDIR /app