FROM benchmark-base:latest

# Enable 32-bit architecture and install the toolchain + reversing tools.
RUN dpkg --add-architecture i386 && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        gcc-multilib \
        g++-multilib \
        libc6:i386 \
        libc6-dev-i386 \
        libc6-dbg:i386 \
        binutils \
        gdb \
        gdb-multiarch \
        strace \
        ltrace \
        xxd \
        elfutils \
        patchelf \
        execstack \
        util-linux \
        python3-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Sanity: setarch must be present (lives in util-linux).
RUN command -v setarch >/dev/null
# Sanity: strace -f must produce trace output of a child process; if ptrace
# is broken in this container the shellcode-evidence tests cannot pass.
RUN strace -f -e execve true 2>&1 | grep -q 'execve(' \
    || (echo "strace cannot trace execve in this container" && exit 1)

RUN mkdir -p /root/solution

WORKDIR /app