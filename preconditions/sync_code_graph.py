#!/usr/bin/env python3
"""Refresh the code graph of the project.

Meant to run as a hook after files change. It reads the hook event (JSON) from stdin, scans the
source files under the project directory and writes the imports and top-level definitions of each
file to .codegraph/graph.json, so that other tools can look up where a name is defined.

Python files are parsed with ast. Shell scripts and C sources are matched with regular
expressions, line by line. Only the standard library is used.

    python3 tools/sync_code_graph.py < event.json
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SKIP_DIRS = {".git", ".meltzer", ".codegraph", "__pycache__"}
MAX_FILES = 2000  # source files recorded
MAX_VISITED = 20000  # directory entries looked at
MAX_FILE_BYTES = 512 * 1024
GRAPH_DIR = ".codegraph"
GRAPH_FILE = "graph.json"

LANGUAGES = {".py": "python", ".sh": "shell", ".c": "c", ".h": "c"}

SHELL_SOURCE = re.compile(r"^\s*(?:source|\.)\s+([^\s;&|]+)")
SHELL_FUNCTION = re.compile(
    r"^(?:function\s+([A-Za-z_][\w.:-]*)(?:\s*\(\s*\))?|([A-Za-z_][\w.:-]*)\s*\(\s*\))"
)

C_INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]')
C_MACRO = re.compile(r"^#\s*define\s+([A-Za-z_]\w*)")
C_TYPE = re.compile(r"^(?:typedef\s+)?(struct|union|enum)\s+([A-Za-z_]\w*)\s*\{")
C_FUNCTION = re.compile(
    r"^(?!(?:if|for|while|switch|return|else|do|typedef|sizeof)\b)"
    r"[A-Za-z_][\w\s\*]*?[\s\*]([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{?\s*$"
)


def scan_python(text: str) -> tuple[list[str], list[dict]]:
    tree = ast.parse(text)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.append((node.lineno, "." * node.level + (node.module or "")))
    imports = list(dict.fromkeys(name for _, name in sorted(found)))

    definitions = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definitions.append({"name": node.name, "kind": "function", "line": node.lineno})
        elif isinstance(node, ast.ClassDef):
            definitions.append({"name": node.name, "kind": "class", "line": node.lineno})
    return imports, definitions


def scan_shell(text: str) -> tuple[list[str], list[dict]]:
    imports: list[str] = []
    definitions: list[dict] = []
    for number, line in enumerate(text.splitlines(), start=1):
        sourced = SHELL_SOURCE.match(line)
        if sourced:
            imports.append(sourced.group(1))
            continue
        function = SHELL_FUNCTION.match(line)
        if function:
            name = function.group(1) or function.group(2)
            definitions.append({"name": name, "kind": "function", "line": number})
    return list(dict.fromkeys(imports)), definitions


def scan_c(text: str) -> tuple[list[str], list[dict]]:
    imports: list[str] = []
    definitions: list[dict] = []
    for number, line in enumerate(text.splitlines(), start=1):
        included = C_INCLUDE.match(line)
        if included:
            imports.append(included.group(1))
            continue
        macro = C_MACRO.match(line)
        if macro:
            definitions.append({"name": macro.group(1), "kind": "macro", "line": number})
            continue
        declared = C_TYPE.match(line)
        if declared:
            definitions.append({"name": declared.group(2), "kind": declared.group(1), "line": number})
            continue
        function = C_FUNCTION.match(line)
        if function:
            definitions.append({"name": function.group(1), "kind": "function", "line": number})
    return list(dict.fromkeys(imports)), definitions


SCANNERS = {"python": scan_python, "shell": scan_shell, "c": scan_c}


def find_sources(root: Path) -> tuple[list[Path], bool]:
    """Source files under root in a stable order, and whether a limit cut the search short."""
    found: list[Path] = []
    visited = 0
    for current, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(names):
            visited += 1
            if visited > MAX_VISITED:
                return found, True
            path = Path(current) / name
            if path.suffix not in LANGUAGES or path.is_symlink():
                continue
            if len(found) >= MAX_FILES:
                return found, True
            found.append(path)
    return found, False


def build_graph(root: Path) -> dict:
    paths, truncated = find_sources(root)
    files = {}
    for path in paths:
        language = LANGUAGES[path.suffix]
        entry = {"language": language, "imports": [], "definitions": []}
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            entry["imports"], entry["definitions"] = SCANNERS[language](text)
        except (OSError, SyntaxError, ValueError, RecursionError):
            entry["error"] = "could not be parsed"
        files[path.relative_to(root).as_posix()] = entry
    return {
        "root": str(root),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "truncated": truncated,
        "files": files,
    }


def write_graph(root: Path, graph: dict) -> Path:
    directory = root / GRAPH_DIR
    directory.mkdir(exist_ok=True)
    target = directory / GRAPH_FILE
    staged = directory / (GRAPH_FILE + ".tmp")
    staged.write_text(json.dumps(graph, indent=1) + "\n", encoding="utf-8")
    os.replace(staged, target)
    return target


def read_event() -> dict:
    """The hook event from stdin; empty when run by hand or when it is not a JSON object."""
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return {}
    return event if isinstance(event, dict) else {}


def main() -> int:
    event = read_event()
    root = Path(event.get("cwd") or os.getcwd())
    if not root.is_dir():
        print(f"sync_code_graph: not a directory: {root}", file=sys.stderr)
        return 1
    graph = build_graph(root)
    target = write_graph(root, graph)
    definitions = sum(len(entry["definitions"]) for entry in graph["files"].values())
    print(f"code graph: {len(graph['files'])} files, {definitions} definitions -> {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
