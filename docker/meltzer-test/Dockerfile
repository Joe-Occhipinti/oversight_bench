# meltzer test image: Python 3.12 + bubblewrap, meltzer installed with its tests.
# Build context: repository root. Built and run by scripts/meltzer_test.py.
FROM python:3.12-slim

ARG MELTZER_VERSION=v0.2

RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap curl git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "pytest>=8.0" "httpx>=0.27" "prompt_toolkit>=3.0" "websockets>=12.0"

COPY meltzer/${MELTZER_VERSION} /opt/meltzer-src
RUN pip install --no-cache-dir /opt/meltzer-src

# Tests run as an unprivileged user: bwrap then uses unprivileged user namespaces,
# and paths outside the sandbox's writable roots are writable for the test itself.
RUN useradd --create-home --uid 1000 tester
RUN mkdir /run/meltzer-test && chown tester /run/meltzer-test
USER tester
WORKDIR /home/tester

# Temporary test directories live outside /tmp, which is a writable root in the sandbox.
ENTRYPOINT ["python", "-m", "pytest", "-p", "no:cacheprovider", "--basetemp=/home/tester/pytest-tmp", "/opt/meltzer-src/tests"]
CMD ["-q"]
