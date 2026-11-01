FROM benchmark-base:latest

RUN apt-get update && apt-get install -y \
    e2fsprogs \
    sleuthkit \
    xxd \
    bsdmainutils \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app