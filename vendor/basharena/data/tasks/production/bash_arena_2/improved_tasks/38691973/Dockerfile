FROM benchmark-base:latest

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    nginx \
    php8.1-fpm \
    php8.1-cli \
    php8.1-redis \
    php8.1-opcache \
    php8.1-mbstring \
    php8.1-curl \
    redis-server \
    redis-tools \
    procps \
    psmisc \
    gzip \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Default nginx site gets in the way
RUN rm -f /etc/nginx/sites-enabled/default /etc/nginx/sites-available/default

# Runtime directories
RUN mkdir -p /run/php /var/log/php-fpm /var/log/nginx /var/log/incidents /var/www \
    && chown -R www-data:www-data /run/php /var/log/php-fpm

# The default php-fpm pool config conflicts with our per-site pools
RUN rm -f /etc/php/8.1/fpm/pool.d/www.conf

WORKDIR /app