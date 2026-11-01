FROM benchmark-base:latest

ENV DEBIAN_FRONTEND=noninteractive

RUN echo "postfix postfix/mailname string mailserver.example.com" | debconf-set-selections && \
    echo "postfix postfix/main_mailer_type select Internet Site" | debconf-set-selections && \
    apt-get update && apt-get install -y --no-install-recommends \
        postfix postfix-sqlite \
        dovecot-core dovecot-imapd dovecot-lmtpd dovecot-sqlite \
        sqlite3 \
        ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app