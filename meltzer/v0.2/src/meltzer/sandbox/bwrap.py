"""Builds the bubblewrap (bwrap) command line for a sandboxed command.

Layout of the sandbox:
  --new-session                      own terminal session
  --die-with-parent                  the sandbox ends when meltzer ends
  --ro-bind / /                      the whole filesystem, read-only
  --tmpfs /run (and /var/run)        empty runtime directories: no host sockets
  --dev /dev, --proc /proc           fresh device nodes and proc (own pid namespace)
  --unshare-user                     own user namespace
  --unshare-pid                      own process id namespace
  --unshare-ipc                      own System V IPC objects and POSIX message queues
  --bind <temp dir> /tmp             the session's temporary directory instead of the
                                     host /tmp (an empty tmpfs when the policy has none;
                                     remounted read-only in read-only mode)
  --bind <root> <root>               the session root, read-write (read-only in
                                     read-only mode)
  --bind <dir> <dir>                 extra writable directories that exist
  --ro-bind <p> <p>                  protected paths inside writable roots, read-only again
  --tmpfs <d> --remount-ro <d>       denied directories, replaced by an empty read-only tmpfs
  --ro-bind /dev/null <f>            denied files, replaced by an empty file
  --ro-bind <proxy dir> /run/meltzer-net
                                     network mode proxy: the directory with the
                                     command's network proxy socket
  --unshare-net                      no network (proxy mode: only loopback, where
                                     the bridge to the proxy listens)
  --json-status-fd <fd>              where bwrap reports the child pid and exit code
  --chdir <cwd>                      the command's working directory

bwrap reports on --json-status-fd: first the child pid, then {"exit-code": N}
once the command has run. A missing exit code means bwrap failed before it
could start the command (mount, chdir or exec failure).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from meltzer.sandbox.policy import SANDBOX_TEMP_DIR, NetworkMode, SandboxMode, SandboxPolicy


# Host directories replaced by an empty tmpfs. /var/run is usually a symlink
# to /run and is then covered by the first entry.
RUNTIME_DIRS = (Path("/run"), Path("/var/run"))
# Where a proxy-mode command finds the socket directory of its network proxy.
PROXY_SOCKET_DIR = Path("/run/meltzer-net")


class SandboxSetupError(Exception):
    """The sandbox could not be set up; the command was not run."""


def find_bwrap() -> str:
    path = shutil.which("bwrap")
    if path is None:
        raise SandboxSetupError("bubblewrap (bwrap) is not installed or not on PATH")
    return path


def build_bwrap_args(
    policy: SandboxPolicy, command: list[str], cwd: Path, status_fd: int, *, proxy_dir: Path | None = None
) -> list[str]:
    """`proxy_dir`: host directory with the network proxy socket, for a policy with network mode proxy."""
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
        "--unshare-user",
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
        if root == policy.temp_dir:
            continue
        if root != policy.root:
            if root.is_symlink():
                raise SandboxSetupError(f"writable path {root} is a symlink")
            if not root.exists():
                continue
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
            # Protected paths that are symbolic links are refused.
            raise SandboxSetupError(f"protected path {path} is a symlink")
        if path.exists():
            args += ["--ro-bind", str(path), str(path)]
    for path in sorted(policy.deny_read, key=lambda p: len(p.parts)):
        if path.is_symlink():
            raise SandboxSetupError(f"denied path {path} is a symlink")
        if path.is_dir():
            args += ["--tmpfs", str(path), "--remount-ro", str(path)]
        elif path.exists():
            args += ["--ro-bind", "/dev/null", str(path)]
    if policy.network == NetworkMode.PROXY and proxy_dir is not None:
        args += ["--ro-bind", str(proxy_dir), PROXY_SOCKET_DIR.as_posix()]
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
