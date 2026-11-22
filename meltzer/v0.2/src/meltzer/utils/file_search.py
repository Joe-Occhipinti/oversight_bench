"""Find files under a directory by a fuzzy query (the `@` file picker of the terminal UI).

The files are those `git ls-files` lists (tracked and untracked, without
ignored files) when the directory is in a Git work tree, or else those found
by walking the directory, skipping hidden directories (names starting
with `.`). A query
matches a path when its characters appear in the path in order, ignoring case.
"""

from __future__ import annotations

import os
from pathlib import Path

from meltzer.utils.git import run_git

MAX_FILES = 50_000
MAX_RESULTS = 20

# Characters after which a match starts a new word of the path.
WORD_SEPARATORS = "/_-. "


def list_files(root: Path, *, max_files: int = MAX_FILES) -> list[str]:
    """Paths of the files under root, relative to it, with `/` separators; at most max_files."""
    files = _git_files(root, max_files)
    if files is None:
        files = _walk_files(root, max_files)
    return files


def _git_files(root: Path, max_files: int) -> list[str] | None:
    """Files git lists in root, or None when root is not in a work tree or git cannot run."""
    result = run_git(["ls-files", "--cached", "--others", "--exclude-standard", "-z"], root)
    if result is None or result.returncode != 0:
        return None
    names = [os.fsdecode(name) for name in result.stdout.split(b"\0") if name]
    # --cached and --others both list a file that is staged as deleted and present again.
    return list(dict.fromkeys(names))[:max_files]


def _walk_files(root: Path, max_files: int) -> list[str]:
    files: list[str] = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        relative = Path(directory).relative_to(root)
        for name in sorted(filenames):
            files.append((relative / name).as_posix())
            if len(files) >= max_files:
                return files
    return files


def match_score(query: str, path: str) -> int | None:
    """How well path matches query (higher is better), or None when it does not match."""
    if not query:
        return 0
    lowered = path.lower()
    needle = query.lower()
    name_start = lowered.rfind("/") + 1
    # Match in the file name when the whole query fits there, else from the start of the path.
    start = name_start if _subsequence(needle, lowered[name_start:]) is not None else 0
    positions = _subsequence(needle, lowered[start:])
    if positions is None:
        return None
    positions = [start + p for p in positions]
    score = 0
    if needle in lowered[name_start:]:
        score += 100
    elif needle in lowered:
        score += 50
    if start == name_start:
        score += 20
    for previous, current in zip(positions, positions[1:]):
        if current == previous + 1:
            score += 10
    for position in positions:
        if position == 0 or lowered[position - 1] in WORD_SEPARATORS:
            score += 5
    return score


def _subsequence(needle: str, haystack: str) -> list[int] | None:
    positions = []
    index = 0
    for char in needle:
        index = haystack.find(char, index)
        if index < 0:
            return None
        positions.append(index)
        index += 1
    return positions


def search(query: str, files: list[str], limit: int = MAX_RESULTS) -> list[str]:
    """The best matches for query, best first; shorter paths first among equal scores."""
    scored = []
    for path in files:
        score = match_score(query, path)
        if score is not None:
            scored.append((-score, len(path), path))
    scored.sort()
    return [path for _, _, path in scored[:limit]]
