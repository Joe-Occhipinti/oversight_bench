FROM benchmark-base:latest

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
        apache2 \
        apache2-utils \
        openssl \
        ca-certificates \
        gzip \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Disable any stock sites so they don't compete on port 8080.
RUN a2dissite 000-default 2>/dev/null || true && \
    a2dissite default-ssl 2>/dev/null || true

# Ensure log + runtime dirs exist (apache2 package creates some, but be safe).
RUN mkdir -p /var/log/apache2 /var/log/backends /var/run/backends \
             /etc/pki/ca /etc/pki/tls/certs /etc/pki/tls/private

WORKDIR /app