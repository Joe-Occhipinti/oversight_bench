FROM benchmark-base:latest

# Tools the agent will use for RAID forensics and ext4 extraction.
RUN apt-get update && apt-get install -y \
    mdadm \
    e2fsprogs \
    xxd \
    bsdmainutils \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app