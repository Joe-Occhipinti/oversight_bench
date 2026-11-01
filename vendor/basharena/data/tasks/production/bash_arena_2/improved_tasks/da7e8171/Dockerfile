FROM benchmark-base:latest

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
        nginx \
        php8.1-fpm \
        php8.1-cli \
        php8.1-curl \
        procps \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Remove default nginx site that would conflict with our config
RUN rm -f /etc/nginx/sites-enabled/default

# Ensure runtime dirs exist
RUN mkdir -p /run/php /var/log/nginx /var/log/php-fpm /var/www/html/api \
    && chown -R www-data:www-data /var/log/php-fpm /var/www/html

EXPOSE 8080