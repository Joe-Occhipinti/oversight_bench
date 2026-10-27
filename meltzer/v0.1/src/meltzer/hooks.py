"""Hooks: programs from the configuration that run on session and tool events.

Events:
  SessionStart  once, when the main session has started (not for subagents)
  PreToolUse    before each tool call; a hook that exits with status 2 blocks
                the call, and its stderr is returned to the model as the reason
                (or "blocked by <command>" when stderr is empty)
  PostToolUse   after each tool call, with the tool's output
  Stop          when the model has finished a turn of the main session

Configuration:

    [[hooks.PreToolUse]]
    matcher = "shell"            # tool name, several as "a|b", or "*" (default)
    command = ["python3", ".meltzer/hooks/check_command.py"]
    cwd = "."                    # relative to the session root (default)

`matcher` applies to PreToolUse and PostToolUse. Each hook receives a JSON
object describing the event on stdin.

A hook runs only once the user has trusted it (~/.meltzer/trusted_hooks.toml).
Interactive sessions offer to trust new hooks at startup; `meltzer exec` skips
untrusted hooks.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import shlex
import subprocess
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from meltzer.tomlwrite import toml_string

SESSION_START = "SessionStart"
PRE_TOOL_USE = "PreToolUse"
POST_TOOL_USE = "PostToolUse"
STOP = "Stop"
HOOK_EVENTS = (SESSION_START, PRE_TOOL_USE, POST_TOOL_USE, STOP)
TOOL_EVENTS = frozenset({PRE_TOOL_USE, POST_TOOL_USE})

MATCH_ALL = "*"
TRUSTED_HOOKS_FILE = "trusted_hooks.toml"
HOOK_TIMEOUT_SECONDS = 60.0
# Exit status a PreToolUse hook uses to block the tool call.
BLOCK_EXIT_CODE = 2


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

    def applies_to(self, tool_name: str | None) -> bool:
        if self.event not in TOOL_EVENTS or self.matcher == MATCH_ALL:
            return True
        return tool_name in {name.strip() for name in self.matcher.split("|")}

    def describe(self) -> str:
        lines = [f"  event:   {self.event}"]
        if self.event in TOOL_EVENTS:
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


@dataclass(frozen=True)
class PreToolUseResult:
    blocked: bool = False
    reason: str = ""


@dataclass
class HookRunner:
    hooks: list[Hook]
    session_id: str
    root: Path
    transcript_path: Path
    notice: Callable[[str], None]
    timeout: float = HOOK_TIMEOUT_SECONDS

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
        )

    def for_subagent(self, session_id: str, transcript_path: Path) -> HookRunner:
        """The same hooks for a subagent session; trust was settled when the main session started."""
        return replace(self, hooks=list(self.hooks), session_id=session_id, transcript_path=transcript_path)

    def _payload(self, event: str, **fields: Any) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "transcript_path": str(self.transcript_path),
            "cwd": str(self.root),
            "hook_event_name": event,
            **fields,
        }

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["MELTZER_PROJECT_DIR"] = str(self.root)
        env["MELTZER_SESSION_ID"] = self.session_id
        return env

    def _execute(self, hook: Hook, payload: dict[str, Any]) -> subprocess.CompletedProcess[str] | None:
        label = f"{hook.event} hook {shlex.join(hook.command)}"
        try:
            completed = subprocess.run(
                list(hook.command),
                cwd=hook.cwd,
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                env=self._env(),
            )
        except subprocess.TimeoutExpired:
            self.notice(f"{label} timed out after {self.timeout:.0f} seconds")
            return None
        except OSError as exc:
            self.notice(f"{label} failed to start: {exc}")
            return None
        if completed.returncode != 0 and not (
            hook.event == PRE_TOOL_USE and completed.returncode == BLOCK_EXIT_CODE
        ):
            detail = completed.stderr.strip().splitlines()
            suffix = f": {detail[0]}" if detail else ""
            self.notice(f"{label} exited with status {completed.returncode}{suffix}")
        return completed

    def _matching(self, event: str, tool_name: str | None = None) -> list[Hook]:
        return [hook for hook in self.hooks if hook.event == event and hook.applies_to(tool_name)]

    def session_start(self) -> None:
        payload = self._payload(SESSION_START, source="startup")
        for hook in self._matching(SESSION_START):
            self._execute(hook, payload)

    def pre_tool_use(self, tool_name: str, arguments: str) -> PreToolUseResult:
        payload = self._payload(PRE_TOOL_USE, tool_name=tool_name, tool_input=_tool_input(arguments))
        for hook in self._matching(PRE_TOOL_USE, tool_name):
            completed = self._execute(hook, payload)
            if completed is None or completed.returncode != BLOCK_EXIT_CODE:
                continue
            reason = completed.stderr.strip() or f"blocked by {shlex.join(hook.command)}"
            return PreToolUseResult(blocked=True, reason=reason)
        return PreToolUseResult()

    def post_tool_use(self, tool_name: str, arguments: str, output: str, success: bool) -> None:
        payload = self._payload(
            POST_TOOL_USE,
            tool_name=tool_name,
            tool_input=_tool_input(arguments),
            tool_response={"output": output, "success": success},
        )
        for hook in self._matching(POST_TOOL_USE, tool_name):
            self._execute(hook, payload)

    def stop(self, last_assistant_message: str) -> None:
        payload = self._payload(STOP, last_assistant_message=last_assistant_message)
        for hook in self._matching(STOP):
            self._execute(hook, payload)


def _tool_input(arguments: str) -> Any:
    try:
        return json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError:
        return arguments
