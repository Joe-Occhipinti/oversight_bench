"""Check that the code citations in registry cards exist in a meltzer tree.

Usage: python scripts/meltzer_symbols.py [--version v0.2] [--registry registry/v0.2/cards] [--list]

A card cites code as `src/meltzer/<path>.py::Name` or `src/meltzer/<path>.py::Class.method`
(or a plain file path such as `src/meltzer/monitor/policy.md`). Each citation is
checked against the tree: the file must exist and, for a symbol, a top-level
function or class with that name (and the method inside the class) must be defined.
With --list, the cited symbols are printed, one per line, for the freeze record.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CITATION = re.compile(r"`(src/meltzer/[\w/.-]+?)(?:::([\w.]+))?`")


def defined(tree: ast.Module, symbol: str) -> bool:
    parts = symbol.split(".")
    nodes: list[ast.stmt] = tree.body
    for part in parts:
        match = next(
            (
                node for node in nodes
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == part
            ),
            None,
        )
        if match is None:
            # A module-level or class-level name: an assignment.
            return part == parts[-1] and any(
                isinstance(target, ast.Name) and target.id == part
                for node in nodes
                for target in (node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)])
                if isinstance(node, (ast.Assign, ast.AnnAssign))
            )
        nodes = getattr(match, "body", [])
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="v0.2")
    parser.add_argument("--registry", default=None, help="cards directory (default registry/<version>/cards)")
    parser.add_argument("--list", action="store_true", help="print the cited symbols")
    args = parser.parse_args()
    tree_root = ROOT / "meltzer" / args.version
    cards = Path(args.registry) if args.registry else ROOT / "registry" / args.version / "cards"
    citations: set[tuple[str, str | None]] = set()
    for card in sorted(cards.glob("*.md")):
        citations.update(CITATION.findall(card.read_text(encoding="utf-8")))
    parsed: dict[str, ast.Module] = {}
    missing = 0
    for path, symbol in sorted(citations, key=lambda c: (c[0], c[1] or "")):
        file = tree_root / path
        if args.list:
            print(f"{path}::{symbol}" if symbol else path)
        if not file.is_file():
            print(f"missing file: {path}")
            missing += 1
            continue
        if symbol:
            if path not in parsed:
                parsed[path] = ast.parse(file.read_text(encoding="utf-8"))
            if not defined(parsed[path], symbol):
                print(f"missing symbol: {path}::{symbol}")
                missing += 1
    print(f"{len(citations)} citation(s), {missing} missing")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
