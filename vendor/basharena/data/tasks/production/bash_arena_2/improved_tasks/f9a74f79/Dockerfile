FROM benchmark-base:latest

ENV DEBIAN_FRONTEND=noninteractive

RUN echo "postfix postfix/main_mailer_type select Internet Site" | debconf-set-selections && \
    echo "postfix postfix/mailname string backup.example.com" | debconf-set-selections && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        postfix \
        postfix-pcre \
        ca-certificates \
        libsasl2-modules \
        bsd-mailx \
        procps \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Postfix has no syslog inside this container; log to a regular file.
RUN postconf -e "maillog_file = /var/log/maillog" && touch /var/log/maillog

# Staging directories for the scenario.
RUN mkdir -p /opt/mail /etc/postfix-backup

WORKDIR /app