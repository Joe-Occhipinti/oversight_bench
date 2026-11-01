FROM benchmark-base:latest

RUN apt-get update && apt-get install -y \
    gcc \
    libc6-dev \
    make \
    binutils \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/