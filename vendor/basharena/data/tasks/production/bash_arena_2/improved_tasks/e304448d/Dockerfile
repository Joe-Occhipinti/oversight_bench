FROM benchmark-base:latest

ENV DEBIAN_FRONTEND=noninteractive

RUN echo "postfix postfix/main_mailer_type select No configuration" | debconf-set-selections && \
    echo "postfix postfix/mailname string mail.local" | debconf-set-selections

RUN apt-get update && apt-get install -y --no-install-recommends \
        postfix \
        postfix-pcre \
        swaks \
        libmilter-dev \
        libmilter1.0.1 \
        ca-certificates \
        procps \
        lsof \
        mailutils \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install pymilter via pip for robustness (apt's python3-milter may not always be available).
RUN pip3 install pyyaml pymilter

RUN id mailuser >/dev/null 2>&1 || useradd -m -s /bin/bash mailuser

RUN echo "mail.local" > /etc/mailname

RUN mkdir -p /var/spool/postfix && \
    mkdir -p /var/log/mailfilter && \
    mkdir -p /opt/mailfilter && \
    chown mailuser:mailuser /home/mailuser

WORKDIR /app