"""Skill discovery: SKILL.md files under the skills directories.

Skills are read when a session starts from:

    <session root>/.meltzer/skills/   project skills
    ~/.meltzer/skills/                user skills

Each skill is a directory with a SKILL.md file that starts with a frontmatter
block:

    ---
    name: deploy                          # default: the directory name
    description: Deploy the service to staging
    metadata:
      short-description: Deploy to staging
    ---
    <instructions>

Directories are scanned down to MAX_SCAN_DEPTH levels below a skills
directory, at most MAX_SKILL_DIRS_PER_ROOT directories per skills directory;
hidden directories and symbolic links are skipped. `[[skills.config]]` entries
in the user config (`name` or `path`, `enabled`) turn skills off and on.
"""

from __future__ import annotations

import os
import re
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Sequence

from meltzer.sandbox.policy import resolve

SKILLS_DIR_NAME = "skills"
SKILL_FILE_NAME = "SKILL.md"
MAX_SCAN_DEPTH = 6
MAX_SKILL_DIRS_PER_ROOT = 2000
MAX_NAME_CHARS = 64
DEFAULT_SKILL_NAME = "skill"
CONFIG_RULE_KEYS = ("name", "path", "enabled")

_KEY_LINE = re.compile(r"^([^\s:#'\"][^:]*?)\s*:(?:\s+(.*))?$")
_DOUBLE_QUOTE_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "/": "/"}


class SkillScope(StrEnum):
    REPO = "repo"
    USER = "user"


# Catalog order: project skills first, then user skills.
SCOPE_ORDER = (SkillScope.REPO, SkillScope.USER)


@dataclass(frozen=True)
class SkillRoot:
    path: Path
    scope: SkillScope


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path  # the SKILL.md file
    scope: SkillScope
    short_description: str | None = None


class SkillError(ValueError):
    """A SKILL.md file is invalid; the skill is not loaded."""


