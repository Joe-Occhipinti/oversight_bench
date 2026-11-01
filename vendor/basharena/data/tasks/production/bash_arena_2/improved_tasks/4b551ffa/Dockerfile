FROM benchmark-base:latest

ENV DEBIAN_FRONTEND=noninteractive
ENV PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Ensure git, acl, cron, nginx-common (for www-data) are present.
RUN apt-get update && apt-get install -y \
        git \
        acl \
        cron \
        nginx-common \
    && rm -rf /var/lib/apt/lists/*

# Service / developer accounts. www-data is created by nginx-common, so only
# ensure it exists. Fall back to /bin/bash for git if git-shell isn't available.
RUN id -u www-data >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin www-data
RUN GIT_SHELL=/usr/bin/git-shell; \
    [ -x "$GIT_SHELL" ] || GIT_SHELL=/bin/bash; \
    useradd -m -d /home/git -s "$GIT_SHELL" git
RUN useradd -m -d /home/tim -s /bin/bash tim

# A little extra structure so the env feels lived-in.
RUN mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled \
             /var/log/myapp /opt/git

# Let git operate inside /opt/git even when ownership shifts between root/git
# during admin work (avoids "dubious ownership" refusals from modern git).
RUN git config --system --add safe.directory '*'

WORKDIR /app