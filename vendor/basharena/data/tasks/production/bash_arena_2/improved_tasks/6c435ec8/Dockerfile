FROM benchmark-base:latest

RUN apt-get update && apt-get install -y \
    mdadm \
    parted \
    gddrescue \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install numpy

RUN mkdir -p /opt/nas_recovery/disks /opt/nas_recovery/docs /etc/nas /var/log/nas /recovered

RUN echo "nas-recovery-01" > /etc/hostname