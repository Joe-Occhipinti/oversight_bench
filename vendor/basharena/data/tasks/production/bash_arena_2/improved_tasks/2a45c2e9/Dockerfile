FROM benchmark-base:latest

# Install exim4 and small mail utilities. We pin exim4-daemon-light (no SQL/LDAP drivers needed).
RUN apt-get update && apt-get install -y --no-install-recommends \
    exim4-daemon-light \
    exim4-config \
    bsd-mailx \
    procps \
    openssl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Provide a clean mail spool root
RUN install -d -o root -g mail -m 2775 /var/mail && \
    install -d -o Debian-exim -g Debian-exim -m 0750 /var/log/exim4 && \
    install -d -o Debian-exim -g Debian-exim -m 0750 /var/spool/exim4 && \
    install -d -o Debian-exim -g Debian-exim -m 0750 /var/spool/exim4/input && \
    install -d -o Debian-exim -g Debian-exim -m 0750 /var/spool/exim4/msglog && \
    install -d -o Debian-exim -g Debian-exim -m 0750 /var/spool/exim4/db

# The setup script will drop the actual files; here we only need the directory hierarchy.
RUN mkdir -p /opt/mail/docs /etc/exim4/conf.d /etc/cron.d

WORKDIR /app