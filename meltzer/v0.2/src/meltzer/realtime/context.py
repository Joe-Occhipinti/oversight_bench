"""Startup context for a voice conversation: the workspace root and a short listing of it."""

from __future__ import annotations

import os
from pathlib import Path

TREE_MAX_DEPTH = 2
DIR_ENTRY_LIMIT = 20
# About 1,600 tokens at 4 characters per token.
CONTEXT_MAX_CHARS = 6_400
SKIPPED_NAMES = frozenset(
    {"build", "dist", "node_modules", "out", "target", "__pycache__", ".git", ".next", ".pytest_cache", ".ruff_cache"}
)
OPEN_TAG = "<startup_context>"
CLOSE_TAG = "</startup_context>"
CUT_LINE = "- ..."


def startup_context(root: Path, max_chars: int = CONTEXT_MAX_CHARS) -> str:
    """The workspace root and its tree to depth 2, cut to `max_chars` characters at a line boundary."""
    lines = [OPEN_TAG, f"Workspace root: {root}", "Workspace tree:"]
    budget = max_chars - len(CLOSE_TAG) - len(CUT_LINE) - 1
    used = sum(len(line) + 1 for line in lines)
    for line in tree_lines(root):
        if used + len(line) + 1 > budget:
            lines.append(CUT_LINE)
            break
        lines.append(line)
        used += len(line) + 1
    lines.append(CLOSE_TAG)
    return "\n".join(lines)


def tree_lines(directory: Path, depth: int = 0) -> list[str]:
    """Directories first, then files, by name; hidden and build directories are left out; links are not followed."""
    if depth >= TREE_MAX_DEPTH:
        return []
    try:
        with os.scandir(directory) as scan:
            entries = [entry for entry in scan if not _skipped(entry.name)]
    except OSError:
        return []
    entries.sort(key=lambda entry: (not entry.is_dir(follow_symlinks=False), entry.name))
    indent = "  " * depth
    lines = []
    for entry in entries[:DIR_ENTRY_LIMIT]:
        is_dir = entry.is_dir(follow_symlinks=False)
        lines.append(f"{indent}- {entry.name}{'/' if is_dir else ''}")
        if is_dir:
            lines.extend(tree_lines(Path(entry.path), depth + 1))
    if len(entries) > DIR_ENTRY_LIMIT:
        lines.append(f"{indent}- ... {len(entries) - DIR_ENTRY_LIMIT} more entries")
    return lines


def _skipped(name: str) -> bool:
    return name.startswith(".") or name in SKIPPED_NAMES
