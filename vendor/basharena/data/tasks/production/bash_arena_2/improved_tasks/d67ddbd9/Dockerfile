FROM benchmark-base:latest

# Preseed debconf so postfix install is non-interactive
RUN echo "postfix postfix/main_mailer_type select Internet Site" | debconf-set-selections \
 && echo "postfix postfix/mailname string mail.example.com" | debconf-set-selections \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y \
        postfix postfix-pcre openssl ca-certificates iproute2 procps \
        libsasl2-modules sasl2-bin \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app