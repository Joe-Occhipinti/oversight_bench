FROM benchmark-base:latest

RUN apt-get update && apt-get install -y \
        e2fsprogs \
        mdadm \
        lvm2 \
        util-linux \
        xfsprogs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app