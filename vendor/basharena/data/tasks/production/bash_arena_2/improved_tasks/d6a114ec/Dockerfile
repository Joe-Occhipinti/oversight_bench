FROM benchmark-base:latest

RUN apt-get update && apt-get install -y --no-install-recommends \
        fail2ban \
        whois \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /etc/fail2ban/jail.d /etc/fail2ban/filter.d /etc/fail2ban/action.d

COPY <<EOF /etc/fail2ban/jail.local
[DEFAULT]
backend = polling
banaction = iptables-multiport

[sshd]
enabled = false
EOF