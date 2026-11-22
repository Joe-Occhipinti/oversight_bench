"""Builds the macOS Seatbelt (sandbox-exec) policy for a sandboxed command.

The policy, in order:
  base policy                 deny by default; processes, /dev/null, ptys, system lookups
                              (seatbelt/base_policy.sbpl)
  (allow file-read*)          reads everywhere, or everywhere except the denied paths
  (allow file-write* ...)     each writable root, minus its protected and read-only
                              paths (the path itself and everything below it)
  network                     none or proxy: nothing (network mode proxy needs the
                              Linux sandbox); full: all traffic

Paths never appear in the policy text: each one is a parameter (-DKEY=path)
that the policy refers to as (param "KEY").
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from meltzer.sandbox.bwrap import SandboxSetupError
from meltzer.sandbox.policy import PROTECTED_NAMES, NetworkMode, SandboxPolicy, is_within

# Only this sandbox-exec is used, never one found on PATH.
SEATBELT_EXECUTABLE = "/usr/bin/sandbox-exec"


def _policy_file(name: str) -> str:
    return resources.files("meltzer.sandbox").joinpath("seatbelt", name).read_text(encoding="utf-8")


def _access_rule(action: str, prefix: str, roots: list[tuple[str, list[Path]]], params: dict[str, str]) -> str:
    """(allow <action> ...) for each root minus its excluded paths; adds the paths to params."""
    parts = []
    for index, (root, excluded) in enumerate(roots):
        key = f"{prefix}_{index}"
        params[key] = root
        require = [f'(subpath (param "{key}"))']
        for number, path in enumerate(excluded):
            excluded_key = f"{key}_EXCLUDED_{number}"
            params[excluded_key] = str(path)
            # The path itself too, so it cannot be created when it does not exist.
            require.append(f'(require-not (literal (param "{excluded_key}")))')
            require.append(f'(require-not (subpath (param "{excluded_key}")))')
        parts.append(require[0] if len(require) == 1 else f"(require-all {' '.join(require)})")
    if not parts:
        return ""
    return f"(allow {action}\n  " + "\n  ".join(parts) + ")"


def _check_paths(policy: SandboxPolicy) -> None:
    for root in policy.writable_roots():
        if root != policy.root and root.is_symlink():
            raise SandboxSetupError(f"writable path {root} is a symlink")
    for path in (*policy.read_only_subpaths(), *policy.deny_read):
        if path.is_symlink():
            raise SandboxSetupError(f"protected path {path} is a symlink")


def build_seatbelt_policy(policy: SandboxPolicy, cwd: Path) -> tuple[str, dict[str, str]]:
    """Return (policy text, parameters)."""
    if not policy.is_sandboxed:
        raise SandboxSetupError(f"sandbox mode {policy.mode} does not use a sandbox")
    if not policy.can_read(cwd):
        raise SandboxSetupError(f"working directory {cwd} is not readable in the sandbox")
    _check_paths(policy)
    params: dict[str, str] = {}

    if policy.deny_read:
        denied = sorted(policy.deny_read, key=lambda p: len(p.parts))
        read_rule = _access_rule("file-read*", "READABLE_ROOT", [("/", denied)], params)
    else:
        read_rule = "(allow file-read*)"

    read_only = policy.read_only_subpaths()
    writable = []
    for root in policy.writable_roots():
        excluded = [p for p in read_only if is_within(p, root) or is_within(root, p)]
        excluded += [root / name for name in PROTECTED_NAMES if root / name not in excluded]
        writable.append((str(root), excluded))
    write_rule = _access_rule("file-write*", "WRITABLE_ROOT", writable, params)

    network_rule = ""
    if policy.network == NetworkMode.FULL:
        network_rule = "(allow network-outbound)\n(allow network-inbound)\n" + _policy_file("network_policy.sbpl")

    sections = [_policy_file("base_policy.sbpl"), read_rule, write_rule, network_rule]
    return "\n".join(section for section in sections if section), params


def seatbelt_command(policy: SandboxPolicy, command: list[str], cwd: Path) -> list[str]:
    """The sandbox-exec argv that runs `command` under the policy."""
    text, params = build_seatbelt_policy(policy, cwd)
    return [SEATBELT_EXECUTABLE, "-p", text, *(f"-D{key}={value}" for key, value in params.items()), "--", *command]
