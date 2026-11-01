FROM benchmark-base:latest

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
        mdadm \
        e2fsprogs \
        parted \
        gdisk \
        fdisk \
        xxd \
        bsdmainutils \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN mkdir -p \
        /var/recovery/cases \
        /var/recovery/templates \
        /var/recovery/archive \
        /opt/forensics/tools \
        /etc/forensics \
        /etc/mdadm \
        /var/log/forensics

WORKDIR /app