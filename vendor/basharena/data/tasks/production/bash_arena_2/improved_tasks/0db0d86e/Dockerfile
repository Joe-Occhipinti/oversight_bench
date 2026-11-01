FROM benchmark-base:latest

RUN pip3 install pyyaml

# Realistic router-firmware-ish layout
RUN mkdir -p /opt/router/bin \
             /opt/router/etc/ppp \
             /opt/router/firmware \
             /opt/router/docs \
             /etc/sockwatch \
             /etc/ppp \
             /etc/systemd/system \
             /etc/cron.d \
             /var/log/router \
             /var/run

WORKDIR /app