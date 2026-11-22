"""Run git to read information about a work tree: commit, branch and remotes.

Every run turns off the file system monitor and hooks the repository config may
name, ignores the system git config, does not use a bare repository found in
the directory, reads nothing from stdin, and is stopped with every process it
started when it does not finish within the timeout.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

GIT_TIMEOUT_SECONDS = 5.0
GIT_OPTIONS = (
    "-c", "core.fsmonitor=false",
    "-c", "safe.bareRepository=explicit",
    "-c", f"core.hooksPath={os.devnull}",
)


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def run_git(args: list[str], cwd: Path, timeout: float = GIT_TIMEOUT_SECONDS) -> GitResult | None:
    """Run `git <args>` in cwd. None when git is not installed, cannot start or times out."""
    if shutil.which("git") is None:
        return None
    try:
        process = subprocess.Popen(
            ["git", *GIT_OPTIONS, *args],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # No optional locks, no system git config.
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_OPTIONAL_LOCKS": "0"},
            # Git runs in its own process group.
            start_new_session=True,
        )
    except OSError:
        return None
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException as exc:
        _kill(process)
        process.wait()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        if isinstance(exc, subprocess.TimeoutExpired):
            return None
        raise
    return GitResult(process.returncode, stdout, stderr)


def _kill(process: subprocess.Popen) -> None:
    try:
        if sys.platform == "win32":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # The group is gone, or only processes we may not signal are left in it.
        return


def _output(args: list[str], cwd: Path) -> str | None:
    """Stdout of a successful run, or None."""
    result = run_git(args, cwd)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def _without_credentials(url: str) -> str:
    """url without the user name and password of a `scheme://user:password@host` address."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme and "@" in parts.netloc:
        return parts._replace(netloc=parts.netloc.rpartition("@")[2]).geturl()
    return url


def git_info(root: Path) -> dict[str, Any] | None:
    """Commit, branch and fetch remote URLs (without credentials) of the repository at root; None outside one.

    Keys are left out when git cannot report them (no commit yet, detached HEAD, no remotes).
    """
    if (_output(["rev-parse", "--is-inside-work-tree"], root) or "").strip() != "true":
        return None
    info: dict[str, Any] = {}
    commit = (_output(["rev-parse", "HEAD"], root) or "").strip()
    if commit:
        info["commit"] = commit
    branch = (_output(["rev-parse", "--abbrev-ref", "HEAD"], root) or "").strip()
    if branch and branch != "HEAD":
        info["branch"] = branch
    remotes = {}
    for line in (_output(["remote", "-v"], root) or "").splitlines():
        name, _, rest = line.partition("\t")
        url, _, kind = rest.rpartition(" ")
        if kind == "(fetch)" and url:
            remotes[name] = _without_credentials(url)
    if remotes:
        info["remotes"] = dict(sorted(remotes.items()))
    return info
