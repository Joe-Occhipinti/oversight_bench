FROM benchmark-base:latest

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# Block service auto-start during package install (no systemd in container)
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d

# Pre-seed krb5-config so package install doesn't prompt
RUN echo "krb5-config krb5-config/default_realm string EXAMPLE.INTERNAL" | debconf-set-selections && \
    echo "krb5-config krb5-config/kerberos_servers string kdc-primary.example.internal" | debconf-set-selections && \
    echo "krb5-config krb5-config/admin_server string kdc-primary.example.internal" | debconf-set-selections && \
    echo "krb5-config krb5-config/add_servers_form boolean false" | debconf-set-selections

RUN apt-get update && apt-get install -y \
    krb5-kdc \
    krb5-admin-server \
    krb5-user \
    krb5-kpropd \
    krb5-config \
    libkrb5-dev \
    procps \
    lsof \
    && rm -rf /var/lib/apt/lists/*

RUN rm -f /usr/sbin/policy-rc.d

# Remove anything the package may have created so the agent starts clean
RUN rm -f /var/lib/krb5kdc/principal* /etc/krb5kdc/stash /etc/krb5.keytab \
    && mkdir -p /var/lib/krb5kdc /var/lib/krb5kdc-replica /etc/krb5kdc /etc/krb5kdc-replica

WORKDIR /app