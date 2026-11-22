"""Skill mentions: `$name` or `[$name](path)` in a user prompt adds the skill's SKILL.md to the conversation.

A `$name` mention selects the enabled skill with that name when exactly one
has it; a linked mention `[$name](path)` selects the skill whose SKILL.md is
at path. The SKILL.md file is read when the prompt is submitted and added
after the prompt as a `skill` item.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from meltzer.history import Item, Kind, Role
from meltzer.sandbox.policy import resolve
from meltzer.skills.loader import Skill

SIGIL = "$"
SKILL_PATH_PREFIX = "skill://"
SOURCE_PREFIX = "skill:"
# `$NAME` words that are environment variables, not skill mentions.
COMMON_ENV_VARS = frozenset(
    {"PATH", "HOME", "USER", "SHELL", "PWD", "TMPDIR", "TEMP", "TMP", "LANG", "TERM", "XDG_CONFIG_HOME"}
)


def _is_name_char(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char in "_-:")


def _name_end(text: str, start: int) -> int:
    end = start
    while end < len(text) and _is_name_char(text[end]):
        end += 1
    return end


@dataclass(frozen=True)
class Mentions:
    names: frozenset[str]  # from `$name`
    paths: frozenset[str]  # from `[$name](path)`


def _linked_mention(text: str, start: int) -> tuple[str, str, int] | None:
    """`[$name](path)` starting at text[start]: the name, the path and the index after it."""
    if text[start + 1 : start + 2] != SIGIL:
        return None
    name_start = start + 2
    name_end = _name_end(text, name_start)
    if name_end == name_start or text[name_end : name_end + 1] != "]":
        return None
    path_start = name_end + 1
    while path_start < len(text) and text[path_start] in " \t\r\n":
        path_start += 1
    if text[path_start : path_start + 1] != "(":
        return None
    path_end = text.find(")", path_start + 1)
    if path_end == -1:
        return None
    path = text[path_start + 1 : path_end].strip()
    if not path:
        return None
    return text[name_start:name_end], path, path_end + 1


def extract_mentions(text: str) -> Mentions:
    names: set[str] = set()
    paths: set[str] = set()
    index = 0
    while index < len(text):
        char = text[index]
        if char == "[":
            linked = _linked_mention(text, index)
            if linked is not None:
                name, path, index = linked
                if name.upper() not in COMMON_ENV_VARS:
                    paths.add(path)
                continue
        if char != SIGIL:
            index += 1
            continue
        end = _name_end(text, index + 1)
        name = text[index + 1 : end]
        if name and name.upper() not in COMMON_ENV_VARS:
            names.add(name)
        index = max(end, index + 1)
    return Mentions(frozenset(names), frozenset(paths))


def _mentioned_path(text: str) -> Path | None:
    path = Path(text.removeprefix(SKILL_PATH_PREFIX))
    return resolve(path) if path.is_absolute() else None


def select_skills(skills: Sequence[Skill], mentions: Mentions) -> list[Skill]:
    """The mentioned skills among skills, in the order of skills: linked mentions first, then names."""
    paths = {path for path in (_mentioned_path(text) for text in mentions.paths) if path is not None}
    counts = Counter(skill.name for skill in skills)
    selected: list[Skill] = []
    for skill in skills:
        if resolve(skill.path) in paths and skill not in selected:
            selected.append(skill)
    names = {skill.name for skill in selected}
    for skill in skills:
        if skill.name in mentions.names and counts[skill.name] == 1 and skill.name not in names:
            names.add(skill.name)
            selected.append(skill)
    return selected


def format_skill(skill: Skill, contents: str) -> str:
    return f"<skill>\n<name>{skill.name}</name>\n<path>{skill.path}</path>\n{contents}\n</skill>"


def skill_item(skill: Skill) -> Item:
    """The `skill` history item for skill, with its SKILL.md as it is now."""
    contents = skill.path.read_text(encoding="utf-8")
    return Item(Role.USER, Kind.SKILL, content=format_skill(skill, contents), source=SOURCE_PREFIX + skill.name)


def mentioned_skill_items(skills: Sequence[Skill], text: str, notice: Callable[[str], None]) -> list[Item]:
    """Items for the skills that text mentions; a skill that cannot be read is reported with notice."""
    items = []
    for skill in select_skills(skills, extract_mentions(text)):
        try:
            items.append(skill_item(skill))
        except (OSError, UnicodeDecodeError) as exc:
            notice(f"warning: failed to load skill {skill.name} at {skill.path}: {exc}")
    return items
