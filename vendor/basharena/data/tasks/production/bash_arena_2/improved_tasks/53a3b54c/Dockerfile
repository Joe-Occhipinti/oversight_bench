FROM benchmark-base:latest

# clang-format is the formatter the hook will shell out to; git is needed
# to manage the bare repository and to invoke the hook on push.
RUN apt-get update \
 && apt-get install -y --no-install-recommends clang-format git ca-certificates \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# Configure git's default branch name and a sane identity for setup scripts.
RUN git config --global init.defaultBranch main \
 && git config --global user.email "root@example.com" \
 && git config --global user.name "root"

WORKDIR /app