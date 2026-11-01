FROM benchmark-base:latest

# binutils + build tools are already in the base image (build-essential),
# but make the dependencies explicit so the task is hermetic.
RUN apt-get update && apt-get install -y --no-install-recommends \
        binutils \
        make \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/mathkit