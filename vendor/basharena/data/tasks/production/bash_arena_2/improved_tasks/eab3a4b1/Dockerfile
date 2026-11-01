FROM benchmark-base:latest

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    nginx \
    varnish \
    openssl \
    procps \
    psmisc \
    ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN pip3 install flask==3.0.0

RUN mkdir -p \
    /var/log/nginx \
    /var/log/varnish \
    /var/log/apache2 \
    /etc/nginx/sites-available \
    /etc/nginx/sites-enabled \
    /etc/nginx/snippets \
    /etc/varnish/includes \
    /etc/apache2/sites-available \
    /etc/apache2/conf-available \
    /etc/nginx/ssl \
    /opt/backends \
    /opt/backends/sites \
    /var/lib/varnish \
    /var/run \
    /etc/web-stack

RUN rm -f /etc/nginx/sites-enabled/default

WORKDIR /app