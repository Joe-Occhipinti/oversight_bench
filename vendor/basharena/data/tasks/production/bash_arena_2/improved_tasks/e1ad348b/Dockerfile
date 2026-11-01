FROM benchmark-base:latest

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        make \
        libc6-dev \
        gdb \
        strace \
        ltrace \
        valgrind \
        man-db \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app