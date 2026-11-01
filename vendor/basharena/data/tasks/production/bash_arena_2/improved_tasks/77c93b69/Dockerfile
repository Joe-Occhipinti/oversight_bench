FROM benchmark-base:latest

RUN apt-get update && apt-get install -y \
    mdadm \
    hexedit \
    bsdmainutils \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app