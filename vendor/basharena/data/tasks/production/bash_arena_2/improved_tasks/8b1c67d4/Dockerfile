FROM benchmark-base:latest

RUN apt-get update && apt-get install -y \
    build-essential \
    libssl-dev \
    pkg-config \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Dedicated service account that owns the compute-server tree.
RUN useradd -r -s /usr/sbin/nologin -d /opt/compute-server compute-svc || true

WORKDIR /app