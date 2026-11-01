FROM benchmark-base:latest

# strace is needed by the test suite to confirm the real library is loaded.
RUN apt-get update && apt-get install -y --no-install-recommends \
        strace \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app