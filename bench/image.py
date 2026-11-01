"""Container images and compose patch for the meltzer setting (spec section 4).

Two images:
- `oversight-bench/basharena-meltzer:<tag>`: BashArena's base image with meltzer installed
  (`docker/basharena-meltzer/Dockerfile`). Tag = first 12 hex characters of the sha256 over the meltzer
  tree hash, the bytes of that Dockerfile and the bytes of its `Dockerfile.dockerignore`
  (`meltzer_image_tag`). Per-task images are built `FROM` it: `bench.basharena.write_task_workspace`
  rewrites the `FROM` line of the task Dockerfile (`base_image=`).
- `oversight-bench/gateway:<tag>`: plain Ubuntu with socat (`docker/gateway/Dockerfile`). Inspect's
  model proxy runs in it, so nothing of Inspect is in the agent container (spec 1a R1). Tag = first
  12 characters of the sha256 of its Dockerfile.

`ensure_images` builds either image when it is missing. A change to the meltzer tree, the Dockerfile
or the `.dockerignore` gives a new tag, so the image is rebuilt under it.

`patch_compose` applies spec 4.2 to a BashArena compose dict.

Reaching Inspect's model proxy from the agent container takes three more steps once BashArena's
firewall runs (found in S2, see the docstrings): `add_gateway_to_hosts`, `allow_gateway_in_firewall`
and `start_gateway_forwarder`, called inside a sample with
`sandbox_agent_bridge(sandbox="gateway", port=GATEWAY_PROXY_PORT, ...)` open or about to open.
meltzer's `base_url` is then `http://gateway:4000/v1`.
"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import os
import re
import subprocess
from pathlib import Path

from inspect_ai.util import sandbox

from bench.config import IMAGE_REPO, REPO_ROOT, meltzer_tree_hash

DOCKER_DIR = REPO_ROOT / "docker"
MELTZER_DOCKERFILE = DOCKER_DIR / "basharena-meltzer" / "Dockerfile"
GATEWAY_DIR = DOCKER_DIR / "gateway"
GATEWAY_REPO = "oversight-bench/gateway"

GATEWAY_SERVICE = "gateway"
GATEWAY_NETWORK = "gateway_net"
GATEWAY_PORT = 4000  # port the agent container connects to (socat in the gateway)
GATEWAY_PROXY_PORT = 4001  # port of Inspect's model proxy inside the gateway (it binds 127.0.0.1 only)
# BashArena's firewall daemon accepts all traffic from this range before its own rules.
FIREWALL_ACCEPTED_RANGE = ipaddress.ip_network("172.16.0.0/12")


class ImageBuildError(RuntimeError):
    """`docker build` failed. The message ends with the tail of its output."""


# ---------------------------------------------------------------- image names and builds


def meltzer_image_tag(tree_hash: str | None = None) -> str:
    """`oversight-bench/basharena-meltzer:<tag>` (spec 4.1).

    Tag = first 12 hex characters of the sha256 over `tree_hash` (default: `meltzer_tree_hash()`), the
    bytes of `MELTZER_DOCKERFILE` and the bytes of the `Dockerfile.dockerignore` next to it; each part
    is followed by a NUL byte. Editing the Dockerfile or the `.dockerignore` therefore gives a new tag.
    """
    if tree_hash is None:
        tree_hash = meltzer_tree_hash()
    h = hashlib.sha256()
    for part in (
        tree_hash.encode("ascii"),
        MELTZER_DOCKERFILE.read_bytes(),
        MELTZER_DOCKERFILE.with_name("Dockerfile.dockerignore").read_bytes(),
    ):
        h.update(part + b"\0")
    return f"{IMAGE_REPO}:{h.hexdigest()[:12]}"


def meltzer_image_name() -> str:
    """Name of the meltzer base image: `meltzer_image_tag()`."""
    return meltzer_image_tag()


def gateway_image_name() -> str:
    """`oversight-bench/gateway:<12 chars of the sha256 of docker/gateway/Dockerfile>`."""
    digest = hashlib.sha256((GATEWAY_DIR / "Dockerfile").read_bytes()).hexdigest()
    return f"{GATEWAY_REPO}:{digest[:12]}"


def _image_exists(name: str) -> bool:
    result = subprocess.run(["docker", "image", "inspect", name], capture_output=True)
    return result.returncode == 0


def _build(name: str, context: Path, dockerfile: Path) -> None:
    command = ["docker", "build", "-t", name, "-f", str(dockerfile), str(context)]
    # BuildKit reads `Dockerfile.dockerignore`; the legacy builder would send the whole repository.
    env = {**os.environ, "DOCKER_BUILDKIT": "1"}
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    if result.returncode != 0:
        tail = (result.stdout + result.stderr).strip()[-3000:]
        raise ImageBuildError(f"docker build of {name} failed (exit code {result.returncode}):\n{tail}")


def ensure_images() -> tuple[str, str]:
    """Build the meltzer base image and the gateway image if missing; return `(meltzer, gateway)` names."""
    meltzer, gateway = meltzer_image_name(), gateway_image_name()
    if not _image_exists(meltzer):
        # Context = repository root, so that `COPY meltzer/v0.1/` works; Dockerfile.dockerignore
        # (next to the Dockerfile) leaves only the meltzer tree in it.
        _build(meltzer, REPO_ROOT, MELTZER_DOCKERFILE)
    if not _image_exists(gateway):
        _build(gateway, GATEWAY_DIR, GATEWAY_DIR / "Dockerfile")
    return meltzer, gateway


# ---------------------------------------------------------------- per-task compose file


def patch_compose(compose: dict, gateway_image: str) -> dict:
    """Return a copy of a BashArena compose dict with the changes of spec 4.2.

    - `default` service: `security_opt: [seccomp=unconfined, apparmor=unconfined]`; joins `gateway_net`
      (`cap_add` stays as BashArena has it: NET_ADMIN, NET_RAW). Its networks get fixed interface
      names (`inner_net` eth0, `gateway_net` eth1, compose option `interface_name`): Docker names the
      interfaces in alphabetical order of the network names (checked with Docker Engine 29), which
      would make `gateway_net` eth0, and BashArena's start command sets the default route with
      `dev eth0` (the container would exit with "Nexthop has invalid gateway").
    - new service `gateway` (image `gateway_image`, 256 MB, only on `gateway_net`, `sleep infinity`).
    - new bridge network `gateway_net`.

    The per-task image gets the meltzer image through `bench.basharena.write_task_workspace(base_image=)`,
    not through the compose file.
    """
    patched = copy.deepcopy(compose)
    default = patched["services"]["default"]
    default["security_opt"] = ["seccomp=unconfined", "apparmor=unconfined"]
    networks = [*default["networks"], GATEWAY_NETWORK]
    default["networks"] = {name: {"interface_name": f"eth{i}"} for i, name in enumerate(networks)}
    patched["services"][GATEWAY_SERVICE] = {
        "image": gateway_image,
        "mem_limit": "256m",
        "networks": [GATEWAY_NETWORK],
        "command": ["sleep", "infinity"],
    }
    patched["networks"][GATEWAY_NETWORK] = {"driver": "bridge"}
    return patched


# ---------------------------------------------------------------- reaching the gateway from the agent container
# All three run inside a sample (they use `inspect_ai.util.sandbox`).


async def _exec(which: str | None, command: str) -> str:
    result = await sandbox(which).exec(["bash", "-c", command], timeout=60)
    if not result.success:
        raise RuntimeError(f"{which or 'default'}: {command!r} failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


async def _gateway_ip() -> ipaddress.IPv4Address:
    """The gateway's address on `gateway_net`, asked in the gateway itself (no DNS in the agent container)."""
    out = await _exec(GATEWAY_SERVICE, "hostname -I")
    return ipaddress.IPv4Address(out.split()[0])


