"""Read-only tools for the reviewer: read_file, list_dir, grep.

The reviewer can inspect local state (a script a command would run, the target
of a deletion) before deciding. These tools are implemented in Python and only
open files for reading, so they need no sandboxed process and cannot change
anything. Relative paths are resolved against the session directory.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from meltzer.tools.registry import ToolError, ToolRegistry, ToolSpec, optional_string, require_string

READ_FILE_MAX_LINES = 300
READ_FILE_HEAD_LINES = 120
READ_FILE_TAIL_LINES = 120
MAX_FILE_BYTES = 2 * 1024 * 1024
BINARY_CHECK_BYTES = 8192
LIST_DIR_MAX_ENTRIES = 200
GREP_MAX_MATCHES = 100
GREP_MAX_LINE_CHARS = 400
GREP_SKIP_DIRS = {".git"}


def _resolve(cwd: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else cwd / path


def _read_bytes(path: Path) -> bytes:
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ToolError(f"{path} is too large to read ({size} bytes); use grep to search it")
        return path.read_bytes()
    except OSError as exc:
        raise ToolError(f"cannot read {path}: {exc.strerror}") from exc


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:BINARY_CHECK_BYTES]


def truncate_lines(lines: list[str]) -> list[str]:
    if len(lines) <= READ_FILE_MAX_LINES:
        return lines
    omitted = len(lines) - READ_FILE_HEAD_LINES - READ_FILE_TAIL_LINES
    return [*lines[:READ_FILE_HEAD_LINES], f"<truncated {omitted} lines/>", *lines[-READ_FILE_TAIL_LINES:]]


class ReadFileTool:
    spec = ToolSpec(
        name="read_file",
        description=(
            f"Read a text file. Files longer than {READ_FILE_MAX_LINES} lines are shown as their first "
            f"{READ_FILE_HEAD_LINES} and last {READ_FILE_TAIL_LINES} lines."
        ),
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File path, absolute or relative to the session directory."}},
            "required": ["path"],
            "additionalProperties": False,
        },
    )

    def __init__(self, cwd: Path):
        self.cwd = cwd

    def run(self, arguments: dict[str, Any]) -> str:
        path = _resolve(self.cwd, require_string(arguments, "path"))
        if not path.is_file():
            raise ToolError(f"not a file: {path}")
        data = _read_bytes(path)
        if _is_binary(data):
            return f"{path} is a binary file ({len(data)} bytes)"
        lines = data.decode("utf-8", errors="replace").splitlines()
        return "\n".join(truncate_lines(lines))


class ListDirTool:
    spec = ToolSpec(
        name="list_dir",
        description="List a directory. Directories end with '/', symbolic links show their target.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Directory path; defaults to the session directory."}},
            "additionalProperties": False,
        },
    )

    def __init__(self, cwd: Path):
        self.cwd = cwd

    def run(self, arguments: dict[str, Any]) -> str:
        path = _resolve(self.cwd, optional_string(arguments, "path") or ".")
        if not path.is_dir():
            raise ToolError(f"not a directory: {path}")
        try:
            children = sorted(path.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            raise ToolError(f"cannot list {path}: {exc.strerror}") from exc
        lines = []
        for child in children[:LIST_DIR_MAX_ENTRIES]:
            if child.is_symlink():
                try:
                    target = os.readlink(child)
                except OSError as exc:
                    target = f"<unreadable: {exc.strerror}>"
                lines.append(f"{child.name} -> {target}")
            elif child.is_dir():
                lines.append(f"{child.name}/")
            else:
                lines.append(child.name)
        if len(children) > LIST_DIR_MAX_ENTRIES:
            lines.append(f"... {len(children) - LIST_DIR_MAX_ENTRIES} more entries")
        return "\n".join(lines) if lines else f"{path} is empty"


class GrepTool:
    spec = ToolSpec(
        name="grep",
        description=(
            "Search files for a regular expression (Python syntax). Searches a file, or every file "
            f"under a directory (skipping .git). Returns up to {GREP_MAX_MATCHES} matches as path:line:text."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression."},
                "path": {"type": "string", "description": "File or directory; defaults to the session directory."},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    )

    def __init__(self, cwd: Path):
        self.cwd = cwd

    def _files(self, path: Path) -> list[Path]:
        if path.is_file():
            return [path]
        files = []
        for directory, subdirs, names in os.walk(path):
            subdirs[:] = sorted(d for d in subdirs if d not in GREP_SKIP_DIRS)
            files.extend(Path(directory) / name for name in sorted(names))
        return files

    def run(self, arguments: dict[str, Any]) -> str:
        try:
            pattern = re.compile(require_string(arguments, "pattern"))
        except re.error as exc:
            raise ToolError(f"invalid pattern: {exc}") from exc
        path = _resolve(self.cwd, optional_string(arguments, "path") or ".")
        if not path.exists():
            raise ToolError(f"no such file or directory: {path}")
        matches: list[str] = []
        for file in self._files(path):
            try:
                if not file.is_file() or file.stat().st_size > MAX_FILE_BYTES:
                    continue
                data = file.read_bytes()
            except OSError:
                continue
            if _is_binary(data):
                continue
            shown = file.relative_to(self.cwd) if file.is_relative_to(self.cwd) else file
            for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), start=1):
                if pattern.search(line):
                    matches.append(f"{shown}:{number}:{line[:GREP_MAX_LINE_CHARS]}")
                    if len(matches) >= GREP_MAX_MATCHES:
                        matches.append(f"(stopped after {GREP_MAX_MATCHES} matches)")
                        return "\n".join(matches)
        return "\n".join(matches) if matches else "no matches"


def review_tools(cwd: Path) -> ToolRegistry:
    return ToolRegistry([ReadFileTool(cwd), ListDirTool(cwd), GrepTool(cwd)])
