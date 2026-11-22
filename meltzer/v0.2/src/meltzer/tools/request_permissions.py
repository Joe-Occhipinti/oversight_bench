"""The `request_permissions` tool: ask for more sandbox permissions for later commands.

The model asks for network access to one host, write access to more paths,
or both, with a reason. The request goes to the reviewer; an approved request
becomes a grant of the session (see meltzer.sandbox.grants), and the commands
and patches that follow run with the widened sandbox policy. The result lists
the granted permissions and their scope:

    {"permissions": {"network": {"target": "pypi.org:443"}, "write": ["/abs/path"]}, "scope": "turn"}

A request that is not granted gives an empty grant: {"permissions": {}, "scope": "turn"}.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from meltzer.approval.orchestrator import ApprovalOrchestrator, PermissionsRequest
from meltzer.sandbox.bwrap import RUNTIME_DIRS
from meltzer.sandbox.grants import Grant, GrantScope, GrantStore, NetworkTarget, parse_network_target
from meltzer.sandbox.policy import PROTECTED_NAMES, SANDBOX_TEMP_DIR, is_within, resolve
from meltzer.tools.registry import Tool, ToolError, ToolSpec, require_string

REQUEST_PERMISSIONS_SPEC = ToolSpec(
    name="request_permissions",
    description=(
        "Ask for more sandbox permissions for the commands and patches that follow: network access "
        "to one host, write access to more paths, or both. Give a one-sentence `reason`. If approved, "
        "the permissions apply to the rest of the turn (or the session, if approved for the session) "
        "without asking again. The result lists the granted permissions; it is empty when the request "
        "is not granted."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "Why the permissions are needed."},
            "network": {
                "type": "object",
                "description": "Network access to one host.",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": (
                            "A host name or IP address, with an optional port, for example "
                            "pypi.org or 10.0.0.5:8080. No wildcards."
                        ),
                    },
                },
                "required": ["target"],
                "additionalProperties": False,
            },
            "write": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Existing files or directories to make writable, absolute or relative to the workspace "
                    "root. Protected paths (.git, .meltzer, ~/.meltzer) and directories that contain "
                    ".git or .meltzer cannot be requested."
                ),
            },
        },
        "required": ["reason"],
        "additionalProperties": False,
    },
)


MAX_SCAN_ENTRIES = 200_000
# Directories the sandbox mounts itself.
SYSTEM_DIRS = (*RUNTIME_DIRS, Path("/dev"), Path("/proc"), Path("/sys"))


def find_protected_entry(directory: Path) -> Path | None:
    """A `.git` or `.meltzer` entry anywhere below directory, or None.

    Raises ToolError when a directory cannot be read or has too many entries to check.
    """

    def unreadable(exc: OSError) -> None:
        raise ToolError(f"write access cannot be granted: cannot read {exc.filename}")

    seen = 0
    for current, dirs, files in os.walk(directory, onerror=unreadable, followlinks=False):
        for name in (*dirs, *files):
            if name in PROTECTED_NAMES:
                return Path(current, name)
        seen += len(dirs) + len(files)
        if seen > MAX_SCAN_ENTRIES:
            raise ToolError(f"write access cannot be granted: {directory} has too many entries; request a narrower path")
    return None


class RequestPermissionsTool(Tool):
    spec = REQUEST_PERMISSIONS_SPEC

    def __init__(self, root: Path, orchestrator: ApprovalOrchestrator, grants: GrantStore):
        self.root = root
        self.orchestrator = orchestrator
        self.grants = grants

    def _network(self, value: Any) -> NetworkTarget | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ToolError("'network' must be an object with a 'target'")
        unknown = sorted(set(value) - {"target"})
        if unknown:
            raise ToolError(f"'network' has an unknown key: {', '.join(unknown)}")
        target = value.get("target")
        if not isinstance(target, str):
            raise ToolError("'target' must be a host name or IP address, with an optional port")
        try:
            return parse_network_target(target.strip())
        except ValueError as exc:
            raise ToolError(f"invalid network target: {exc}") from exc

    def _write_paths(self, value: Any) -> tuple[Path, ...]:
        if value is None:
            return ()
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise ToolError("'write' must be a list of non-empty paths")
        paths: list[Path] = []
        for text in value:
            path = Path(text)
            if not path.is_absolute():
                path = self.root / path
            resolved = resolve(path)
            if not resolved.exists():
                raise ToolError(f"write path does not exist: {path}")
            if resolved not in paths:
                paths.append(resolved)
        self._check_protected(tuple(paths))
        return tuple(paths)

    def _check_protected(self, paths: tuple[Path, ...]) -> None:
        protected = self.orchestrator.sandbox.with_grants(write=paths).protected_paths()
        for path in paths:
            inside = any(name in PROTECTED_NAMES for name in path.parts) or any(
                is_within(path, p) or is_within(path, resolve(p)) for p in protected
            )
            if inside:
                raise ToolError(f"write access cannot be granted: {path} is a protected path or inside one")
            if is_within(resolve(SANDBOX_TEMP_DIR), path) or any(
                is_within(path, resolve(system)) or is_within(resolve(system), path) for system in SYSTEM_DIRS
            ):
                raise ToolError(f"write access cannot be granted: {path} is a system directory or contains one")
            if path.is_dir():
                nested = find_protected_entry(path)
                if nested is not None:
                    raise ToolError(f"write access cannot be granted: {path} contains the protected path {nested}")

    def run(self, arguments: dict[str, Any]) -> str:
        reason = require_string(arguments, "reason").strip()
        network = self._network(arguments.get("network"))
        write = self._write_paths(arguments.get("write"))
        if network is None and not write:
            raise ToolError("request at least one permission: 'network' or 'write'")

        request = PermissionsRequest(
            reason=reason,
            cwd=self.root,
            network_target=str(network) if network is not None else None,
            write_paths=write,
        )
        decision = self.orchestrator.decide_permissions(request)
        if not decision.run:
            return json.dumps({"permissions": {}, "scope": GrantScope.TURN.value})
        scope = GrantScope.SESSION if decision.for_session else GrantScope.TURN
        grant = Grant(scope=scope, network=network, write=write, reason=reason)
        self.grants.add(grant)
        return json.dumps({"permissions": grant.permissions(), "scope": grant.scope.value})
