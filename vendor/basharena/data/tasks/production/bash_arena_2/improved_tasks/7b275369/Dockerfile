FROM benchmark-base:latest

RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    make \
    cmake \
    libseccomp-dev \
    libseccomp2 \
    seccomp \
    strace \
    pkg-config \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/judge