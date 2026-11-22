"""Count the lines of a meltzer tree: source, tests and docs, per area and in total.

Usage: python scripts/meltzer_size.py [--version v0.2] [--cap 30000]

Areas follow the package layout: each top-level module or package under
src/meltzer is one area. Lines are all lines, blank ones included.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_SUFFIXES = {".py", ".md", ".sbpl", ".toml", ".json"}


def count_lines(path: Path) -> int:
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def files(directory: Path, suffixes: set[str]) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.rglob("*")
        if p.is_file() and p.suffix in suffixes and "__pycache__" not in p.parts
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="v0.2")
    parser.add_argument("--cap", type=int, default=30_000, help="maximum total lines (default 30000)")
    args = parser.parse_args()
    tree = ROOT / "meltzer" / args.version
    package = tree / "src" / "meltzer"

    areas: Counter[str] = Counter()
    for path in files(package, SOURCE_SUFFIXES):
        relative = path.relative_to(package)
        area = relative.parts[0] if len(relative.parts) > 1 else relative.stem
        areas[area] += count_lines(path)
    source = sum(areas.values())
    tests = sum(count_lines(p) for p in files(tree / "tests", {".py"}))
    docs = sum(count_lines(p) for p in [tree / "README.md", *files(tree / "docs", {".md"})] if p.is_file())
    total = source + tests + docs

    print(f"meltzer {args.version}")
    print(f"{'area':<24}{'lines':>8}")
    for area, lines in sorted(areas.items(), key=lambda entry: -entry[1]):
        print(f"{area:<24}{lines:>8}")
    print(f"{'source':<24}{source:>8}")
    print(f"{'tests':<24}{tests:>8}   ({tests / max(1, source + tests):.0%} of source + tests)")
    print(f"{'docs':<24}{docs:>8}   ({docs / max(1, total):.0%} of total)")
    print(f"{'total':<24}{total:>8}   (cap {args.cap})")
    return 0 if total <= args.cap else 1


if __name__ == "__main__":
    sys.exit(main())
