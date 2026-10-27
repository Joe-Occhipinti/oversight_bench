"""Builds the bubblewrap (bwrap) command line for a sandboxed command.

Layout of the sandbox:
  --ro-bind / /                      the whole filesystem, read-only
  --tmpfs /run (and /var/run)        empty runtime directories: no host sockets
  --dev /dev, --proc /proc           fresh device nodes and proc (own pid namespace)
  --unshare-ipc                      own System V IPC objects and POSIX message queues
  --bind <temp dir> /tmp             the session's temporary directory instead of the
                                     host /tmp (an empty tmpfs when the policy has none;
                                     remounted read-only in read-only mode)
  --bind <root> <root>               the session root, read-write (read-only in
                                     read-only mode)
  --ro-bind <p> <p>                  protected paths inside writable roots, read-only again
  --unshare-net                      no network

bwrap reports on --json-status-fd: first the child pid, then {"exit-code": N}
once the command has run. A missing exit code means bwrap failed before it
could start the command (mount, chdir or exec failure).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from meltzer.sandbox.policy import SANDBOX_TEMP_DIR, SandboxMode, SandboxPolicy


# Host directories replaced by an empty tmpfs. /var/run is usually a symlink
# to /run and is then covered by the first entry.
RUNTIME_DIRS = (Path("/run"), Path("/var/run"))


class SandboxSetupError(Exception):
    """The sandbox could not be set up; the command was not run."""


def find_bwrap() -> str:
    path = shutil.which("bwrap")
    if path is None:
        raise SandboxSetupError("bubblewrap (bwrap) is not installed or not on PATH")
    return path


def build_bwrap_args(policy: SandboxPolicy, command: list[str], cwd: Path, status_fd: int) -> list[str]:
    if not policy.is_sandboxed:
        raise SandboxSetupError(f"sandbox mode {policy.mode} does not use a sandbox")
    args = [
        find_bwrap(),
        "--new-session",
        "--die-with-parent",
        "--ro-bind", "/", "/",
    ]
    for directory in RUNTIME_DIRS:
        if directory.is_dir() and not directory.is_symlink():
            args += ["--tmpfs", str(directory)]
    args += [
        "--dev", "/dev",
        "--unshare-pid",
        "--unshare-ipc",
        "--proc", "/proc",
    ]
    temp = SANDBOX_TEMP_DIR.as_posix()
    if policy.temp_dir is not None:
        temp_mount = ["--bind", str(policy.temp_dir), temp]
    else:
        temp_mount = ["--tmpfs", temp]
    mounts: list[tuple[Path, list[str]]] = [(SANDBOX_TEMP_DIR, temp_mount)]
    if policy.mode == SandboxMode.READ_ONLY:
        mounts.append((policy.root, ["--ro-bind", str(policy.root), str(policy.root)]))
    for root in policy.writable_roots():
        if root != policy.temp_dir:
            mounts.append((root, ["--bind", str(root), str(root)]))
    # Outer directories first, so a session root under /tmp on the host stays
    # visible on top of the temporary directory. The sort is stable: /tmp goes
    # first among mounts of the same depth.
    for _, mount in sorted(mounts, key=lambda entry: len(entry[0].parts)):
        args += mount
    if policy.mode == SandboxMode.READ_ONLY:
        args += ["--remount-ro", temp]
    for path in policy.read_only_subpaths():
        if path.is_symlink():
            # A symlink here could be swapped to point somewhere else after the
            # mount is set up; refuse instead of guessing.
            raise SandboxSetupError(f"protected path {path} is a symlink")
        if path.exists():
            args += ["--ro-bind", str(path), str(path)]
    if not policy.allows_network:
        args.append("--unshare-net")
    args += ["--json-status-fd", str(status_fd), "--chdir", str(cwd), "--", *command]
    return args


def exit_code_from_status(data: bytes) -> int | None:
    """Exit code reported by bwrap on its status fd, or None if the command never started."""
    decoder = json.JSONDecoder()
    text = data.decode("utf-8", errors="replace")
    position = 0
    exit_code: int | None = None
    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text):
            break
        try:
            obj, position = decoder.raw_decode(text, position)
        except json.JSONDecodeError:
            break
        if isinstance(obj, dict) and isinstance(obj.get("exit-code"), int):
            exit_code = obj["exit-code"]
    return exit_code
