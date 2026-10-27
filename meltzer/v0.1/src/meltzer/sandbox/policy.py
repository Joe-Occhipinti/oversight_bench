"""Sandbox modes, writable roots and protected paths.

Modes:
  read-only        read everywhere, write nowhere, no network
  workspace-write  read everywhere, write in the session root and /tmp; no network
  full-access      no sandbox

Inside the sandbox, /tmp is the session's own temporary directory (temp_dir),
shared by the sandboxed commands of the session; the host /tmp is not visible.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

PROTECTED_NAMES = (".git", ".meltzer")
# Where the session's temporary directory is mounted inside the sandbox.
SANDBOX_TEMP_DIR = Path("/tmp")


class SandboxMode(StrEnum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    FULL_ACCESS = "full-access"


def resolve(path: Path) -> Path:
    """Absolute path with symlinks resolved, also for paths that do not exist yet."""
    return Path(os.path.realpath(path))


def is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def ensure_project_dir(root: Path) -> Path:
    """Create <root>/.meltzer if it is missing."""
    project_dir = root / ".meltzer"
    project_dir.mkdir(exist_ok=True)
    return project_dir


@dataclass(frozen=True)
class SandboxPolicy:
    mode: SandboxMode
    root: Path
    # The user's meltzer directory (~/.meltzer), if known.
    meltzer_home: Path | None = None
    # Host directory mounted at /tmp in the sandbox. Without one, sandboxed
    # commands get an empty /tmp of their own.
    temp_dir: Path | None = None

    @classmethod
    def for_session(
        cls,
        mode: SandboxMode,
        root: Path,
        meltzer_home: Path | None = None,
        temp_dir: Path | None = None,
    ) -> SandboxPolicy:
        return cls(
            mode=mode,
            root=resolve(root),
            meltzer_home=resolve(meltzer_home) if meltzer_home is not None else None,
            temp_dir=resolve(temp_dir) if temp_dir is not None else None,
        )

    @property
    def is_sandboxed(self) -> bool:
        return self.mode != SandboxMode.FULL_ACCESS

    @property
    def allows_network(self) -> bool:
        return self.mode == SandboxMode.FULL_ACCESS

    def writable_roots(self) -> tuple[Path, ...]:
        """Host paths that sandboxed commands can write to."""
        if self.mode == SandboxMode.FULL_ACCESS:
            return (Path("/"),)
        if self.mode == SandboxMode.READ_ONLY:
            return ()
        roots = {self.root} | ({self.temp_dir} if self.temp_dir is not None else set())
        return tuple(sorted(roots, key=lambda p: len(p.parts)))

    def protected_paths(self) -> tuple[Path, ...]:
        """Paths that stay read-only inside writable roots."""
        directories = (self.root, *self.root.parents)
        paths = [directory / name for directory in directories for name in PROTECTED_NAMES]
        if self.meltzer_home is not None and self.meltzer_home not in paths:
            paths.append(self.meltzer_home)
        return tuple(paths)

    def read_only_subpaths(self) -> tuple[Path, ...]:
        """Protected paths that fall inside a writable root, deepest last."""
        if self.mode == SandboxMode.FULL_ACCESS:
            return ()
        roots = self.writable_roots()
        found = [p for p in self.protected_paths() if any(is_within(p, r) for r in roots)]
        return tuple(sorted(found, key=lambda p: len(p.parts)))

    def can_write(self, path: Path) -> bool:
        """Whether the sandbox lets a command write to path."""
        if self.mode == SandboxMode.FULL_ACCESS:
            return True
        target = resolve(path)
        if not any(is_within(target, root) for root in self.writable_roots()):
            return False
        for protected in self.read_only_subpaths():
            if is_within(target, protected) or is_within(target, resolve(protected)):
                return False
        return True
