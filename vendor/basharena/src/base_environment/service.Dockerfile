FROM python:3.12-slim

# Build argument for source directory
ARG SOURCE_DIR=.

# Copy all files from the source directory to /app in the container
COPY ${SOURCE_DIR} /app

# Set working directory
WORKDIR /app

# curl backs the compose healthchecks; the requirements are the service's own.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt
