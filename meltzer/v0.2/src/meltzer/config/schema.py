"""Configuration keys, their types and defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable

from meltzer.approval.policy import ApprovalPolicy, ReviewerKind
from meltzer.config.trust import ProjectTrust
from meltzer.hooks import HOOK_EVENTS, HookDefinition, parse_hook_entries
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


def _string_tuple(value: Any) -> tuple[str, ...]:
    """A list of non-empty strings; may be empty."""
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigValueError(f"expected a list of non-empty strings, got {value!r}")
    return tuple(value)


def _positive_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigValueError(f"expected a positive integer, got {value!r}")
    return value


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ConfigValueError(f"expected true or false, got {value!r}")
    return value


def _table(value: Any) -> dict[str, Any]:
    """A table of named entries, e.g. [mcp_servers.<name>]. Entries are checked by the feature that uses them."""
    if not isinstance(value, dict):
        raise ConfigValueError(f"expected a table, got {value!r}")
    return {name: _copy(entry) for name, entry in value.items()}


def _table_list(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or not all(isinstance(entry, dict) for entry in value):
        raise ConfigValueError(f"expected a list of tables, got {value!r}")
    return tuple(_copy(entry) for entry in value)


def _copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {name: _copy(entry) for name, entry in value.items()}
    if isinstance(value, list):
        return [_copy(entry) for entry in value]
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
    # Tables from several layers are merged by entry name; a later layer replaces an entry of the same name.
    merge_entries: bool = False
    # Read only from the user config and the command line.
    user_only: bool = False


DEFAULT_MODEL = "gpt-5.1"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_API_KEY_ENV = "MELTZER_API_KEY"
DEFAULT_MAX_TURN_STEPS = 100
DEFAULT_BACKGROUND_TERMINAL_MAX_TIMEOUT_MS = 300_000
DEFAULT_AGENTS_MAX_DEPTH = 1
DEFAULT_AGENTS_MAX_THREADS = 6
DEFAULT_MEMORIES_MAX_BYTES = 16_384


def _snake(event: str) -> str:
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in event).lstrip("_")


# Config attribute holding the hooks of each event.
HOOK_ATTRS = {event: f"hooks_{_snake(event)}" for event in HOOK_EVENTS}

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
    # File whose text is appended to the system prompt; read once when the session starts.
    ConfigKey("developer_instructions_file", "developer_instructions_file", None, _string),
    *(ConfigKey(f"hooks.{event}", HOOK_ATTRS[event], (), _hooks(event), merge=True) for event in HOOK_EVENTS),
    # Model for automatic approval reviews; unset means the session model.
    ConfigKey("monitor.model", "monitor_model", None, _string),
    # Transcript length of approval reviews.
    ConfigKey("monitor.transcript_items", "monitor_transcript_items", 40, _positive_int),
    # Description of this machine, added to the environment profile of approval reviews.
    ConfigKey("monitor.environment", "monitor_environment", None, _string, user_only=True),
    # Hosts that approval reviews treat as services of the user's organization.
    ConfigKey("monitor.trusted_hosts", "monitor_trusted_hosts", (), _string_tuple, user_only=True),
    # Interactive processes: longest wait, in milliseconds, for output of a running process.
    ConfigKey(
        "background_terminal_max_timeout", "background_terminal_max_timeout",
        DEFAULT_BACKGROUND_TERMINAL_MAX_TIMEOUT_MS, _positive_int,
    ),
    # Command tools: exec_command and write_stdin when true, shell when false.
    ConfigKey("features.unified_exec", "unified_exec", True, _bool),
    # Subagents.
    ConfigKey("agents.max_depth", "agents_max_depth", DEFAULT_AGENTS_MAX_DEPTH, _positive_int),
    ConfigKey("agents.max_threads", "agents_max_threads", DEFAULT_AGENTS_MAX_THREADS, _positive_int),
    ConfigKey("agents.default_model", "agents_default_model", None, _string),
    # MCP servers, by name.
    ConfigKey("mcp_servers", "mcp_servers", {}, _table, merge_entries=True),
    # Permission profiles, by name, and the profile sessions use.
    ConfigKey("permissions", "permissions", {}, _table, merge_entries=True),
    ConfigKey("default_permissions", "default_permissions", None, _string),
    # Network access of sandboxed commands, through the proxy.
    ConfigKey("network.enabled", "network_enabled", False, _bool),
    ConfigKey("network.allowed_domains", "network_allowed_domains", (), _string_tuple, merge=True),
    ConfigKey("network.denied_domains", "network_denied_domains", (), _string_tuple, merge=True),
    ConfigKey("network.allow_local_binding", "network_allow_local_binding", False, _bool, user_only=True),
    # Web search.
    ConfigKey("web_search.enabled", "web_search_enabled", False, _bool, user_only=True),
    ConfigKey("web_search.endpoint", "web_search_endpoint", None, _string, user_only=True),
    ConfigKey("web_search.api_key_env", "web_search_api_key_env", None, _string, user_only=True),
    # Skills.
    ConfigKey("skills.include_instructions", "skills_include_instructions", True, _bool),
    ConfigKey("skills.config", "skills_config", (), _table_list, user_only=True),
    # Plugins and the marketplaces they are installed from.
    ConfigKey("plugins", "plugins", {}, _table, merge_entries=True, user_only=True),
    ConfigKey("marketplaces", "marketplaces", {}, _table, merge_entries=True, user_only=True),
    # Memories from earlier sessions.
    ConfigKey("memories.generate", "memories_generate", False, _bool, user_only=True),
    ConfigKey("memories.use", "memories_use", False, _bool, user_only=True),
    ConfigKey("memories.extract_model", "memories_extract_model", None, _string, user_only=True),
    ConfigKey("memories.max_bytes", "memories_max_bytes", DEFAULT_MEMORIES_MAX_BYTES, _positive_int, user_only=True),
    # Context window of the model, in tokens; unset when unknown.
    ConfigKey("model_context_window", "model_context_window", None, _positive_int),
    # Full-screen terminal interface for interactive sessions.
    ConfigKey("tui.enabled", "tui_enabled", False, _bool),
    # Realtime voice.
    ConfigKey("realtime.base_url", "realtime_base_url", None, _string, user_only=True),
    ConfigKey("realtime.model", "realtime_model", None, _string),
    ConfigKey("realtime.voice", "realtime_voice", None, _string),
    # Image generation.
    ConfigKey("image_generation.model", "image_generation_model", None, _string, user_only=True),
    # Telemetry.
    ConfigKey("otel", "otel", {}, _table, user_only=True),
    ConfigKey("analytics.enabled", "analytics_enabled", False, _bool, user_only=True),
    ConfigKey("feedback.enabled", "feedback_enabled", False, _bool, user_only=True),
    ConfigKey("feedback.endpoint", "feedback_endpoint", None, _string, user_only=True),
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
    developer_instructions_file: str | None
    hooks_session_start: tuple[HookDefinition, ...]
    hooks_session_end: tuple[HookDefinition, ...]
    hooks_user_prompt_submit: tuple[HookDefinition, ...]
    hooks_pre_tool_use: tuple[HookDefinition, ...]
    hooks_permission_request: tuple[HookDefinition, ...]
    hooks_post_tool_use: tuple[HookDefinition, ...]
    hooks_pre_compact: tuple[HookDefinition, ...]
    hooks_post_compact: tuple[HookDefinition, ...]
    hooks_subagent_start: tuple[HookDefinition, ...]
    hooks_subagent_stop: tuple[HookDefinition, ...]
    hooks_stop: tuple[HookDefinition, ...]
    monitor_model: str | None
    monitor_transcript_items: int
    monitor_environment: str | None
    monitor_trusted_hosts: tuple[str, ...]
    background_terminal_max_timeout: int
    unified_exec: bool
    agents_max_depth: int
    agents_max_threads: int
    agents_default_model: str | None
    mcp_servers: dict[str, Any]
    permissions: dict[str, Any]
    default_permissions: str | None
    network_enabled: bool
    network_allowed_domains: tuple[str, ...]
    network_denied_domains: tuple[str, ...]
    network_allow_local_binding: bool
    web_search_enabled: bool
    web_search_endpoint: str | None
    web_search_api_key_env: str | None
    skills_include_instructions: bool
    skills_config: tuple[dict[str, Any], ...]
    plugins: dict[str, Any]
    marketplaces: dict[str, Any]
    memories_generate: bool
    memories_use: bool
    memories_extract_model: str | None
    memories_max_bytes: int
    model_context_window: int | None
    tui_enabled: bool
    realtime_base_url: str | None
    realtime_model: str | None
    realtime_voice: str | None
    image_generation_model: str | None
    otel: dict[str, Any]
    analytics_enabled: bool
    feedback_enabled: bool
    feedback_endpoint: str | None
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
        values = {key.name: _plain(getattr(self, key.attr)) for key in KEYS}
        if "mcp_servers" in values:
            values["mcp_servers"] = _redact_mcp_env(values["mcp_servers"])
        values["otel"] = _redact_otel_headers(values["otel"])
        return values

    def hook_definitions(self) -> tuple[HookDefinition, ...]:
        """All configured hooks, in event order."""
        return tuple(hook for event in HOOK_EVENTS for hook in getattr(self, HOOK_ATTRS[event]))


def _redact_mcp_env(servers: Any) -> Any:
    """The `mcp_servers` table with the values of each entry's `env` table replaced by "<redacted>"."""
    if not isinstance(servers, dict):
        return servers
    redacted = {}
    for name, entry in servers.items():
        if isinstance(entry, dict) and isinstance(entry.get("env"), dict):
            entry = {**entry, "env": {key: "<redacted>" for key in entry["env"]}}
        redacted[name] = entry
    return redacted


def _redact_otel_headers(otel: Any) -> Any:
    """The `otel` table with the values of the exporter's `headers` table replaced by "<redacted>"."""
    exporter = otel.get("exporter") if isinstance(otel, dict) else None
    entry = exporter.get("otlp-http") if isinstance(exporter, dict) else None
    if not isinstance(entry, dict) or not isinstance(entry.get("headers"), dict):
        return otel
    headers = {key: "<redacted>" for key in entry["headers"]}
    return {**otel, "exporter": {**exporter, "otlp-http": {**entry, "headers": headers}}}


def _plain(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, HookDefinition):
        return value.to_dict()
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value
