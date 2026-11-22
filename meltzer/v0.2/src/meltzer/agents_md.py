"""AGENTS.md: project instructions for coding agents."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from meltzer.history import Item, Kind, Role

AGENTS_MD_FILE = "AGENTS.md"
# Larger files are cut at this size, so one file cannot fill the context.
MAX_AGENTS_MD_BYTES = 32 * 1024


def read_agents_md(cwd: Path, can_read: Callable[[Path], bool] | None = None) -> str | None:
    """Content of <cwd>/AGENTS.md, or None if there is no such file, it is empty or can_read refuses it."""
    path = cwd / AGENTS_MD_FILE
    if not path.is_file() or (can_read is not None and not can_read(path)):
        return None
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_AGENTS_MD_BYTES)
    except OSError:
        return None
    text = data.decode("utf-8", errors="replace").strip()
    return text or None


def format_agents_md(cwd: Path, text: str) -> str:
    return f"# AGENTS.md instructions for {cwd}\n\n<INSTRUCTIONS>\n{text}\n</INSTRUCTIONS>"


def load_agents_md(cwd: Path, can_read: Callable[[Path], bool] | None = None) -> Item | None:
    """The AGENTS.md history item for a new session, or None."""
    text = read_agents_md(cwd, can_read)
    if text is None:
        return None
    return Item(Role.USER, Kind.AGENTS_MD, content=format_agents_md(cwd, text))
