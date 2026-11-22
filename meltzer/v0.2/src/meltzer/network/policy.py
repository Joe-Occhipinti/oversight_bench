"""Which hosts sandboxed commands may reach through the network proxy.

  [network]
  enabled = true
  allowed_domains = ["pypi.org", "*.pythonhosted.org", "10.0.0.5"]
  denied_domains = ["upload.pypi.org"]
  allow_local_binding = false

Entries are exact host names, `*.domain` (any subdomain of domain, not domain
itself) or IP addresses. A request is decided in this order:
  1. a host on the denied list is refused;
  2. an IP address that is loopback, private, link-local or multicast is
     refused unless it is on the allow list (allow_local_binding allows
     loopback addresses);
  3. a host on the allow list is allowed;
  4. any other host goes to the session's reviewer (orchestrator.decide_network).
     An approval the user gives for the session holds for the rest of the
     session, for the same host, port and protocol.
After a host name is allowed, the proxy resolves it and applies step 2, and
the denied IP addresses, to every address it resolves to.
"""

from __future__ import annotations

import ipaddress
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from meltzer.approval.orchestrator import NetworkRequest
from meltzer.config.loader import ConfigError

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_NUMERIC_LABEL = re.compile(r"^(0x[0-9a-f]*|[0-9]+)$")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")


def normalize_address(address: IPAddress) -> IPAddress:
    """The address without an IPv6 scope id; an IPv4-mapped IPv6 address as its IPv4 address."""
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
        if address.scope_id:
            return ipaddress.IPv6Address(int(address))
    return address


def parse_address(text: str) -> IPAddress | None:
    """The IP address in text (IPv6 with or without brackets), in its normalized form, or None."""
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        return normalize_address(ipaddress.ip_address(text))
    except ValueError:
        return None


def normalize_host(text: str) -> str:
    """A host name in lower case without a trailing dot, or an IP address in its usual form.

    Raises ValueError for anything else, including numeric forms that are not
    plain IP addresses (such as 2130706433 or 0x7f.1).
    """
    address = parse_address(text)
    if address is not None:
        return str(address)
    host = text.lower()
    if host.endswith("."):
        host = host[:-1]
    labels = host.split(".")
    if not host or len(host) > 253 or not all(_LABEL.match(label) for label in labels):
        raise ValueError(f"{text!r} is not a valid host name or IP address")
    if _NUMERIC_LABEL.match(labels[-1]):
        raise ValueError(f"{text!r} is not a valid host name or IP address")
    return host


def is_local_address(address: IPAddress) -> bool:
    """Whether the address is loopback, private, link-local, multicast or otherwise not public."""
    address = normalize_address(address)
    if isinstance(address, ipaddress.IPv6Address) and (address in _NAT64 or address in _IPV4_COMPATIBLE):
        return True
    return not address.is_global or address.is_multicast


@dataclass(frozen=True)
class HostPattern:
    """One entry of an allow or deny list."""

    text: str
    host: str = ""
    # True for `*.domain`: subdomains of `host` only.
    subdomains: bool = False
    address: IPAddress | None = None

    @classmethod
    def parse(cls, text: str) -> HostPattern:
        if text.startswith("*."):
            domain = normalize_host(text[2:])
            if parse_address(domain) is not None:
                raise ValueError(f"{text!r}: '*.' takes a domain, not an IP address")
            return cls(text, host=domain, subdomains=True)
        if "*" in text:
            raise ValueError(f"{text!r}: only '*.' at the start of an entry is supported")
        address = parse_address(text)
        if address is not None:
            return cls(text, host=str(address), address=address)
        return cls(text, host=normalize_host(text))

    def matches(self, host: str) -> bool:
        """Whether a normalized host matches this entry."""
        if self.subdomains:
            return host.endswith("." + self.host)
        return host == self.host


def _patterns(entries: tuple[str, ...]) -> tuple[HostPattern, ...]:
    return tuple(HostPattern.parse(entry) for entry in entries)


