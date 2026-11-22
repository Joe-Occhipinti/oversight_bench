"""Plans the Windows sandbox for a sandbox policy: the ACL changes on the filesystem.

Modes:
  unelevated  commands run with a restricted token of the current user
  elevated    commands run as a separate sandbox user, set up once by an administrator

In both modes the sandbox identity gets an allow-write entry on each writable
root, a deny-write entry on each protected or read-only path inside them, and
a deny-read entry on each denied path. Only paths that exist are listed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from meltzer.sandbox.bwrap import SandboxSetupError
from meltzer.sandbox.policy import SandboxPolicy


class WindowsSandboxMode(StrEnum):
    UNELEVATED = "unelevated"
    ELEVATED = "elevated"


@dataclass(frozen=True)
class AllowDenyPaths:
    allow: tuple[Path, ...]
    deny_write: tuple[Path, ...]
    deny_read: tuple[Path, ...]


def compute_allow_deny_paths(policy: SandboxPolicy, cwd: Path) -> AllowDenyPaths:
    """The ACL changes for a command in cwd under the policy."""
    if not policy.is_sandboxed:
        raise SandboxSetupError(f"sandbox mode {policy.mode} does not use a sandbox")
    if not policy.can_read(cwd):
        raise SandboxSetupError(f"working directory {cwd} is not readable in the sandbox")
    return AllowDenyPaths(
        allow=tuple(p for p in policy.writable_roots() if p.exists()),
        deny_write=tuple(p for p in policy.read_only_subpaths() if p.exists()),
        deny_read=tuple(p for p in policy.deny_read if p.exists()),
    )
