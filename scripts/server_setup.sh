#!/usr/bin/env bash
# One-time setup of an Ubuntu 22.04 server to run oversight_bench experiments.
# Run as root from inside the cloned repo:
#   sudo [DOCKER_DATA_ROOT=/mnt/data/docker CONTAINERD_ROOT=/mnt/data/containerd] bash scripts/server_setup.sh
#
# Installs Docker Engine with the compose plugin (Docker's apt repository: the compose option `interface_name`
# needs Engine 28 or newer), sets Docker's network address pools inside 172.16.0.0/12 (spec 7.2: without it at
# most 9 sample stacks run at once, and BashArena's firewall rejects other ranges), installs uv and the
# project's Python environment, and checks disk space and the API key.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-root}"
MIN_FREE_GB=200

echo "== packages"
apt-get update
apt-get install -y ca-certificates curl git tmux rsync

echo "== Docker Engine"
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
if [ "$RUN_USER" != root ]; then usermod -aG docker "$RUN_USER"; fi

echo "== Docker address pools"
if ip -4 -o addr show | grep -E ' inet 172\.(1[6-9]|2[0-9]|3[01])\.' | grep -v docker; then
  echo "WARNING: a host interface above is inside 172.16.0.0/12; sample networks may collide with it" >&2
fi
if [ -f /etc/docker/daemon.json ]; then cp /etc/docker/daemon.json /etc/docker/daemon.json.bak; fi
# DOCKER_DATA_ROOT (optional): where Docker keeps images and containers, e.g. a folder on a large data disk.
data_root=""
if [ -n "${DOCKER_DATA_ROOT:-}" ]; then mkdir -p "$DOCKER_DATA_ROOT"; data_root="\"data-root\": \"$DOCKER_DATA_ROOT\","; fi
cat > /etc/docker/daemon.json <<EOF
{
  $data_root
  "default-address-pools": [{"base": "172.16.0.0/12", "size": 24}]
}
EOF
# CONTAINERD_ROOT (optional): Docker 29 keeps image layers in containerd's store, which ignores data-root.
if [ -n "${CONTAINERD_ROOT:-}" ]; then
  systemctl stop docker docker.socket containerd
  mkdir -p "$CONTAINERD_ROOT"
  if grep -q '^root = ' /etc/containerd/config.toml; then
    sed -i "s|^root = .*|root = \"$CONTAINERD_ROOT\"|" /etc/containerd/config.toml
  else
    sed -i "1i root = \"$CONTAINERD_ROOT\"" /etc/containerd/config.toml
  fi
  systemctl start containerd
fi
systemctl restart docker
docker version --format 'Docker Engine {{.Server.Version}}'
docker compose version

echo "== uv and the Python environment"
sudo -u "$RUN_USER" -H bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
sudo -u "$RUN_USER" -H bash -c "cd '$REPO_DIR' && \$HOME/.local/bin/uv sync"

echo "== checks"
free_gb=$(df -BG --output=avail "$(docker info -f '{{.DockerRootDir}}')" | tail -1 | tr -dc 0-9)
echo "free space for Docker: ${free_gb} GB"
if [ "$free_gb" -lt "$MIN_FREE_GB" ]; then echo "WARNING: less than ${MIN_FREE_GB} GB free for Docker images" >&2; fi
if ! grep -q '^OPENROUTER_API_KEY=.' "$REPO_DIR/.env" 2>/dev/null; then
  echo "WARNING: $REPO_DIR/.env has no OPENROUTER_API_KEY; copy it from the laptop" >&2
fi
echo "done. Log out and in again so that $RUN_USER can use docker without sudo."
