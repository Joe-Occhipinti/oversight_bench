"""Agent roles: named presets that `spawn_agent` can start a subagent with.

Roles are read from ~/.meltzer/agents/*.toml and, in a trusted project, from
the project's nearest .meltzer/agents/ directory. A role file sets:

    name = "reviewer"                      # default: the file name without .toml
    description = "Reviews a diff for bugs."
    developer_instructions = "Read the diff and list the bugs you find."
    model = "openai/gpt-5-mini"            # optional

Other keys are ignored with a warning. Project roles replace user roles of the
same name, and both replace the built-in roles `default`, `explorer` and
`worker`. A role sets the subagent's model and adds its instructions to the
subagent's system message; everything else comes from the parent session.
"""

from __future__ import annotations

import dataclasses
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meltzer.config.loader import CONFIG_DIR_NAME, project_search_dirs
from meltzer.config.schema import Config
from meltzer.sandbox.policy import resolve

AGENTS_DIR_NAME = "agents"
ROLE_SUFFIX = ".toml"
ROLE_KEYS = ("name", "description", "developer_instructions", "model")
DEFAULT_ROLE = "default"
BUILT_IN_SOURCE = "built-in"

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class AgentRole:
    name: str
    description: str = ""
    developer_instructions: str = ""
    model: str | None = None
    # "built-in", or the file the role was read from.
    source: str = BUILT_IN_SOURCE


BUILT_IN_ROLES = (
    AgentRole(DEFAULT_ROLE, description="Default agent."),
    AgentRole(
        "explorer",
        description=(
            "Use for specific, well-scoped questions about the codebase. Explorers answer "
            "quickly; start several in parallel for independent questions, and reuse an "
            "explorer for related ones."
        ),
    ),
    AgentRole(
        "worker",
        description=(
            "Use for execution work: implementing part of a feature, fixing tests or bugs. "
            "Tell the worker which files it is responsible for, and that other agents may be "
            "editing the workspace at the same time."
        ),
    ),
)


class RoleError(ValueError):
    """A role file is invalid; the role is not loaded."""


def _optional_text(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RoleError(f"'{key}' must be a string")
    return value


def parse_role(text: str, path: Path) -> tuple[AgentRole, list[str]]:
    """Parse one role file. Returns the role and warnings about ignored keys."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RoleError(f"invalid TOML: {exc}") from None
    warnings = [
        f"{path}: unknown key '{key}' in agent role; ignored" for key in data if key not in ROLE_KEYS
    ]
    name = _optional_text(data, "name")
    if name is None:
        name = path.stem
    if not _NAME.match(name):
        raise RoleError(f"invalid role name {name!r}")
    instructions = _optional_text(data, "developer_instructions")
    if instructions is None or not instructions.strip():
        raise RoleError("'developer_instructions' must be a non-empty string")
    model = _optional_text(data, "model")
    if model is not None and not model.strip():
        raise RoleError("'model' must be a non-empty string")
    role = AgentRole(
        name=name,
        description=(_optional_text(data, "description") or "").strip(),
        developer_instructions=instructions,
        model=model,
        source=str(path),
    )
    return role, warnings


def _role_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.suffix == ROLE_SUFFIX and p.is_file())


def find_project_agents_dir(cwd: Path, user_dir: Path, home: Path | None = None) -> Path | None:
    """The project agents directory for cwd."""
    user = resolve(user_dir)
    for directory in project_search_dirs(cwd, home):
        candidate = directory / CONFIG_DIR_NAME / AGENTS_DIR_NAME
        if candidate.is_dir() and resolve(candidate) != user:
            return candidate
    return None


class RoleSet:
    def __init__(self, roles: list[AgentRole], warnings: list[str] | None = None):
        self.roles: dict[str, AgentRole] = {}
        for role in roles:
            self.roles[role.name] = role
        self.warnings = list(warnings or [])

    @classmethod
    def load(
        cls,
        user_dir: Path,
        cwd: Path,
        *,
        include_project: bool = False,
        home: Path | None = None,
    ) -> RoleSet:
        """Built-in roles, user roles, and the nearest project roles when include_project is set."""
        directories = [user_dir]
        project_dir = find_project_agents_dir(cwd, user_dir, home) if include_project else None
        if project_dir is not None:
            directories.append(project_dir)
        roles = list(BUILT_IN_ROLES)
        warnings: list[str] = []
        for directory in directories:
            try:
                paths = _role_files(directory)
            except OSError as exc:
                warnings.append(f"{directory}: cannot read agents directory: {exc.strerror}")
                continue
            for path in paths:
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    warnings.append(f"{path}: cannot read agent role: {exc}")
                    continue
                try:
                    role, problems = parse_role(text, path)
                except RoleError as exc:
                    warnings.append(f"{path}: agent role rejected: {exc}")
                    continue
                roles.append(role)
                warnings.extend(problems)
        return cls(roles, warnings)

    def get(self, name: str) -> AgentRole | None:
        return self.roles.get(name)

    def names(self) -> list[str]:
        return list(self.roles)

    def describe(self) -> str:
        """The list of roles shown to the model in the `spawn_agent` description."""
        lines = []
        for role in self.roles.values():
            lines.append(f"- {role.name}: {role.description}" if role.description else f"- {role.name}")
        return "Available roles:\n" + "\n".join(lines)


def role_config(config: Config, role: AgentRole) -> Config:
    """The configuration of a subagent started with `role` by a session with `config`.

    Only the model changes: the role's model, else `agents.default_model`, else
    the parent's. The approval review keeps the parent's review model.
    """
    model = role.model or config.agents_default_model or config.model
    if model == config.model:
        return config
    source = f"agent role {role.name}" if role.model else config.sources.get("agents.default_model", "")
    return dataclasses.replace(
        config,
        model=model,
        monitor_model=config.monitor_model or config.model,
        sources={**config.sources, "model": source},
    )
