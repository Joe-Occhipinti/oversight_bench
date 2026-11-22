"""Permissions granted to one session through the `request_permissions` tool.

A grant lasts until the end of the turn in which it was approved, or until
the session ends when it was approved for the session. The session's current
sandbox policy (`orchestrator.sandbox`) is its base policy widened by every
grant it holds:

  write    the granted paths become writable roots; protected paths inside
           them stay read-only
  network  network access for sandboxed commands, to the requested target

Grants belong to the session that asked for them; each subagent has its own
store.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meltzer.history import SessionRecorder
from meltzer.sandbox.policy import NetworkMode, SandboxPolicy

if TYPE_CHECKING:
    from meltzer.approval.orchestrator import ApprovalOrchestrator

MAX_HOST_LENGTH = 253
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class GrantScope(StrEnum):
    TURN = "turn"
    SESSION = "session"


@dataclass(frozen=True)
class NetworkTarget:
    """A single host name or IP address, with an optional port."""

    host: str
    port: int | None = None

    def __str__(self) -> str:
        host = f"[{self.host}]" if ":" in self.host and self.port is not None else self.host
        return host if self.port is None else f"{host}:{self.port}"


def _parse_port(text: str) -> int:
    if not text.isdigit():
        raise ValueError(f"invalid port {text!r}")
    port = int(text)
    if not 1 <= port <= 65535:
        raise ValueError(f"port {port} is out of range (1-65535)")
    return port


def _parse_host(text: str) -> str:
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        pass
    host = text.lower().removesuffix(".")
    if not host or len(host) > MAX_HOST_LENGTH:
        raise ValueError(f"invalid host {text!r}")
    labels = host.split(".")
    if not all(_LABEL.match(label) for label in labels):
        raise ValueError(f"invalid host {text!r}")
    if all(label.isdigit() for label in labels):
        raise ValueError(f"invalid IP address {text!r}")
    return host


def parse_network_target(text: str) -> NetworkTarget:
    """Parse `host`, `host:port`, an IP address, `[IPv6]` or `[IPv6]:port`."""
    if "*" in text:
        raise ValueError("wildcards are not allowed; give a single host or IP address")
    if not text or any(c.isspace() or c in ",;/@" for c in text):
        raise ValueError(f"{text!r} is not a single host or IP address")
    if text.startswith("["):
        address, bracket, rest = text[1:].partition("]")
        if not bracket or (rest and not rest.startswith(":")):
            raise ValueError(f"invalid address {text!r}")
        try:
            host = str(ipaddress.IPv6Address(address))
        except ValueError:
            raise ValueError(f"invalid IPv6 address {address!r}") from None
        return NetworkTarget(host, _parse_port(rest[1:]) if rest else None)
    if text.count(":") > 1:
        try:
            return NetworkTarget(str(ipaddress.IPv6Address(text)))
        except ValueError:
            raise ValueError(f"invalid IPv6 address {text!r}") from None
    host, colon, port = text.partition(":")
    return NetworkTarget(_parse_host(host), _parse_port(port) if colon else None)


@dataclass(frozen=True)
class Grant:
    scope: GrantScope
    network: NetworkTarget | None = None
    # Resolved paths that commands may write to.
    write: tuple[Path, ...] = ()
    reason: str = ""

    def permissions(self) -> dict[str, Any]:
        granted: dict[str, Any] = {}
        if self.network is not None:
            granted["network"] = {"target": str(self.network)}
        if self.write:
            granted["write"] = [str(p) for p in self.write]
        return granted


class GrantStore:
    """The grants of one session, applied to its orchestrator's sandbox policy."""

    def __init__(self, orchestrator: ApprovalOrchestrator, recorder: SessionRecorder | None = None):
        self.orchestrator = orchestrator
        self.recorder = recorder
        # The policy without grants.
        self.base = orchestrator.sandbox
        self._grants: list[Grant] = []

    @property
    def grants(self) -> tuple[Grant, ...]:
        return tuple(self._grants)

    def policy(self) -> SandboxPolicy:
        """The base policy with every grant applied."""
        write = tuple(path for grant in self._grants for path in grant.write)
        network = NetworkMode.FULL if any(grant.network is not None for grant in self._grants) else None
        return self.base.with_grants(write=write, network=network)

    def add(self, grant: Grant) -> None:
        self._grants.append(grant)
        self._apply()
        if self.recorder is not None:
            self.recorder.event(
                "permissions_granted",
                {"scope": grant.scope.value, "permissions": grant.permissions(), "reason": grant.reason},
            )

    def end_turn(self) -> None:
        """Remove the grants that last for the turn."""
        self._grants = [grant for grant in self._grants if grant.scope != GrantScope.TURN]
        self._apply()

    def clear(self) -> None:
        self._grants.clear()
        self._apply()

    def _apply(self) -> None:
        self.orchestrator.sandbox = self.policy()
