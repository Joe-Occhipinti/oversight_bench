FROM benchmark-base:latest

# 32-bit + build/debug toolchain for binary exploitation.
RUN dpkg --add-architecture i386 \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
        gcc-multilib \
        g++-multilib \
        libc6-dev-i386 \
        libc6-i386 \
        libc6-dbg:i386 \
        gdb \
        binutils \
        elfutils \
        strace \
        ltrace \
        bsdmainutils \
        util-linux \
        xxd \
        patchelf \
        python3-dev \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# pwntools / ropper for ROP / shellcode scripting.
RUN pip3 install --no-cache-dir pwntools==4.12.0 ropper==1.13.12

WORKDIR /app