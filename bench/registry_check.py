"""Static check of the registry (spec 3.4 step 2): cards parse, ids are unique, every citation resolves
in the target source.

Citations in card bodies are backticked: `path/to/file.py::Symbol.attr` (path relative to the target root).
Non-Python files are cited by path only (`path/to/policy.md`).
"""

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from bench import cards

CITATION = re.compile(r"`([\w./-]+\.(?:py|md|toml))(?:::([\w.]+))?`")


@dataclass(frozen=True)
class Problem:
    card: str
    message: str


def _symbols(tree: ast.Module) -> dict[str, ast.AST]:
    """Qualified names (Class, Class.method, function, CONSTANT) defined in a module."""
    out: dict[str, ast.AST] = {}

    def visit(body, prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + node.name
                out[name] = node
                if isinstance(node, ast.ClassDef):
                    visit(node.body, name + ".")
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        out[prefix + t.id] = node

    visit(tree.body, "")
    return out


def check_citation(target_root: Path, path: str, symbol: str | None, cache: dict) -> str | None:
    file = target_root / path
    if not file.is_file():
        return f"file not found: {path}"
    if symbol is None:
        return None
    if file.suffix != ".py":
        return f"symbol citations need a .py file: {path}::{symbol}"
    if path not in cache:
        cache[path] = _symbols(ast.parse(file.read_text(encoding="utf-8")))
    if symbol not in cache[path]:
        return f"symbol not found: {path}::{symbol}"
    return None


def check_registry(cards_dir: Path, target_root: Path, expected_target: str, expected_version: str) -> list[Problem]:
    problems: list[Problem] = []
    try:
        loaded = cards.load_registry(cards_dir)
    except cards.CardError as e:
        return [Problem("-", str(e))]
    if not loaded:
        return [Problem("-", f"no cards in {cards_dir}")]
    cache: dict = {}
    for card in loaded:
        header, _ = cards.split_header(card.text, where=card.path.name)
        if header.get("target") != expected_target or str(header.get("version")) != expected_version:
            problems.append(Problem(card.id, f"target/version must be {expected_target}/{expected_version}"))
        if not card.path.name.startswith(f"{card.id}-"):
            problems.append(Problem(card.id, f"file name must start with '{card.id}-'"))
        for section in ("## Summary", "## Preconditions", "## Steps"):
            if section not in card.body:
                problems.append(Problem(card.id, f"missing section {section}"))
        found = CITATION.findall(card.body)
        if not found:
            problems.append(Problem(card.id, "no citations"))
        for path, symbol in found:
            msg = check_citation(target_root, path, symbol or None, cache)
            if msg:
                problems.append(Problem(card.id, msg))
    return problems
