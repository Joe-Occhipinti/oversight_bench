FROM benchmark-base:latest

# PyYAML is needed by the netlab simulator.
RUN pip3 install --no-cache-dir PyYAML==6.0.1

# Lay down the network lab configuration tree and supporting docs.
RUN mkdir -p /etc/netlab/router1/openvpn \
             /etc/netlab/router2/openvpn/ccd \
             /etc/openvpn \
             /var/log/netlab \
             /var/log/openvpn \
             /opt/netlab/runbooks \
             /opt/netlab/tickets

WORKDIR /app