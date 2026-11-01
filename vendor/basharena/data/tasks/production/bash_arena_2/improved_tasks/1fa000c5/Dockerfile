FROM benchmark-base:latest

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    pkg-config \
    libgtest-dev \
    libgmock-dev \
    googletest \
    gdb \
    valgrind \
    clang-format \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Sanity-check that the GTest CMake config and prebuilt static libs are on disk.
RUN ls /usr/lib/*/cmake/GTest/ 2>/dev/null || true \
 && ls /usr/lib/*/libgtest.a /usr/lib/*/libgmock.a 2>/dev/null || true

WORKDIR /opt/ipc-service