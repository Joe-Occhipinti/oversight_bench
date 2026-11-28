# Build from the repository root: docker build -f docker/basharena-meltzer/Dockerfile .
# Only meltzer/v0.2 is sent as build context (Dockerfile.dockerignore next to this file).
FROM linuxarena/env-basharena-base:latest

# Remove what the published base image installs for its own sandbox tooling:
# the pipx venv, its link, its logs and caches, and the browser it downloaded (spec 1a R1).
RUN pipx uninstall inspect-tool-support || true \
 && rm -rf /root/.local/pipx /root/.local/bin/inspect-tool-support \
           /root/.cache/inspect-tool-support /root/.cache/pip /root/.cache/ms-playwright

RUN apt-get update && apt-get install -y bubblewrap && rm -rf /var/lib/apt/lists/*

# MCP SDK for the system python3 (the ops server in /app/tools/ops_mcp runs with it). jsonschema's
# timing scripts are not used at run time and are removed (spec 1a R2: no such words in the image).
RUN python3 -m pip install --no-cache-dir mcp==1.30.0 pydantic==2.14.3  && rm -rf "$(python3 -c 'import jsonschema, os; print(os.path.dirname(jsonschema.__file__))')/benchmarks"

COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python

# The system python3 (3.10) stays the default; meltzer runs from its own 3.12 venv.
COPY meltzer/v0.2/ /opt/meltzer/
RUN uv venv --no-cache /opt/meltzer-venv --python 3.12 \
 && uv pip install --no-cache --python /opt/meltzer-venv/bin/python -e /opt/meltzer \
 && ln -s /opt/meltzer-venv/bin/meltzer /usr/local/bin/meltzer
