"""Build the meltzer test image and run the meltzer test suite in it.

Usage: python scripts/meltzer_test.py [--version v0.2] [pytest args...]

Test paths are given relative to the meltzer tree, e.g. tests/test_rules.py;
without one, the whole suite runs.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "docker" / "meltzer-test" / "Dockerfile"
TREE_IN_IMAGE = "/opt/meltzer-src"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="v0.2", help="meltzer tree under meltzer/ (default: v0.2)")
    parser.add_argument("--tag", default=None, help="image tag (default: meltzer-test:<version>)")
    args, pytest_args = parser.parse_known_args()
    tag = args.tag or f"meltzer-test:{args.version}"
    build = [
        "docker", "build", "-q", "-f", str(DOCKERFILE),
        "--build-arg", f"MELTZER_VERSION={args.version}", "-t", tag, str(ROOT),
    ]
    if subprocess.run(build).returncode != 0:
        return 1
    paths = [a for a in pytest_args if a.startswith("tests/") or a.startswith("tests\\")]
    options = [a for a in pytest_args if a not in paths]
    targets = [f"{TREE_IN_IMAGE}/{p.replace(chr(92), '/')}" for p in paths] or [f"{TREE_IN_IMAGE}/tests"]
    # bwrap needs unprivileged user namespaces; the default seccomp and AppArmor profiles block them.
    run = [
        "docker", "run", "--rm",
        "--security-opt", "seccomp=unconfined", "--security-opt", "apparmor=unconfined",
        tag, *(options or ["-q"]), *targets,
    ]
    return subprocess.run(run).returncode


if __name__ == "__main__":
    sys.exit(main())
