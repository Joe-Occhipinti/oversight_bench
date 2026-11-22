"""Permission profiles: named sandbox settings from [permissions.<name>].

  default_permissions = "dev"

  [permissions.dev]
  description = "Workspace, the shared library and the pip cache"
  extends = ":workspace"

  [permissions.dev.workspace_roots]
  "~/code/shared-lib" = true

  [permissions.dev.filesystem]
  "~/.cache/pip" = "write"
  "docs" = "read"
  "~/.aws" = "deny"

  [permissions.dev.network]
  enabled = true

Built-in profiles, equal to the three sandbox modes:
  :read-only    read-only
  :workspace    workspace-write
  :full-access  full-access

A profile starts from the profile it extends (`:read-only` when it has no
`extends`) and applies its own entries on top. Paths are absolute, relative to
the session root, or start with `~/`. Access:
  read   read-only, also inside a writable root
  write  writable; the protected names (.git, .meltzer) inside it stay read-only
  deny   neither readable nor writable
Workspace roots are writable directories with the same protection as the
session root; they apply to profiles based on `:workspace`. Network on means
access through meltzer's network proxy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

from meltzer.config.loader import ConfigError
from meltzer.config.schema import Config
from meltzer.sandbox.policy import (
    SANDBOX_TEMP_DIR,
    NetworkMode,
    SandboxMode,
    SandboxPolicy,
    is_within,
    resolve,
)

BUILTIN_PROFILES: dict[str, SandboxMode] = {
    ":read-only": SandboxMode.READ_ONLY,
    ":workspace": SandboxMode.WORKSPACE_WRITE,
    ":full-access": SandboxMode.FULL_ACCESS,
}
PROFILE_KEYS = ("description", "extends", "workspace_roots", "filesystem", "network")
NETWORK_KEYS = ("enabled",)
# Source recorded by the config loader for values given on the command line.
COMMAND_LINE_SOURCE = "command line"


class Access(StrEnum):
    READ = "read"
    WRITE = "write"
    DENY = "deny"


@dataclass(frozen=True)
class ProfileSpec:
    """One [permissions.<name>] table as written in the config."""

    name: str
    description: str = ""
    extends: str | None = None
    workspace_roots: dict[str, bool] = field(default_factory=dict)
    filesystem: dict[str, Access] = field(default_factory=dict)
    network: bool | None = None


@dataclass(frozen=True)
class Profile:
    """A profile with the profiles it extends applied."""

    name: str
    mode: SandboxMode
    description: str = ""
    workspace_roots: dict[str, bool] = field(default_factory=dict)
    filesystem: dict[str, Access] = field(default_factory=dict)
    # None: the network of the sandbox mode.
    network: bool | None = None


def _bool(where: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: expected true or false, got {value!r}")
    return value


def _table(where: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a table, got {value!r}")
    return value


def parse_profile(name: str, entry: Any) -> ProfileSpec:
    where = f"permissions.{name}"
    if name in BUILTIN_PROFILES or name.startswith(":"):
        raise ConfigError(f"{where}: names starting with ':' are reserved for built-in profiles")
    table = _table(where, entry)
    unknown = sorted(set(table) - set(PROFILE_KEYS))
    if unknown:
        raise ConfigError(f"{where}: unknown key {unknown[0]!r}")
    description = table.get("description", "")
    if not isinstance(description, str):
        raise ConfigError(f"{where}.description: expected a string, got {description!r}")
    extends = table.get("extends")
    if extends is not None and (not isinstance(extends, str) or not extends):
        raise ConfigError(f"{where}.extends: expected a profile name, got {extends!r}")
    roots = {
        path: _bool(f"{where}.workspace_roots.{path}", enabled)
        for path, enabled in _table(f"{where}.workspace_roots", table.get("workspace_roots", {})).items()
    }
    filesystem: dict[str, Access] = {}
    for path, access in _table(f"{where}.filesystem", table.get("filesystem", {})).items():
        try:
            filesystem[path] = Access(access)
        except (ValueError, TypeError):
            allowed = ", ".join(member.value for member in Access)
            raise ConfigError(f"{where}.filesystem.{path}: expected one of: {allowed}; got {access!r}") from None
    network = None
    if "network" in table:
        settings = _table(f"{where}.network", table["network"])
        unknown = sorted(set(settings) - set(NETWORK_KEYS))
        if unknown:
            raise ConfigError(f"{where}.network: unknown key {unknown[0]!r}")
        if "enabled" in settings:
            network = _bool(f"{where}.network.enabled", settings["enabled"])
    return ProfileSpec(
        name=name,
        description=description,
        extends=extends,
        workspace_roots=roots,
        filesystem=filesystem,
        network=network,
    )


def load_profiles(permissions: dict[str, Any]) -> dict[str, ProfileSpec]:
    """Parse the `permissions` table of the config."""
    return {name: parse_profile(name, entry) for name, entry in permissions.items()}


def resolve_profile(specs: dict[str, ProfileSpec], name: str) -> Profile:
    """Follow `extends` from name to a built-in profile; the parent applies first."""
    if name in BUILTIN_PROFILES:
        return Profile(name=name, mode=BUILTIN_PROFILES[name])
    parents: list[ProfileSpec] = []
    current: str = name
    while current not in BUILTIN_PROFILES:
        spec = specs.get(current)
        if spec is None:
            if current == name:
                raise ConfigError(f"default_permissions: no profile named {name!r} in [permissions]")
            raise ConfigError(f"permissions.{parents[-1].name}.extends: no profile named {current!r}")
        if spec in parents:
            names = " -> ".join([*(s.name for s in parents), current])
            raise ConfigError(f"permissions.{name}: extends forms a cycle ({names})")
        parents.append(spec)
        current = spec.extends if spec.extends is not None else ":read-only"
    profile = Profile(name=name, mode=BUILTIN_PROFILES[current])
    for spec in reversed(parents):
        profile = Profile(
            name=name,
            mode=profile.mode,
            description=spec.description or profile.description,
            workspace_roots={**profile.workspace_roots, **spec.workspace_roots},
            filesystem={**profile.filesystem, **spec.filesystem},
            network=spec.network if spec.network is not None else profile.network,
        )
    return profile


def _entry_path(where: str, text: str, root: Path, home: Path | None) -> Path:
    if text.startswith(":"):
        raise ConfigError(f"{where}: unknown special path {text!r}")
    if text == "~" or text.startswith("~/"):
        base = home if home is not None else Path.home()
        return resolve(base / text[2:])
    if text.startswith("~"):
        raise ConfigError(f"{where}: {text!r}: only '~' and '~/...' are expanded")
    path = Path(text)
    return resolve(path if path.is_absolute() else root / path)


def compile_profile(
    profile: Profile,
    root: Path,
    *,
    meltzer_home: Path | None = None,
    temp_dir: Path | None = None,
    home: Path | None = None,
) -> SandboxPolicy:
    """The sandbox policy of a profile for a session rooted at root."""
    where = f"permissions.{profile.name}"
    if profile.mode == SandboxMode.FULL_ACCESS:
        if profile.filesystem or profile.workspace_roots or profile.network is not None:
            raise ConfigError(
                f"{where}: a profile based on :full-access runs commands without a sandbox "
                "and cannot set filesystem, workspace_roots or network"
            )
        return SandboxPolicy.for_session(profile.mode, root, meltzer_home=meltzer_home, temp_dir=temp_dir)

    root = resolve(root)
    entries: dict[Path, tuple[str, Access]] = {}
    for text, access in profile.filesystem.items():
        entries[_entry_path(f"{where}.filesystem", text, root, home)] = (text, access)
    writable = [path for path, (_, access) in entries.items() if access == Access.WRITE]
    if profile.mode == SandboxMode.WORKSPACE_WRITE:
        for text, enabled in profile.workspace_roots.items():
            if enabled:
                path = _entry_path(f"{where}.workspace_roots", text, root, home)
                if path not in writable:
                    writable.append(path)
    read_only = [path for path, (_, access) in entries.items() if access == Access.READ]
    denied = [path for path, (_, access) in entries.items() if access == Access.DENY]
    network = None if profile.network is None else (NetworkMode.PROXY if profile.network else NetworkMode.NONE)
    policy = SandboxPolicy.for_session(
        profile.mode,
        root,
        meltzer_home=meltzer_home,
        temp_dir=temp_dir,
        extra_writable=tuple(writable),
        read_only_extra=tuple(read_only),
        network=network,
        deny_read=tuple(denied),
    )

    # An entry cannot contain a path that has more access than the entry gives.
    def name(path: Path) -> str:
        text, access = entries[path]
        return f"{where}.filesystem: {text!r} = {access.value!r}"

    temp_paths = [SANDBOX_TEMP_DIR, *([policy.temp_dir] if policy.temp_dir is not None else [])]
    for path in policy.read_only_extra:
        for inner in policy.writable_roots():
            if is_within(inner, path):
                label = "workspace root" if inner == policy.root else "writable path"
                raise ConfigError(f"{name(path)} contains the {label} {inner}")
    for path in policy.deny_read:
        if is_within(policy.root, path):
            raise ConfigError(f"{name(path)} contains the workspace root {policy.root}")
        for inner in policy.writable_roots():
            if is_within(inner, path):
                raise ConfigError(f"{name(path)} contains the writable path {inner}")
        for inner in policy.read_only_extra:
            if is_within(inner, path):
                raise ConfigError(f"{name(path)} contains the read-only path {inner}")
        for inner in temp_paths:
            if is_within(inner, path):
                raise ConfigError(f"{name(path)} contains the temporary directory {inner}")
    return policy


def selected_profile(config: Config) -> Profile:
    """The profile a session runs with.

    `default_permissions` selects a profile; without it, or when `sandbox_mode`
    is given on the command line, the built-in profile of `sandbox_mode` applies,
    with network access through the proxy in workspace-write mode when
    `network.enabled` is true.
    """
    name = config.default_permissions
    if name is None or config.sources.get("sandbox_mode") == COMMAND_LINE_SOURCE:
        mode_names = {mode: builtin for builtin, mode in BUILTIN_PROFILES.items()}
        network = True if config.network_enabled and config.sandbox_mode == SandboxMode.WORKSPACE_WRITE else None
        return Profile(name=mode_names[config.sandbox_mode], mode=config.sandbox_mode, network=network)
    return resolve_profile(load_profiles(config.permissions), name)


def policy_from_config(
    config: Config,
    root: Path,
    *,
    meltzer_home: Path | None = None,
    temp_dir: Callable[[], Path] | None = None,
    home: Path | None = None,
) -> SandboxPolicy:
    """The sandbox policy of a session. `temp_dir` creates the session's
    temporary directory; it is called only when commands run in a sandbox, and
    the directory is removed again if the profile is invalid."""
    profile = selected_profile(config)
    directory = temp_dir() if temp_dir is not None and profile.mode != SandboxMode.FULL_ACCESS else None
    try:
        return compile_profile(profile, root, meltzer_home=meltzer_home, temp_dir=directory, home=home)
    except ConfigError:
        if directory is not None:
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
