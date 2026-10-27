"""Project trust: whether a project's .meltzer directory configures sessions.

Decisions are stored in the user config, one table per directory:

    [projects."/home/me/src/app"]
    trust_level = "trusted"        # or "untrusted"
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from meltzer.tomlwrite import toml_string

PROJECTS_KEY = "projects"
TRUST_LEVEL_KEY = "trust_level"

TRUST_LEVEL_LINE = re.compile(rf"^\s*{TRUST_LEVEL_KEY}\s*=")


class TrustLevel(StrEnum):
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


class ProjectsTableError(ValueError):
    """The [projects] table of the user config is invalid or cannot be updated."""


def normalize_path(path: str | Path) -> Path:
    return Path(os.path.realpath(path))


def parse_projects(value: Any) -> tuple[dict[Path, TrustLevel], list[str]]:
    """Trust levels by directory from the [projects] table. Returns (levels, warnings)."""
    if not isinstance(value, dict):
        raise ProjectsTableError(f"'{PROJECTS_KEY}' must be a table of project tables")
    levels: dict[Path, TrustLevel] = {}
    warnings: list[str] = []
    for key, entry in value.items():
        where = f"{PROJECTS_KEY}.{toml_string(key)}"
        if not isinstance(entry, dict):
            raise ProjectsTableError(f"'{where}' must be a table")
        unknown = set(entry) - {TRUST_LEVEL_KEY}
        if unknown:
            raise ProjectsTableError(f"'{where}': unknown key(s): {', '.join(sorted(unknown))}")
        if TRUST_LEVEL_KEY not in entry:
            continue
        raw = entry[TRUST_LEVEL_KEY]
        try:
            level = TrustLevel(raw)
        except ValueError:
            raise ProjectsTableError(
                f"'{where}.{TRUST_LEVEL_KEY}': expected \"trusted\" or \"untrusted\", got {raw!r}"
            ) from None
        if not os.path.isabs(key):
            warnings.append(f"'{where}': not an absolute path; ignored")
            continue
        levels[normalize_path(key)] = level
    return levels, warnings


def find_git_root(directory: Path) -> Path | None:
    """The nearest of directory and its parents that contains .git."""
    for candidate in (directory, *directory.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


@dataclass(frozen=True)
class ProjectTrust:
    """The trust decision that applies to a session directory."""

    # Directory a new decision for this session is recorded for.
    root: Path
    level: TrustLevel | None
    # The [projects] entry the level comes from, if it is recorded.
    entry: Path | None = None

    @property
    def trusted(self) -> bool:
        return self.level == TrustLevel.TRUSTED

    @property
    def recorded(self) -> bool:
        return self.entry is not None


def project_trust(cwd: Path, levels: dict[Path, TrustLevel]) -> ProjectTrust:
    cwd = normalize_path(cwd)
    root = find_git_root(cwd) or cwd
    for directory in (cwd, *cwd.parents):
        level = levels.get(directory)
        if level is not None:
            return ProjectTrust(root=root, level=level, entry=directory)
    return ProjectTrust(root=root, level=None)


def untrusted_notice(trust: ProjectTrust, user_config: Path) -> str:
    if trust.recorded and trust.level == TrustLevel.UNTRUSTED:
        return (
            f"{trust.entry} is marked as untrusted in {user_config}; "
            "project config and rules in .meltzer are not loaded"
        )
    return (
        f"{trust.root} is not a trusted project; project config and rules in .meltzer are not loaded. "
        f"To load them, set {TRUST_LEVEL_KEY} = \"trusted\" under "
        f"[{PROJECTS_KEY}.{toml_string(str(trust.root))}] in {user_config}"
    )


def _table_path(line: str) -> tuple[str, ...] | None:
    """The key path of a table header line such as `[projects."/src/app"]`, else None."""
    stripped = line.strip()
    if not stripped.startswith("["):
        return None
    try:
        data: Any = tomllib.loads(stripped)
    except tomllib.TOMLDecodeError:
        return None
    path: list[str] = []
    while isinstance(data, dict) and len(data) == 1:
        (name, data), = data.items()
        path.append(name)
    return tuple(path) if path else None


def set_trust_level(text: str, project: str, level: TrustLevel) -> str:
    """Return `text` with trust_level set in the table of `project`; other lines are kept."""
    assignment = f"{TRUST_LEVEL_KEY} = {toml_string(level.value)}\n"
    lines = text.splitlines(keepends=True)
    header = next((n for n, line in enumerate(lines) if _table_path(line) == (PROJECTS_KEY, project)), None)
    if header is None:
        prefix = text
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        if prefix:
            prefix += "\n"
        return prefix + f"[{PROJECTS_KEY}.{toml_string(project)}]\n" + assignment
    end = next(
        (n for n in range(header + 1, len(lines)) if _table_path(lines[n]) is not None),
        len(lines),
    )
    for n in range(header + 1, end):
        if TRUST_LEVEL_LINE.match(lines[n]):
            lines[n] = assignment
            break
    else:
        if not lines[header].endswith("\n"):
            lines[header] += "\n"
        lines.insert(header + 1, assignment)
    return "".join(lines)


def record_project_trust(config_path: Path, project: Path, level: TrustLevel) -> None:
    """Save a trust decision for `project` in the user config."""
    text = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    key = str(project)
    updated = set_trust_level(text, key, level)
    try:
        saved = tomllib.loads(updated).get(PROJECTS_KEY, {}).get(key, {}).get(TRUST_LEVEL_KEY)
    except (tomllib.TOMLDecodeError, AttributeError) as exc:
        raise ProjectsTableError(f"cannot add {key} to {config_path}; add it by hand: {exc}") from None
    if saved != level.value:
        raise ProjectsTableError(f"cannot add {key} to {config_path}; add it by hand")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_name(config_path.name + ".tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.replace(temporary, config_path)
