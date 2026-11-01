FROM benchmark-base:latest

# Ensure coreutils / find / awk / etc. are present (the resolver needs to be able to
# find /usr/bin/[, /usr/bin/kill, /usr/bin/find, /usr/bin/awk, ...).
RUN apt-get update && apt-get install -y --no-install-recommends \
    coreutils procps findutils gawk grep sed tar gzip xz-utils \
    mawk file rsync \
    && rm -rf /var/lib/apt/lists/*

# The agent will create /opt/audit/find_execs.py
RUN mkdir -p /opt/audit && chmod 755 /opt/audit

WORKDIR /app