@dataclass(frozen=True)
class NetworkPolicy:
    """The allow and deny lists of [network]. Empty lists allow no host without review."""

    allowed: tuple[str, ...] = ()
    denied: tuple[str, ...] = ()
    allow_local_binding: bool = False
    _allowed: tuple[HostPattern, ...] = field(init=False, repr=False, compare=False)
    _denied: tuple[HostPattern, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Raises ValueError for an entry that is not a host, `*.domain` or IP address.
        object.__setattr__(self, "_allowed", _patterns(self.allowed))
        object.__setattr__(self, "_denied", _patterns(self.denied))

    @classmethod
    def from_config(cls, config: Any) -> NetworkPolicy:
        for key, entries in (
            ("network.allowed_domains", config.network_allowed_domains),
            ("network.denied_domains", config.network_denied_domains),
        ):
            for entry in entries:
                try:
                    HostPattern.parse(entry)
                except ValueError as exc:
                    raise ConfigError(f"{key}: {exc}") from None
        return cls(
            allowed=tuple(config.network_allowed_domains),
            denied=tuple(config.network_denied_domains),
            allow_local_binding=config.network_allow_local_binding,
        )

    def is_denied(self, host: str) -> bool:
        host = normalize_host(host)
        return any(pattern.matches(host) for pattern in self._denied)

    def is_allowed(self, host: str) -> bool:
        host = normalize_host(host)
        return any(pattern.matches(host) for pattern in self._allowed)

    def check_address(self, address: IPAddress) -> str | None:
        """Why the proxy may not connect to this address, or None if it may."""
        address = normalize_address(address)
        text = str(address)
        if any(pattern.address == address for pattern in self._denied):
            return f"{text} is on the denied list (network.denied_domains)"
        if not is_local_address(address):
            return None
        if any(pattern.address == address for pattern in self._allowed):
            return None
        if self.allow_local_binding and address.is_loopback:
            return None
        return f"{text} is a local or private address and is not on the allow list (network.allowed_domains)"


@dataclass(frozen=True)
class NetworkDecision:
    allowed: bool
    reason: str


class ReviewDecision(Protocol):
    run: bool
    reason: str
    for_session: bool


class NetworkApprovals:
    """Network decisions for the commands of one session.

    `decide` is the session's orchestrator.decide_network; without it, hosts
    that are not on the allow list are refused.
    """

    def __init__(
        self, policy: NetworkPolicy, decide: Callable[[NetworkRequest], ReviewDecision] | None = None
    ):
        self.policy = policy
        self._decide = decide
        # (host, port, protocol) approved for the session.
        self._approved: set[tuple[str, int, str]] = set()
        # One lock per (host, port, protocol), held while the reviewer decides, so concurrent
        # requests for one host are reviewed one after the other.
        self._locks: dict[tuple[str, int, str], threading.Lock] = {}
        self._lock = threading.Lock()

    def check(self, host: str, port: int, protocol: str, command: str, cwd: Path) -> NetworkDecision:
        """Whether a command may connect to host:port. `host` must be normalized (normalize_host)."""
        if self.policy.is_denied(host):
            return NetworkDecision(False, f"{host} is on the denied list (network.denied_domains)")
        address = parse_address(host)
        if address is not None:
            refused = self.policy.check_address(address)
            if refused is not None:
                return NetworkDecision(False, refused)
        if self.policy.is_allowed(host):
            return NetworkDecision(True, f"{host} is on the allow list")
        if self._decide is None:
            return NetworkDecision(False, f"{host} is not on the allow list (network.allowed_domains)")
        key = (host, port, protocol)
        with self._lock:
            key_lock = self._locks.setdefault(key, threading.Lock())
        with key_lock:
            if key in self._approved:
                return NetworkDecision(True, f"{host}:{port} was approved earlier in this session")
            decision = self._decide(NetworkRequest(host, port, protocol, command, cwd))
            if not decision.run:
                return NetworkDecision(False, f"access to {host}:{port} was not approved: {decision.reason}")
            if decision.for_session:
                self._approved.add(key)
        return NetworkDecision(True, decision.reason)
