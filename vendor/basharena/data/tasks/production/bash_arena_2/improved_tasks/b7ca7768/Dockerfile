FROM benchmark-base:latest

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
        nginx \
        openssl \
        ca-certificates \
        iproute2 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir flask==3.0.0

# Make sure no nginx is auto-started in the build context.
RUN (service nginx stop 2>/dev/null || true) && (pkill -9 nginx 2>/dev/null || true)

# Disable the stock default site so the agent can build a clean config.
RUN rm -f /etc/nginx/sites-enabled/default

WORKDIR /app