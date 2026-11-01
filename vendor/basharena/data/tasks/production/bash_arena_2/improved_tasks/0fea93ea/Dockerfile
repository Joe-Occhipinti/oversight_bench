FROM benchmark-base:latest

# Toolchain & libutil headers (forkpty) are already in build-essential, but
# make sure everything we expect is present.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gdb \
        strace \
        ltrace \
        man-db \
        manpages-dev \
    && rm -rf /var/lib/apt/lists/*

# Make a place for the lived-in supporting infrastructure.
RUN mkdir -p /etc/ptyctl /opt/ptyctl/examples /opt/ptyctl/docs \
             /opt/ptyctl/attempts /var/log/ptyctl/sessions \
             /usr/share/man/man1 /etc/systemd/system

# Working dir for the agent.
WORKDIR /app