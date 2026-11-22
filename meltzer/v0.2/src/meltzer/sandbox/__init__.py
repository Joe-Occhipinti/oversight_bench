"""Command sandboxing: bubblewrap on Linux.

The backend follows the platform (sys.platform), never the configuration. On
a platform without a backend, sandboxed commands are refused.
"""

from __future__ import annotations

import os
import sys
from enum import StrEnum

from meltzer.sandbox.bwrap import SandboxSetupError
from meltzer.sandbox.seatbelt import SEATBELT_EXECUTABLE


class SandboxBackend(StrEnum):
    BWRAP = "bwrap"
    SEATBELT = "seatbelt"


def select_backend(platform: str | None = None) -> SandboxBackend:
    """The sandbox backend for `platform` (default sys.platform); SandboxSetupError if there is none."""
    platform = platform or sys.platform
    if platform.startswith("linux"):
        return SandboxBackend.BWRAP
    if platform == "darwin" and os.path.exists(SEATBELT_EXECUTABLE):
        return SandboxBackend.SEATBELT
    raise SandboxSetupError(f"no sandbox is available on platform {platform}; sandboxed commands cannot run")