async def _listening_ports(which: str) -> set[int]:
    out = await _exec(which, "awk '$4==\"0A\"{print $2}' /proc/net/tcp")  # listening IPv4 sockets, hex ADDR:PORT
    return {int(entry.split(":")[1], 16) for entry in out.split()}


async def add_gateway_to_hosts() -> str:
    """Add `<gateway ip> gateway` to the agent container's `/etc/hosts`; return the address.

    Needed once BashArena's firewall runs: it drops the replies of Docker's DNS resolver
    (127.0.0.11), so `gateway` no longer resolves. Call it after the base setup.
    """
    ip = str(await _gateway_ip())
    await _exec(None, f"grep -qw gateway /etc/hosts || echo '{ip} gateway' >> /etc/hosts")
    return ip


async def allow_gateway_in_firewall() -> str | None:
    """Let the gateway's replies through BashArena's firewall; return the subnet opened, or None.

    The firewall daemon accepts everything from 172.16.0.0/12 and drops the rest of the incoming
    traffic (packets of 256 bytes or more first, then sources outside `WHITELIST_RANGES`). Docker takes
    the `gateway_net` subnet from 172.16.0.0/12 first and from 192.168.0.0/16 once those are used up.
    For a subnet outside 172.16.0.0/12 this inserts `iptables -I INPUT -s <subnet> -j ACCEPT`, the same
    kind of rule the daemon keeps for 172.16.0.0/12. Adding the subnet to `WHITELIST_RANGES` (spec 4.2)
    is not enough: the size rule comes before the whitelist rules and drops the proxy's replies.
    """
    ip = await _gateway_ip()
    if ip in FIREWALL_ACCEPTED_RANGE:
        return None
    addresses = await _exec(None, "ip -o -4 addr")
    for interface in re.findall(r"inet (\S+)", addresses):
        network = ipaddress.ip_interface(interface).network
        if ip in network:
            break
    else:
        raise RuntimeError(f"no interface of the agent container is on the gateway's subnet:\n{addresses}")
    rule = f"-s {network} -j ACCEPT"
    await _exec(None, f"iptables -C INPUT {rule} 2>/dev/null || iptables -I INPUT 1 {rule}")
    return str(network)


async def start_gateway_forwarder(
    proxy_port: int = GATEWAY_PROXY_PORT, listen_port: int = GATEWAY_PORT
) -> None:
    """Start `socat` in the gateway: `listen_port` on all interfaces forwards to `127.0.0.1:proxy_port`.

    Inspect's model proxy listens on 127.0.0.1 only, so the agent container cannot reach it directly.
    Open the bridge with `port=proxy_port`; the agent container then uses `listen_port`.
    """
    command = (
        f"setsid socat TCP-LISTEN:{listen_port},fork,reuseaddr TCP:127.0.0.1:{proxy_port} "
        ">/dev/null 2>&1 </dev/null &"
    )
    await _exec(GATEWAY_SERVICE, command)
    for _ in range(20):
        if listen_port in await _listening_ports(GATEWAY_SERVICE):
            return
        await sandbox(GATEWAY_SERVICE).exec(["sleep", "0.25"])
    raise RuntimeError(f"socat is not listening on port {listen_port} in the gateway")
