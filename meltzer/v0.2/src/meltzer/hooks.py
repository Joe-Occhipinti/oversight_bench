"""Hooks: programs from the configuration that run on session and tool events.

Events:
  SessionStart       once, when the main session has started
  SessionEnd         once, when the main session ends
  UserPromptSubmit   before a prompt is added to the conversation
  PreToolUse         before each tool call; a hook that exits with status 2 blocks
                     the call, and its stderr is returned to the model as the reason
                     (or "blocked by <command>" when stderr is empty)
  PermissionRequest  before a request goes to the reviewer; a hook can deny it
  PostToolUse        after each tool call, with the tool's output
  PreCompact         before the conversation is compacted
  PostCompact        after the conversation is compacted
  SubagentStart      once, when a subagent session has started
  SubagentStop       when a subagent has finished a turn
  Stop               when the model has finished a turn of the main session

Configuration:

    [[hooks.PreToolUse]]
    matcher = "shell"            # tool name, several as "a|b", or "*" (default)
    command = ["python3", ".meltzer/hooks/check.py"]
    cwd = "."                    # relative to the session root (default)

`matcher` applies to PreToolUse and PostToolUse (the tool name),
PermissionRequest (the request type, e.g. "shell" or "apply_patch"), and
SubagentStart and SubagentStop (the agent type). Each hook receives a JSON
object describing the event on stdin; meltzer.hook_output describes what its
exit status and output do. The hooks of an event run one after another, in
configuration order.

A hook runs only once the user has trusted it (~/.meltzer/trusted_hooks.toml).
Interactive sessions offer to trust new hooks at startup; `meltzer exec` skips
untrusted hooks.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import posixpath
import shlex
import signal
import subprocess
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from meltzer.hook_output import (
    HOOK_OUTPUT_LIMIT,
    PERMISSION_REQUEST,
    POST_COMPACT,
    POST_TOOL_USE,
    PRE_COMPACT,
    PRE_TOOL_USE,
    SESSION_END,
    SESSION_START,
    STOP,
    SUBAGENT_START,
    SUBAGENT_STOP,
    USER_PROMPT_SUBMIT,
    HookOutcome,
    parse_output,
    preview,
)
from meltzer.tomlwrite import toml_string

if TYPE_CHECKING:
    from meltzer.approval.policy import ApprovalRequest

HOOK_EVENTS = (
    SESSION_START,
    SESSION_END,
    USER_PROMPT_SUBMIT,
    PRE_TOOL_USE,
    PERMISSION_REQUEST,
    POST_TOOL_USE,
    PRE_COMPACT,
    POST_COMPACT,
    SUBAGENT_START,
    SUBAGENT_STOP,
    STOP,
)
TOOL_EVENTS = frozenset({PRE_TOOL_USE, PERMISSION_REQUEST, POST_TOOL_USE})
# Events whose hooks are selected by `matcher`: tool events by tool name or
# request type, subagent events by agent type.
MATCHER_EVENTS = TOOL_EVENTS | {SUBAGENT_START, SUBAGENT_STOP}
# Events where the first hook that blocks stops the hooks after it.
FIRST_BLOCK_EVENTS = frozenset({PRE_TOOL_USE, PERMISSION_REQUEST})

MATCH_ALL = "*"
TRUSTED_HOOKS_FILE = "trusted_hooks.toml"
HOOK_TIMEOUT_SECONDS = 60.0


class HookConfigError(ValueError):
    """A hook definition in the configuration is invalid."""


@dataclass(frozen=True)
class HookDefinition:
    """A hook as written in the configuration."""

    event: str
    command: tuple[str, ...]
    cwd: str | None = None
    matcher: str = MATCH_ALL

    def to_dict(self) -> dict[str, Any]:
        entry: dict[str, Any] = {"command": list(self.command), "matcher": self.matcher}
        if self.cwd is not None:
            entry["cwd"] = self.cwd
        return entry

    def resolve(self, root: Path) -> Hook:
        cwd = Path(self.cwd).expanduser() if self.cwd else Path(".")
        if not cwd.is_absolute():
            cwd = root / cwd
        return Hook(
            event=self.event,
            command=self.command,
            cwd=Path(os.path.realpath(cwd)),
            matcher=self.matcher,
        )


def parse_hook_entries(event: str, value: Any) -> tuple[HookDefinition, ...]:
    """Validate the list configured for `hooks.<event>`."""
    if not isinstance(value, list):
        raise HookConfigError(f"expected a list of hook tables, got {value!r}")
    hooks = []
    for index, entry in enumerate(value):
        where = f"hook #{index + 1}"
        if not isinstance(entry, dict):
            raise HookConfigError(f"{where}: expected a table, got {entry!r}")
        unknown = set(entry) - {"command", "cwd", "matcher"}
        if unknown:
            raise HookConfigError(f"{where}: unknown field(s): {', '.join(sorted(unknown))}")
        command = entry.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(arg, str) and arg for arg in command)
        ):
            raise HookConfigError(f"{where}: 'command' must be a non-empty list of strings")
        cwd = entry.get("cwd")
        if cwd is not None and (not isinstance(cwd, str) or not cwd):
            raise HookConfigError(f"{where}: 'cwd' must be a non-empty string")
        if cwd is not None:
            # Equivalent spellings compare equal, so a hook defined twice runs once.
            cwd = posixpath.normpath(cwd)
            if cwd == ".":
                cwd = None
        matcher = entry.get("matcher", MATCH_ALL)
        if not isinstance(matcher, str) or not matcher:
            raise HookConfigError(f"{where}: 'matcher' must be a non-empty string")
        hooks.append(HookDefinition(event=event, command=tuple(command), cwd=cwd, matcher=matcher))
    return tuple(hooks)


@dataclass(frozen=True)
class Hook:
    """A hook with its working directory resolved for this session."""

    event: str
    command: tuple[str, ...]
    cwd: Path
    matcher: str = MATCH_ALL

    def identity(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "command": list(self.command),
            "cwd": str(self.cwd),
            "matcher": self.matcher,
        }

    @property
    def sha256(self) -> str:
        canonical = json.dumps(self.identity(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def applies_to(self, target: str | None) -> bool:
        """Whether the hook runs for `target`: the tool name, request type or agent type of the event."""
        if self.event not in MATCHER_EVENTS or self.matcher == MATCH_ALL:
            return True
        return target in {name.strip() for name in self.matcher.split("|")}

    def describe(self) -> str:
        lines = [f"  event:   {self.event}"]
        if self.event in MATCHER_EVENTS:
            lines.append(f"  matcher: {self.matcher}")
        lines += [
            f"  command: {shlex.join(self.command)}",
            f"  cwd:     {self.cwd}",
            f"  sha256:  {self.sha256}",
        ]
        return "\n".join(lines)


class TrustStore:
    """Trusted hooks, stored in ~/.meltzer/trusted_hooks.toml."""

    def __init__(self, path: Path):
        self.path = path
        self.warnings: list[str] = []
        self._hashes = self._read()

    def _read(self) -> set[str]:
        if not self.path.is_file():
            return set()
        try:
            with self.path.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            self.warnings.append(f"{self.path}: cannot read trusted hooks, no hook is trusted: {exc}")
            return set()
        entries = data.get("hook", [])
        if not isinstance(entries, list):
            self.warnings.append(f"{self.path}: 'hook' must be an array of tables; no hook is trusted")
            return set()
        return {
            entry["sha256"]
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("sha256"), str)
        }

    def is_trusted(self, hook: Hook) -> bool:
        return hook.sha256 in self._hashes

    def trust(self, hook: Hook) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        command = ", ".join(toml_string(arg) for arg in hook.command)
        entry = (
            "\n[[hook]]\n"
            f"sha256 = {toml_string(hook.sha256)}\n"
            f"event = {toml_string(hook.event)}\n"
            f"command = [{command}]\n"
            f"cwd = {toml_string(str(hook.cwd))}\n"
            f"matcher = {toml_string(hook.matcher)}\n"
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(entry)
        self._hashes.add(hook.sha256)


@dataclass
class HookRunner:
    hooks: list[Hook]
    session_id: str
    root: Path
    transcript_path: Path
    notice: Callable[[str], None]
    timeout: float = HOOK_TIMEOUT_SECONDS
    # Writes an event to the session log; hook output longer than output_limit is kept there in full.
    record: Callable[[str, dict[str, Any]], None] | None = None
    output_limit: int = HOOK_OUTPUT_LIMIT
    # The role of a subagent session; None for the main session.
    agent_type: str | None = None

    @classmethod
    def for_session(
        cls,
        definitions: tuple[HookDefinition, ...],
        *,
        root: Path,
        trust: TrustStore,
        confirm: Callable[[Hook], bool] | None,
        notice: Callable[[str], None],
        session_id: str,
        transcript_path: Path,
        record: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> HookRunner:
        """Keep the trusted hooks; ask about the others when confirm is given, else skip them."""
        for warning in trust.warnings:
            notice(f"warning: {warning}")
        active: list[Hook] = []
        for definition in definitions:
            hook = definition.resolve(root)
            if trust.is_trusted(hook):
                active.append(hook)
                continue
            if confirm is not None and confirm(hook):
                try:
                    trust.trust(hook)
                except OSError as exc:
                    notice(f"warning: could not record trust in {trust.path}: {exc}; trusted for this session only")
                active.append(hook)
                continue
            notice(
                f"skipping untrusted {hook.event} hook: {shlex.join(hook.command)} "
                f"(sha256 {hook.sha256}); trust it from an interactive session"
            )
        return cls(
            hooks=active,
            session_id=session_id,
            root=root,
            transcript_path=transcript_path,
            notice=notice,
            record=record,
        )

    def for_subagent(
        self,
        session_id: str,
        transcript_path: Path,
        *,
        agent_type: str | None = None,
        record: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> HookRunner:
        """The same hooks for a subagent session; trust was settled when the main session started."""
        return replace(
            self,
            hooks=list(self.hooks),
            session_id=session_id,
            transcript_path=transcript_path,
            agent_type=agent_type,
            record=record,
        )

    def _payload(self, event: str, **fields: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_id": self.session_id,
            "transcript_path": str(self.transcript_path),
            "cwd": str(self.root),
            "hook_event_name": event,
        }
        if self.agent_type is not None:
            payload["agent_id"] = self.session_id
            payload["agent_type"] = self.agent_type
        payload.update(fields)
        return payload

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["MELTZER_PROJECT_DIR"] = str(self.root)
        env["MELTZER_SESSION_ID"] = self.session_id
        return env

    def _execute(self, hook: Hook, payload: dict[str, Any]) -> subprocess.CompletedProcess[str] | None:
        label = f"{hook.event} hook {shlex.join(hook.command)}"
        try:
            process = subprocess.Popen(
                list(hook.command),
                cwd=hook.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                env=self._env(),
                # Own process group, so a timeout stops the processes the hook started too.
                start_new_session=True,
            )
        except OSError as exc:
            self.notice(f"{label} failed to start: {exc}")
            return None
        try:
            stdout, stderr = process.communicate(json.dumps(payload, ensure_ascii=False), timeout=self.timeout)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            process.communicate()
            self.notice(f"{label} timed out after {self.timeout:.0f} seconds")
            return None
        return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)

    def _run(self, hook: Hook, payload: dict[str, Any]) -> HookOutcome | None:
        """Run one hook; None when it did not finish or its output was ignored."""
        completed = self._execute(hook, payload)
        if completed is None:
            return None
        outcome = parse_output(hook.event, completed.returncode, completed.stdout, completed.stderr)
        label = f"{hook.event} hook {shlex.join(hook.command)}"
        if outcome.system_message:
            self.notice(f"{label}: {outcome.system_message}")
        if outcome.error:
            self.notice(f"{label} {outcome.error}")
            return None
        reason = outcome.reason
        if (outcome.blocked or outcome.stop) and not reason:
            verb = "denied" if outcome.decision == "deny" else "blocked" if outcome.blocked else "stopped"
            reason = f"{verb} by {shlex.join(hook.command)}"
        else:
            reason = self._shorten(hook, reason)
        contexts = tuple(self._shorten(hook, text) for text in outcome.additional_context)
        return replace(outcome, reason=reason, additional_context=contexts)

    def _shorten(self, hook: Hook, text: str) -> str:
        """Text for the model: over output_limit it is cut, and recorded in full in the session log."""
        if self.output_limit <= 0 or len(text) <= self.output_limit:
            return text
        if self.record is not None:
            self.record("hook_output", {"event": hook.event, "command": list(hook.command), "text": text})
        return preview(text, self.output_limit)

    def _matching(self, event: str, target: str | None = None) -> list[Hook]:
        return [hook for hook in self.hooks if hook.event == event and hook.applies_to(target)]

    def _dispatch(self, event: str, payload: dict[str, Any], target: str | None = None) -> HookOutcome:
        """Run the matching hooks of `event` and combine what they asked for.

        A block or stop from any hook holds, and a deny wins over an allow. The
        reasons of the hooks that blocked (or else of those that stopped) are
        joined, as are their contexts.
        """
        blocked = stop = False
        block_reasons: list[str] = []
        stop_reasons: list[str] = []
        contexts: list[str] = []
        decision: str | None = None
        updated_input: dict[str, Any] | None = None
        for hook in self._matching(event, target):
            outcome = self._run(hook, payload)
            if outcome is None:
                continue
            contexts.extend(outcome.additional_context)
            if outcome.updated_input is not None:
                updated_input = outcome.updated_input
            if outcome.decision == "allow" and decision is None:
                decision = "allow"
            if outcome.stop:
                stop = True
                stop_reasons.append(outcome.reason)
            if outcome.blocked:
                blocked = True
                block_reasons.append(outcome.reason)
                if outcome.decision == "deny":
                    decision = "deny"
                if event in FIRST_BLOCK_EVENTS:
                    break
        return HookOutcome(
            blocked=blocked,
            reason="\n\n".join(block_reasons if blocked else stop_reasons),
            additional_context=tuple(contexts),
            decision=decision,
            updated_input=None if blocked else updated_input,
            stop=stop,
        )

    def session_start(self, source: str = "startup") -> HookOutcome:
        return self._dispatch(SESSION_START, self._payload(SESSION_START, source=source))

    def session_end(self, reason: str) -> HookOutcome:
        return self._dispatch(SESSION_END, self._payload(SESSION_END, reason=reason))

    def user_prompt_submit(self, prompt: str) -> HookOutcome:
        return self._dispatch(USER_PROMPT_SUBMIT, self._payload(USER_PROMPT_SUBMIT, prompt=prompt))

    def pre_tool_use(self, tool_name: str, arguments: str) -> HookOutcome:
        payload = self._payload(PRE_TOOL_USE, tool_name=tool_name, tool_input=_tool_input(arguments))
        return self._dispatch(PRE_TOOL_USE, payload, tool_name)

    def permission_request(self, request: ApprovalRequest) -> HookOutcome:
        payload = self._payload(PERMISSION_REQUEST, tool_name=request.tool, tool_input=_request_input(request))
        return self._dispatch(PERMISSION_REQUEST, payload, request.tool)

    def post_tool_use(self, tool_name: str, arguments: str, output: str, success: bool) -> HookOutcome:
        payload = self._payload(
            POST_TOOL_USE,
            tool_name=tool_name,
            tool_input=_tool_input(arguments),
            tool_response={"output": output, "success": success},
        )
        return self._dispatch(POST_TOOL_USE, payload, tool_name)

    def pre_compact(self, trigger: str) -> HookOutcome:
        return self._dispatch(PRE_COMPACT, self._payload(PRE_COMPACT, trigger=trigger))

    def post_compact(self, trigger: str) -> HookOutcome:
        return self._dispatch(POST_COMPACT, self._payload(POST_COMPACT, trigger=trigger))

    def subagent_start(self) -> HookOutcome:
        return self._dispatch(SUBAGENT_START, self._payload(SUBAGENT_START), self.agent_type)

    def subagent_stop(self, last_assistant_message: str, stop_hook_active: bool = False) -> HookOutcome:
        payload = self._payload(
            SUBAGENT_STOP,
            stop_hook_active=stop_hook_active,
            last_assistant_message=last_assistant_message,
            agent_transcript_path=str(self.transcript_path),
        )
        return self._dispatch(SUBAGENT_STOP, payload, self.agent_type)

    def stop(self, last_assistant_message: str, stop_hook_active: bool = False) -> HookOutcome:
        payload = self._payload(STOP, stop_hook_active=stop_hook_active, last_assistant_message=last_assistant_message)
        return self._dispatch(STOP, payload)


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (AttributeError, ProcessLookupError, PermissionError):
        process.kill()


def _tool_input(arguments: str) -> Any:
    try:
        return json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError:
        return arguments


def _json_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _request_input(request: ApprovalRequest) -> dict[str, Any]:
    """The fields of an approval request that are set, other than its type."""
    tool_input: dict[str, Any] = {}
    for field in dataclasses.fields(request):
        value = getattr(request, field.name)
        if field.name in ("tool", "suggested_prefix") or value is None or value is False:
            continue
        if isinstance(value, (str, tuple, dict)) and not value:
            continue
        tool_input[field.name] = _json_value(value)
    return tool_input
