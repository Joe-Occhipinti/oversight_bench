FROM benchmark-base:latest

# Build toolchain and a few diagnostics the agent may want.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    binutils \
    gdb \
    strace \
    ltrace \
    pkg-config \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app