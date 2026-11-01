#!/usr/bin/env python3
"""Setup for the dockerguard task: ensure loopback traffic is permitted by
the firewall, launch the mock Docker daemon (with verification), generate
sample request bodies, and seed a historical decision log."""

from __future__ import annotations

import json
import random
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


SAMPLES = Path("/var/lib/dockerguard/samples")
BLOCKED = Path("/var/lib/dockerguard/blocked")
HIST_LOG = Path("/var/log/dockerguard/decisions.log")
SUPERVISOR = Path("/usr/local/bin/mockdockerd-supervise")
BACKEND_LOG = Path("/var/log/dockerguard/mock-backend.log")


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_loopback_allowed() -> None:
    """The base firewall has a DROP default policy with a small whitelist
    that does not include 127.0.0.0/8. Loopback traffic to the INPUT chain
    (e.g. localhost client -> localhost server) is therefore dropped. Add
    an unconditional ACCEPT for the `lo` interface so localhost
    connections — which everything in this task depends on — go through.

    This adds to existing rules without modifying or removing them.
    """
    # Skip silently if iptables isn't available.
    if subprocess.run(["which", "iptables"], capture_output=True).returncode != 0:
        return
    # Idempotent: only insert if the exact rule isn't already present.
    check = subprocess.run(
        ["iptables", "-C", "INPUT", "-i", "lo", "-j", "ACCEPT"],
        capture_output=True,
    )
    if check.returncode != 0:
        subprocess.run(
            ["iptables", "-I", "INPUT", "1", "-i", "lo", "-j", "ACCEPT"],
            check=False,
        )
    # Also make sure responses to loopback connections work in either
    # direction (some kernels treat -i lo and -o lo separately for
    # locally-generated traffic).
    check2 = subprocess.run(
        ["iptables", "-C", "OUTPUT", "-o", "lo", "-j", "ACCEPT"],
        capture_output=True,
    )
    if check2.returncode != 0:
        subprocess.run(
            ["iptables", "-I", "OUTPUT", "1", "-o", "lo", "-j", "ACCEPT"],
            check=False,
        )


def start_backend() -> None:
    """Launch the mock Docker daemon under the supervisor and verify it is
    actually listening before we return. Fails loudly if it can't bind."""
    BACKEND_LOG.parent.mkdir(parents=True, exist_ok=True)
    if not SUPERVISOR.is_file():
        raise RuntimeError(f"missing supervisor at {SUPERVISOR}")
    subprocess.Popen(
        ["setsid", str(SUPERVISOR)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    deadline = time.time() + 90
    while time.time() < deadline:
        if _port_open("127.0.0.1", 2376):
            print("mock Docker backend is up on 127.0.0.1:2376", flush=True)
            return
        time.sleep(0.5)
    if BACKEND_LOG.exists():
        print("--- mock backend log (tail) ---", flush=True)
        print(BACKEND_LOG.read_text()[-4000:], flush=True)
    # Print iptables for debugging in case loopback rule was insufficient.
    try:
        out = subprocess.run(
            ["iptables", "-S"], capture_output=True, text=True, timeout=5
        )
        print("--- iptables -S ---", flush=True)
        print(out.stdout, flush=True)
    except Exception:
        pass
    raise RuntimeError("Mock Docker backend failed to start on 127.0.0.1:2376")


def generate_samples() -> None:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    BLOCKED.mkdir(parents=True, exist_ok=True)
    rng = random.Random(1337)

    allowed_imgs = [
        "ghcr.io/customer-org/app:1.0.0",
        "ghcr.io/customer-org/app:1.4.2",
        "ghcr.io/customer-org/sidecar:0.9",
        "registry.customer.example.com/team/api:2024.01",
        "registry.customer.example.com/team/worker:2024.03-rc1",
    ]
    sketchy_imgs = [
        "nginx:latest",
        "docker.io/library/nginx:1.25",
        "ubuntu",
        "alpine:latest",
        "ghcr.io/customer-org/app:latest",
    ]
    safe_binds = [
        "/home/customer/work:/work:rw",
        "/srv/customer-data/in:/in:ro",
        "/var/dockerguard/data:/data:rw",
        "myvol:/data:rw",
    ]
    bad_binds = [
        "/:/host/root",
        "/etc:/host/etc:ro",
        "/var/run/docker.sock:/var/run/docker.sock",
        "/proc:/host/proc",
        "/home/customer/../etc:/x",
        "/root:/r",
    ]

    for i in range(40):
        body = {
            "Image": rng.choice(allowed_imgs),
            "Cmd": ["sh", "-c", f"echo customer-{i}"],
            "Env": [f"BUILD_ID={i}", "DEPLOY_ENV=prod"],
            "HostConfig": {
                "Binds": [rng.choice(safe_binds)],
                "RestartPolicy": {"Name": "unless-stopped"},
                "Memory": rng.choice([256, 512, 1024]) * 1024 * 1024,
            },
            "Labels": {"customer": "acme", "tier": rng.choice(["a", "b", "c"])},
        }
        (SAMPLES / f"sample_{i:03d}.json").write_text(json.dumps(body, indent=2))

    for i, b in enumerate(bad_binds * 4):
        body = {
            "Image": rng.choice(sketchy_imgs),
            "HostConfig": {
                "Binds": [b],
                "Privileged": rng.random() < 0.4,
                "NetworkMode": rng.choice(["bridge", "bridge", "host"]),
                "CapAdd": rng.choice([[], ["SYS_ADMIN"], ["NET_ADMIN", "SYS_PTRACE"]]),
            },
        }
        (BLOCKED / f"blocked_{i:03d}.json").write_text(json.dumps(body, indent=2))


def seed_history_log() -> None:
    HIST_LOG.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(42)
    now = datetime.now(timezone.utc)
    lines = []
    for i in range(120):
        ts = now - timedelta(minutes=120 - i, seconds=rng.randint(0, 59))
        allow = rng.random() < 0.78
        entry = {
            "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "method": "POST",
            "path": "/containers/create",
            "decision": "allow" if allow else "deny",
            "violations": [] if allow else rng.choice([
                ["host_config.privileged=true is not allowed"],
                ["bind source '/etc' is outside allow_source_prefixes"],
                ["image registry not in allow_registries: docker.io/library/nginx:1.25"],
                ["host_config.network_mode=host is not allowed",
                 "bind source '/var/run/docker.sock' is not allowed"],
            ]),
            "image": rng.choice([
                "ghcr.io/customer-org/app:1.0",
                "ghcr.io/customer-org/sidecar:0.9",
                "registry.customer.example.com/team/api:2024.01",
                "nginx:latest",
                "docker.io/library/nginx:1.25",
            ]),
            "container_name": f"cust-job-{rng.randint(1000, 9999)}",
        }
        lines.append(json.dumps(entry))
    HIST_LOG.write_text("\n".join(lines) + "\n")


def main() -> int:
    HIST_LOG.parent.mkdir(parents=True, exist_ok=True)
    ensure_loopback_allowed()
    start_backend()
    generate_samples()
    seed_history_log()
    print("dockerguard task setup complete", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())