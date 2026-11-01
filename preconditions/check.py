#!/usr/bin/env python3
"""Quick project check.

Compiles every Python file, runs `bash -n` on every shell script and parses every JSON file
under the project directory, then prints a one-line summary. Failures are listed on stderr and
the exit status is 1 when any file fails. Only the standard library is used.

    python3 scripts/check.py [directory]
"""

from __future__ import annotations

import json
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SKIP_DIRS = {".git", ".meltzer", ".codegraph", "__pycache__"}
MAX_FILES = 2000  # files checked
MAX_VISITED = 20000  # directory entries looked at
MAX_FILE_BYTES = 1024 * 1024
KINDS = {".py": "python", ".sh": "shell", ".json": "json"}


def find_files(root: Path) -> list[Path]:
    """Files to check under root in a stable order, at most MAX_FILES."""
    found: list[Path] = []
    visited = 0
    for current, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(names):
            visited += 1
            path = Path(current) / name
            if path.suffix in KINDS and not path.is_symlink():
                found.append(path)
            if visited >= MAX_VISITED or len(found) >= MAX_FILES:
                return found
    return found


def check_python(path: Path, scratch: str) -> str | None:
    try:
        py_compile.compile(str(path), cfile=os.path.join(scratch, "check.pyc"), doraise=True)
    except py_compile.PyCompileError as exc:
        return str(exc.msg).strip().splitlines()[-1]
    except (OSError, ValueError) as exc:
        return str(exc)
    return None


def check_shell(path: Path, scratch: str) -> str | None:
    try:
        done = subprocess.run(
            ["bash", "-n", path.as_posix()],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return "bash -n timed out"
    if done.returncode != 0:
        lines = done.stderr.strip().splitlines()
        return lines[0] if lines else f"bash -n exited with status {done.returncode}"
    return None


def check_json(path: Path, scratch: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            json.load(handle)
    except (ValueError, OSError) as exc:
        return str(exc)
    return None


CHECKS = {"python": check_python, "shell": check_shell, "json": check_json}


def run(root: Path) -> tuple[dict[str, int], int, list[tuple[Path, str]]]:
    """Check the files under root; return counts per kind, skipped files and failures."""
    counts = {kind: 0 for kind in CHECKS}
    skipped = 0
    failures: list[tuple[Path, str]] = []
    have_bash = shutil.which("bash") is not None
    with tempfile.TemporaryDirectory() as scratch:
        for path in find_files(root):
            kind = KINDS[path.suffix]
            try:
                too_big = path.stat().st_size > MAX_FILE_BYTES
            except OSError:
                too_big = True
            if too_big or (kind == "shell" and not have_bash):
                skipped += 1
                continue
            counts[kind] += 1
            problem = CHECKS[kind](path, scratch)
            if problem:
                failures.append((path, problem))
    return counts, skipped, failures


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent
    if not root.is_dir():
        print(f"check: not a directory: {root}", file=sys.stderr)
        return 2
    counts, skipped, failures = run(root)
    total = sum(counts.values())
    detail = ", ".join(f"{n} {kind}" for kind, n in counts.items() if n)
    summary = f"checked {total} files" + (f" ({detail})" if detail else "")
    if skipped:
        summary += f", skipped {skipped}"
    summary += f": {len(failures)} failed" if failures else ": ok"
    print(summary)
    for path, problem in failures:
        print(f"  {path.relative_to(root).as_posix()}: {problem}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
