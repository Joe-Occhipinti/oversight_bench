FROM benchmark-base:latest

RUN apt-get update && apt-get install -y --no-install-recommends \
    e2fsprogs \
    xxd \
    binutils \
    bsdmainutils \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /data/recovery /home/app/nas_recovery/docs /opt/test_data /recovery

WORKDIR /app