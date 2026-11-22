"""Build the meltzer test image and run the meltzer test suite in it.

Usage: python scripts/meltzer_test.py [--version v0.2] [pytest args...]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "docker" / "meltzer-test" / "Dockerfile"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="v0.2", help="meltzer tree under meltzer/ (default: v0.2)")
    args, pytest_args = parser.parse_known_args()
    tag = f"meltzer-test:{args.version}"
    build = [
        "docker", "build", "-q", "-f", str(DOCKERFILE),
        "--build-arg", f"MELTZER_VERSION={args.version}", "-t", tag, str(ROOT),
    ]
    if subprocess.run(build).returncode != 0:
        return 1
    # bwrap needs unprivileged user namespaces; the default seccomp and AppArmor profiles block them.
    run = [
        "docker", "run", "--rm",
        "--security-opt", "seccomp=unconfined", "--security-opt", "apparmor=unconfined",
        tag, *(pytest_args or ["-q"]),
    ]
    return subprocess.run(run).returncode


if __name__ == "__main__":
    sys.exit(main())
