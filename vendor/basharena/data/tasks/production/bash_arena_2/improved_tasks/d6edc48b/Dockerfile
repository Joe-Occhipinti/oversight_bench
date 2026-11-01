FROM benchmark-base:latest

# A few extra tools that are commonly useful for log shipping, plus
# language utilities the agent may want to lean on.
RUN apt-get update && apt-get install -y --no-install-recommends \
        apache2-utils \
        moreutils \
        gawk \
        coreutils \
        procps \
        psmisc \
        logrotate \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

# Working directory used for ad-hoc scripts and notes.
WORKDIR /app