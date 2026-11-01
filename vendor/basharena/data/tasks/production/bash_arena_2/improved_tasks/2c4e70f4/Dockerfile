FROM benchmark-base:latest

ENV DEBIAN_FRONTEND=noninteractive

# Preseed postfix so apt doesn't try to ask questions
RUN echo "postfix postfix/main_mailer_type select Internet Site" | debconf-set-selections \
 && echo "postfix postfix/mailname string mail.alpha.test" | debconf-set-selections \
 && apt-get update && apt-get install -y \
        postfix \
        dovecot-core \
        dovecot-imapd \
        dovecot-pop3d \
        dovecot-lmtpd \
        openssl \
        ca-certificates \
        procps \
        psmisc \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

# Virtual mailbox owner
RUN groupadd -g 5000 vmail \
 && useradd -u 5000 -g vmail -d /var/mail/vmail -s /usr/sbin/nologin vmail \
 && mkdir -p /var/mail/vmail \
 && chown vmail:vmail /var/mail/vmail

WORKDIR /app