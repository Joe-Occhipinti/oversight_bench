"""Loads the layered configuration: defaults < user < project < command line.

The user config lives in ~/.meltzer/config.toml, the project config in a
.meltzer/config.toml (see find_project_config).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Iterable

from meltzer.config.schema import KEYS, KEYS_BY_NAME, Config, ConfigValueError
from meltzer.config.trust import (
    PROJECTS_KEY,
    ProjectsTableError,
    ProjectTrust,
    TrustLevel,
    parse_projects,
    project_trust,
)
from meltzer.sandbox.policy import resolve

CONFIG_DIR_NAME = ".meltzer"
CONFIG_FILE_NAME = "config.toml"
DEFAULT_SOURCE = "default"


class ConfigError(Exception):
    """The configuration is invalid; the session does not start."""


def meltzer_home(home: Path | None = None) -> Path:
    return (home if home is not None else Path.home()) / CONFIG_DIR_NAME


def user_config_path(home: Path | None = None) -> Path:
    return meltzer_home(home) / CONFIG_FILE_NAME


def project_search_dirs(cwd: Path, home: Path | None = None) -> list[Path]:
    """Directories searched for project files, nearest first."""
    home_dir = resolve(home if home is not None else Path.home())
    found: list[Path] = []
    for directory in (cwd, *cwd.parents):
        found.append(directory)
        if (directory / ".git").exists() or resolve(directory) == home_dir:
            break
    return found


def find_project_config(cwd: Path, home: Path | None = None) -> Path | None:
    """Return the nearest project config for cwd, or None."""
    user_config = resolve(user_config_path(home))
    for directory in project_search_dirs(cwd, home):
        candidate = directory / CONFIG_DIR_NAME / CONFIG_FILE_NAME
        # The user config is already its own layer; do not load it twice.
        if candidate.is_file() and resolve(candidate) != user_config:
            return candidate
    return None


def parse_override(text: str) -> tuple[str, Any]:
    """Parse a `-c key=value` override. The value is read as TOML, falling back to a plain string."""
    key, sep, raw = text.partition("=")
    key = key.strip()
    if not sep or not key:
        raise ConfigError(f"invalid override {text!r}: expected key=value")
    raw = raw.strip()
    try:
        value = tomllib.loads(f"value = {raw}")["value"]
    except tomllib.TOMLDecodeError:
        value = raw
    return key, value


def _flatten(table: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for name, value in table.items():
        dotted = f"{prefix}{name}"
        if isinstance(value, dict) and dotted not in KEYS_BY_NAME:
            flat.update(_flatten(value, f"{dotted}."))
        else:
            flat[dotted] = value
    return flat


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read file: {exc.strerror}") from exc


class _Builder:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {key.name: key.default for key in KEYS}
        self.sources: dict[str, str] = {key.name: DEFAULT_SOURCE for key in KEYS}
        self.warnings: list[str] = []

    def apply(self, entries: Iterable[tuple[str, Any]], origin: str) -> None:
        for name, raw in entries:
            key = KEYS_BY_NAME.get(name)
            if key is None:
                raise ConfigError(f"{origin}: unknown key '{name}'")
            try:
                value = key.parse(raw)
            except ConfigValueError as exc:
                raise ConfigError(f"{origin}: invalid value for '{name}': {exc}") from None
            if key.merge:
                self._merge(name, value, origin)
                continue
            self.values[name] = value
            self.sources[name] = origin

    def _merge(self, name: str, value: tuple[Any, ...], origin: str) -> None:
        """Append the entries of a later layer; entries already present are not added twice."""
        added = tuple(entry for entry in dict.fromkeys(value) if entry not in self.values[name])
        if not added:
            return
        self.values[name] = (*self.values[name], *added)
        previous = self.sources[name]
        self.sources[name] = origin if previous == DEFAULT_SOURCE else f"{previous}, {origin}"

    def build(self, trust: ProjectTrust) -> Config:
        kwargs = {key.attr: self.values[key.name] for key in KEYS}
        return Config(
            **kwargs,
            sources=dict(self.sources),
            warnings=tuple(self.warnings),
            project_trust=trust,
        )


def _project_levels(data: dict[str, Any], path: Path) -> tuple[dict[Path, TrustLevel], list[str]]:
    """Trust levels from the [projects] table of the user config, removed from data."""
    if PROJECTS_KEY not in data:
        return {}, []
    try:
        levels, warnings = parse_projects(data.pop(PROJECTS_KEY))
    except ProjectsTableError as exc:
        raise ConfigError(f"{path}: {exc}") from None
    return levels, [f"{path}: {warning}" for warning in warnings]


def read_project_trust(cwd: Path, home: Path | None = None) -> ProjectTrust:
    """The trust decision the user config records for a session directory."""
    user_path = user_config_path(home)
    levels: dict[Path, TrustLevel] = {}
    if user_path.is_file():
        levels, _ = _project_levels(_read_toml(user_path), user_path)
    return project_trust(cwd, levels)


def load_config(
    cwd: Path,
    *,
    home: Path | None = None,
    overrides: Iterable[tuple[str, Any]] = (),
    unrecorded_trust: TrustLevel | None = None,
) -> Config:
    """Build the effective config for a session rooted at cwd.

    `unrecorded_trust` applies when the user config records no trust decision
    for cwd, e.g. an answer from this session that could not be saved.
    """
    builder = _Builder()

    user_path = user_config_path(home)
    levels: dict[Path, TrustLevel] = {}
    if user_path.is_file():
        data = _read_toml(user_path)
        levels, warnings = _project_levels(data, user_path)
        builder.warnings.extend(warnings)
        builder.apply(_flatten(data).items(), str(user_path))

    trust = project_trust(cwd, levels)
    if not trust.recorded and unrecorded_trust is not None:
        trust = ProjectTrust(root=trust.root, level=unrecorded_trust)

    project_path = find_project_config(cwd, home)
    if project_path is not None and trust.trusted:
        data = _read_toml(project_path)
        if PROJECTS_KEY in data:
            del data[PROJECTS_KEY]
            builder.warnings.append(f"{project_path}: '{PROJECTS_KEY}' can only be set in user config; ignored")
        builder.apply(_flatten(data).items(), str(project_path))

    builder.apply(overrides, "command line")
    return builder.build(trust)
