"""Configuration keys, their types and defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable

from meltzer.approval.policy import ApprovalPolicy, ReviewerKind
from meltzer.config.trust import ProjectTrust
from meltzer.hooks import (
    HOOK_EVENTS,
    POST_TOOL_USE,
    PRE_TOOL_USE,
    SESSION_START,
    STOP,
    HookDefinition,
    parse_hook_entries,
)
from meltzer.sandbox.policy import SandboxMode


class ConfigValueError(ValueError):
    """A value has the wrong type or is not one of the accepted choices."""


def _string(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigValueError(f"expected a non-empty string, got {value!r}")
    return value


def _string_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise ConfigValueError(f"expected a non-empty list of non-empty strings, got {value!r}")
    return tuple(value)


def _hooks(event: str) -> Callable[[Any], tuple[HookDefinition, ...]]:
    def parse(value: Any) -> tuple[HookDefinition, ...]:
        try:
            return parse_hook_entries(event, value)
        except ValueError as exc:
            raise ConfigValueError(str(exc)) from None

    return parse


def _positive_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigValueError(f"expected a positive integer, got {value!r}")
    return value


def _choice(enum_type: type[StrEnum]) -> Callable[[Any], StrEnum]:
    def parse(value: Any) -> StrEnum:
        try:
            return enum_type(value)
        except ValueError:
            allowed = ", ".join(member.value for member in enum_type)
            raise ConfigValueError(f"expected one of: {allowed}; got {value!r}") from None

    return parse


@dataclass(frozen=True)
class ConfigKey:
    name: str  # dotted TOML path, e.g. "model_provider.base_url"
    attr: str  # attribute on Config
    default: Any
    parse: Callable[[Any], Any]
    # Lists from several layers are concatenated instead of replaced.
    merge: bool = False


DEFAULT_MODEL = "gpt-5.1"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_API_KEY_ENV = "MELTZER_API_KEY"
DEFAULT_MAX_TURN_STEPS = 100

# Config attribute holding the hooks of each event.
HOOK_ATTRS = {
    SESSION_START: "hooks_session_start",
    PRE_TOOL_USE: "hooks_pre_tool_use",
    POST_TOOL_USE: "hooks_post_tool_use",
    STOP: "hooks_stop",
}

KEYS: tuple[ConfigKey, ...] = (
    ConfigKey("model", "model", DEFAULT_MODEL, _string),
    ConfigKey("model_provider.base_url", "base_url", DEFAULT_BASE_URL, _string),
    ConfigKey("model_provider.api_key_env", "api_key_env", DEFAULT_API_KEY_ENV, _string),
    ConfigKey("sandbox_mode", "sandbox_mode", SandboxMode.WORKSPACE_WRITE, _choice(SandboxMode)),
    ConfigKey("approval_policy", "approval_policy", ApprovalPolicy.ON_REQUEST, _choice(ApprovalPolicy)),
    ConfigKey("approvals_reviewer", "approvals_reviewer", ReviewerKind.USER, _choice(ReviewerKind)),
    # Model requests per turn; the turn stops when it reaches the limit.
    ConfigKey("max_turn_steps", "max_turn_steps", DEFAULT_MAX_TURN_STEPS, _positive_int),
    ConfigKey("notify", "notify", None, _string_list),
    *(ConfigKey(f"hooks.{event}", HOOK_ATTRS[event], (), _hooks(event), merge=True) for event in HOOK_EVENTS),
    # Model for automatic approval reviews; unset means the session model.
    ConfigKey("monitor.model", "monitor_model", None, _string),
    # Transcript length of approval reviews.
    ConfigKey("monitor.transcript_items", "monitor_transcript_items", 40, _positive_int),
)

KEYS_BY_NAME: dict[str, ConfigKey] = {key.name: key for key in KEYS}


@dataclass(frozen=True)
class Config:
    model: str
    base_url: str
    api_key_env: str
    sandbox_mode: SandboxMode
    approval_policy: ApprovalPolicy
    approvals_reviewer: ReviewerKind
    max_turn_steps: int
    notify: tuple[str, ...] | None
    hooks_session_start: tuple[HookDefinition, ...]
    hooks_pre_tool_use: tuple[HookDefinition, ...]
    hooks_post_tool_use: tuple[HookDefinition, ...]
    hooks_stop: tuple[HookDefinition, ...]
    monitor_model: str | None
    monitor_transcript_items: int
    # Where each value came from: key name -> "default", "command line", or a file path.
    sources: dict[str, str] = field(default_factory=dict, compare=False)
    warnings: tuple[str, ...] = field(default=(), compare=False)
    # Trust decision for the session directory; None when built without one.
    project_trust: ProjectTrust | None = field(default=None, compare=False)

    @property
    def project_trusted(self) -> bool:
        return self.project_trust is not None and self.project_trust.trusted

    def summary(self) -> dict[str, Any]:
        """Effective values keyed by config name, for logs and session records."""
        return {key.name: _plain(getattr(self, key.attr)) for key in KEYS}

    def hook_definitions(self) -> tuple[HookDefinition, ...]:
        """All configured hooks, in event order."""
        return tuple(hook for event in HOOK_EVENTS for hook in getattr(self, HOOK_ATTRS[event]))


def _plain(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, HookDefinition):
        return value.to_dict()
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value
