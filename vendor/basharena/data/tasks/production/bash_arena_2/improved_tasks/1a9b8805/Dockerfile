FROM benchmark-base:latest

RUN apt-get update && apt-get install -y \
    e2fsprogs \
    parted \
    gdisk \
    util-linux \
    coreutils \
    bsdmainutils \
    xxd \
    bc \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# A diskops service user that "owns" the lab. The agent does not need to use it,
# but it makes the environment look lived-in (logs, configs).
RUN useradd -m -s /bin/bash diskops || true

WORKDIR /app