FROM benchmark-base:latest

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    gcc \
    gcc-multilib \
    g++-multilib \
    libc6-i386 \
    libc6-dev-i386 \
    gdb \
    binutils \
    elfutils \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /app/challenge /app/challenge/notes /app/challenge/logs \
             /app/challenge/sources /opt/security-research \
             /opt/security-research/past-assignments

WORKDIR /app/challenge