# Frontmatter: the subset of YAML that SKILL.md files use (mappings, scalars, lists of scalars).


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _significant(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _next_significant(lines: list[str], index: int) -> int:
    while index < len(lines) and not _significant(lines[index]):
        index += 1
    return index


def _without_comment(value: str) -> str:
    match = re.search(r"\s#", value)
    return (value[: match.start()] if match else value).rstrip()


def _plain(value: str) -> str | None:
    return None if value in ("~", "null", "Null", "NULL") else value


def _quoted(value: str, number: int) -> str:
    quote, out, index = value[0], [], 1
    while index < len(value):
        char = value[index]
        if quote == "'" and char == "'" and value[index + 1 : index + 2] == "'":
            out.append("'")
            index += 2
            continue
        if quote == '"' and char == "\\" and index + 1 < len(value):
            escaped = value[index + 1]
            out.append(_DOUBLE_QUOTE_ESCAPES.get(escaped, escaped))
            index += 2
            continue
        if char == quote:
            rest = value[index + 1 :].strip()
            if rest and not rest.startswith("#"):
                raise SkillError(f"frontmatter line {number}: unexpected text after a quoted value")
            return "".join(out)
        out.append(char)
        index += 1
    raise SkillError(f"frontmatter line {number}: unterminated quoted value")


def _block_scalar(lines: list[str], index: int, indent: int, header: str) -> tuple[int, str]:
    block: list[str] = []
    while index < len(lines) and (not lines[index].strip() or _indent(lines[index]) > indent):
        block.append(lines[index])
        index += 1
    content = [line for line in block if line.strip()]
    margin = min((_indent(line) for line in content), default=0)
    text_lines = [line[margin:] if line.strip() else "" for line in block]
    if header.startswith(">"):
        text = ""
        for line in text_lines:
            if not line:
                text += "\n"
            elif text and not text.endswith("\n"):
                text += " " + line
            else:
                text += line
    else:
        text = "\n".join(text_lines)
    # Chomping: "-" drops the final line break, "+" keeps trailing blank lines.
    if "-" in header:
        return index, text.rstrip("\n")
    if "+" in header:
        return index, text + "\n"
    text = text.rstrip("\n")
    return index, text + "\n" if text else ""


def _plain_scalar(lines: list[str], index: int, indent: int, value: str) -> tuple[int, str | None]:
    parts = [_without_comment(value)]
    while index < len(lines) and (not lines[index].strip() or _indent(lines[index]) > indent):
        if _significant(lines[index]):
            part = _without_comment(lines[index].strip())
            if re.search(r":(\s|$)", part):
                raise SkillError(f"frontmatter line {index + 1}: unexpected indentation")
            parts.append(part)
        index += 1
    return index, _plain(" ".join(parts))


def _list(lines: list[str], index: int, indent: int) -> tuple[int, list[str | None]]:
    items: list[str | None] = []
    while index < len(lines):
        line = lines[index]
        if not _significant(line):
            index += 1
            continue
        stripped = line.strip()
        if _indent(line) < indent or (_indent(line) == indent and not stripped.startswith("-")):
            break
        if _indent(line) == indent:
            value = stripped[1:].strip()
            if value[:1] in ("'", '"'):
                items.append(_quoted(value, index + 1))
            else:
                items.append(_plain(_without_comment(value)))
        index += 1
    return index, items


def _value(lines: list[str], index: int, indent: int) -> tuple[int, Any]:
    """The nested value after a key with nothing on its line: a mapping, a list or null."""
    start = _next_significant(lines, index)
    if start == len(lines):
        return start, None
    line = lines[start]
    stripped = line.strip()
    if (stripped == "-" or stripped.startswith("- ")) and _indent(line) >= indent:
        return _list(lines, start, _indent(line))
    if _indent(line) > indent:
        data, end = _mapping(lines, start, _indent(line))
        return end, data
    return index, None


def _mapping(lines: list[str], index: int, indent: int) -> tuple[dict[str, Any], int]:
    data: dict[str, Any] = {}
    while index < len(lines):
        line = lines[index]
        if not _significant(line):
            index += 1
            continue
        current = _indent(line)
        if current < indent:
            break
        number = index + 1
        if current > indent:
            raise SkillError(f"frontmatter line {number}: unexpected indentation")
        match = _KEY_LINE.match(line.strip())
        if match is None:
            raise SkillError(f"frontmatter line {number}: expected 'key: value'")
        key, value = match.group(1), (match.group(2) or "").strip()
        index += 1
        if not value or value.startswith("#"):
            index, data[key] = _value(lines, index, current)
        elif value[0] in "|>":
            index, data[key] = _block_scalar(lines, index, current, value.split()[0])
        elif value[0] in "'\"":
            data[key] = _quoted(value, number)
        else:
            index, data[key] = _plain_scalar(lines, index, current, value)
    return data, index


def parse_frontmatter(text: str) -> dict[str, Any]:
    """The frontmatter block at the start of a SKILL.md file, as a dict."""
    lines = text.removeprefix("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillError("missing frontmatter delimited by ---")
    for end in range(1, len(lines)):
        if lines[end].strip() == "---":
            break
    else:
        raise SkillError("missing frontmatter delimited by ---")
    block = lines[1:end]
    if not block:
        raise SkillError("missing frontmatter delimited by ---")
    data, _ = _mapping(block, 0, 0)
    return data


# Skills.


def _single_line(text: str) -> str:
    return " ".join(text.split())


def _text_field(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SkillError(f"invalid {key}: expected a string")
    return _single_line(value)


def default_skill_name(path: Path) -> str:
    return _single_line(path.parent.name) or DEFAULT_SKILL_NAME


def parse_skill(text: str, path: Path, scope: SkillScope) -> Skill:
    """Parse the SKILL.md file at path."""
    data = parse_frontmatter(text)
    metadata = data.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise SkillError("invalid metadata: expected a mapping")
    name = _text_field(data, "name") or default_skill_name(path)
    if len(name) > MAX_NAME_CHARS:
        raise SkillError(f"invalid name: exceeds maximum length of {MAX_NAME_CHARS} characters")
    description = _text_field(data, "description")
    if not description:
        raise SkillError("missing field `description`")
    short_description = _text_field(metadata, "short-description") if metadata else None
    return Skill(
        name=name,
        description=description,
        path=path,
        scope=scope,
        short_description=short_description or None,
    )


def skill_roots(meltzer_home: Path, root: Path, *, include_project: bool = False) -> list[SkillRoot]:
    """The skills directories of a session rooted at root."""
    found = []
    if include_project:
        found.append(SkillRoot(root / ".meltzer" / SKILLS_DIR_NAME, SkillScope.REPO))
    found.append(SkillRoot(meltzer_home / SKILLS_DIR_NAME, SkillScope.USER))
    return found


def find_skill_files(root: Path) -> tuple[list[Path], list[str]]:
    """SKILL.md files under root, in path order, and warnings."""
    base = resolve(root)
    if not base.is_dir():
        return [], []
    found: list[Path] = []
    warnings: list[str] = []
    queue: deque[tuple[Path, int]] = deque([(base, 0)])
    scanned = 0
    while queue:
        directory, depth = queue.popleft()
        if scanned == MAX_SKILL_DIRS_PER_ROOT:
            warnings.append(f"{root}: skills scan reached its limit of {MAX_SKILL_DIRS_PER_ROOT} directories")
            break
        scanned += 1
        try:
            with os.scandir(directory) as entries:
                children = sorted(entries, key=lambda entry: entry.name)
        except OSError as exc:
            warnings.append(f"{directory}: cannot read skills directory: {exc.strerror}")
            continue
        for entry in children:
            if entry.is_symlink():
                continue
            if entry.name == SKILL_FILE_NAME and entry.is_file():
                found.append(Path(entry.path))
            elif entry.is_dir() and not entry.name.startswith(".") and depth < MAX_SCAN_DEPTH:
                queue.append((Path(entry.path), depth + 1))
    return sorted(found), warnings


def _rule_path(value: str, home: Path | None) -> Path:
    if value == "~" or value.startswith("~/"):
        value = str(home if home is not None else Path.home()) + value[1:]
    return resolve(Path(value))


def disabled_skill_paths(
    skills: Sequence[Skill], entries: Iterable[dict[str, Any]], *, home: Path | None = None
) -> tuple[set[Path], list[str]]:
    """SKILL.md paths that `[[skills.config]]` entries turn off, and warnings about invalid entries.

    Entries apply in order, so a later entry for the same skill wins.
    """
    disabled: set[Path] = set()
    warnings: list[str] = []
    for number, entry in enumerate(entries, start=1):
        where = f"skills.config entry {number}"
        unknown = [key for key in entry if key not in CONFIG_RULE_KEYS]
        name, path, enabled = entry.get("name"), entry.get("path"), entry.get("enabled")
        if unknown:
            warnings.append(f"{where}: unknown key '{unknown[0]}'; entry ignored")
            continue
        if not isinstance(enabled, bool):
            warnings.append(f"{where}: 'enabled' must be true or false; entry ignored")
            continue
        if (name is None) == (path is None):
            warnings.append(f"{where}: expected one of 'name' or 'path'; entry ignored")
            continue
        selector = name if name is not None else path
        if not isinstance(selector, str) or not selector.strip():
            warnings.append(f"{where}: 'name' and 'path' must be non-empty strings; entry ignored")
            continue
        if name is not None:
            paths = {skill.path for skill in skills if skill.name == name.strip()}
        else:
            paths = {_rule_path(path, home)}
        if enabled:
            disabled -= paths
        else:
            disabled |= paths
    return disabled, warnings


class SkillSet:
    def __init__(self, skills: list[Skill], disabled: Iterable[Path] = (), warnings: list[str] | None = None):
        self.skills = list(skills)
        self.disabled = set(disabled)
        self.warnings = list(warnings or [])

    @classmethod
    def load(
        cls,
        roots: Sequence[SkillRoot],
        config_entries: Iterable[dict[str, Any]] = (),
        *,
        home: Path | None = None,
    ) -> SkillSet:
        """Skills from roots, in catalog order, with the `[[skills.config]]` entries applied."""
        skills: list[Skill] = []
        warnings: list[str] = []
        seen: set[Path] = set()
        for root in roots:
            files, problems = find_skill_files(root.path)
            warnings.extend(problems)
            for path in files:
                if path in seen:
                    continue
                seen.add(path)
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    warnings.append(f"{path}: cannot read skill: {exc}")
                    continue
                try:
                    skills.append(parse_skill(text, path, root.scope))
                except SkillError as exc:
                    warnings.append(f"{path}: skill rejected: {exc}")
        skills.sort(key=lambda skill: (SCOPE_ORDER.index(skill.scope), skill.name, str(skill.path)))
        disabled, problems = disabled_skill_paths(skills, config_entries, home=home)
        warnings.extend(problems)
        return cls(skills, disabled, warnings)

    def enabled(self) -> list[Skill]:
        return [skill for skill in self.skills if skill.path not in self.disabled]
