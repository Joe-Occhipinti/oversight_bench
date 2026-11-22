"""What the exit status and output of a hook mean for each event.

Exit status 0: stdout may hold one JSON object with the fields below. Empty
stdout has no effect. For SessionStart, SubagentStart and UserPromptSubmit,
stdout that is not JSON is added to the conversation as context.

Exit status 2: for PreToolUse, PermissionRequest, PostToolUse,
UserPromptSubmit, Stop and SubagentStop, the hook blocks (a PermissionRequest
hook denies the request), and stderr is the reason.

Any other exit status, JSON that is not an object, a field of the wrong type
or a field the event does not support: the hook has no effect, and a notice
says why.

Fields for every event: `continue` (false stops, for the events that support
it), `stopReason`, `systemMessage` (shown to the user, not to the model) and
`suppressOutput`. Event fields go in `hookSpecificOutput` (with an optional
`hookEventName`), and `decision` and `reason` at the top level:

  PreToolUse         hookSpecificOutput.permissionDecision "deny" with
                     permissionDecisionReason blocks the call; "allow" with
                     updatedInput replaces the call's arguments;
                     decision "block" with reason blocks; additionalContext
  PermissionRequest  hookSpecificOutput.decision.behavior "allow" or "deny",
                     with an optional message
  PostToolUse        decision "block" with reason replaces the result with an
                     error; continue false replaces the result with reason (or
                     stopReason); additionalContext
  UserPromptSubmit   decision "block" with reason, or continue false: the
                     prompt is not added; additionalContext
  Stop, SubagentStop decision "block" with reason continues the turn with the
                     reason as a prompt; continue false ends it
  PreCompact, PostCompact   continue false ends the turn
  SessionStart, SubagentStart   additionalContext
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Callable

from meltzer.history import Item, Kind, Role

SESSION_START = "SessionStart"
SESSION_END = "SessionEnd"
USER_PROMPT_SUBMIT = "UserPromptSubmit"
PRE_TOOL_USE = "PreToolUse"
PERMISSION_REQUEST = "PermissionRequest"
POST_TOOL_USE = "PostToolUse"
PRE_COMPACT = "PreCompact"
POST_COMPACT = "PostCompact"
SUBAGENT_START = "SubagentStart"
SUBAGENT_STOP = "SubagentStop"
STOP = "Stop"

# Exit status with which a hook blocks.
BLOCK_EXIT_CODE = 2
# Characters of one hook text that the model sees. Longer text is shortened to
# its start and end; the full text is kept in the session log.
HOOK_OUTPUT_LIMIT = 10_000

# Events whose hooks block when they exit with status 2.
BLOCKING_EVENTS = frozenset({PRE_TOOL_USE, PERMISSION_REQUEST, POST_TOOL_USE, USER_PROMPT_SUBMIT, STOP, SUBAGENT_STOP})
# Events whose hooks add plain-text stdout to the conversation.
TEXT_CONTEXT_EVENTS = frozenset({SESSION_START, SUBAGENT_START, USER_PROMPT_SUBMIT})
# Events whose hooks must print JSON when they print anything.
JSON_ONLY_EVENTS = frozenset({STOP, SUBAGENT_STOP})


@dataclass(frozen=True)
class HookOutcome:
    """What the hooks of an event asked for."""

    # PreToolUse: the call does not run. PermissionRequest: the request is denied.
    # PostToolUse: the result is replaced by an error with `reason`.
    # UserPromptSubmit: the prompt is not added. Stop, SubagentStop: the turn
    # continues with `reason` as a prompt.
    blocked: bool = False
    reason: str = ""
    # Text to add to the conversation, one entry per hook.
    additional_context: tuple[str, ...] = ()
    # PermissionRequest: "allow" or "deny".
    decision: str | None = None
    # PreToolUse: arguments that replace the arguments of the tool call.
    updated_input: dict[str, Any] | None = None
    # `continue: false`. PostToolUse: the result is replaced by `reason`.
    # UserPromptSubmit: the prompt is not added. Other events: the turn ends.
    stop: bool = False
    # For the output of one hook: its systemMessage, and why the output was ignored.
    system_message: str = ""
    error: str = ""


class _Ignored(Exception):
    """The output does not fit the event; the hook has no effect."""


def _field(output: dict[str, Any], key: str, kind: type, description: str) -> Any:
    value = output.get(key)
    if value is not None and not isinstance(value, kind):
        raise _Ignored(f"'{key}' must be {description}")
    return value


def _text(output: dict[str, Any], key: str) -> str | None:
    return _field(output, key, str, "a string")


def _flag(output: dict[str, Any], key: str) -> bool | None:
    return _field(output, key, bool, "true or false")


def _object(output: dict[str, Any], key: str) -> dict[str, Any] | None:
    return _field(output, key, dict, "an object")


def _universal(output: dict[str, Any]) -> tuple[bool, str, bool]:
    """`continue`, `stopReason` and `suppressOutput`."""
    proceed = _flag(output, "continue") is not False
    stop_reason = (_text(output, "stopReason") or "").strip()
    suppress = bool(_flag(output, "suppressOutput"))
    return proceed, stop_reason, suppress


def _specific(output: dict[str, Any], event: str) -> dict[str, Any]:
    specific = _object(output, "hookSpecificOutput") or {}
    name = _text(specific, "hookEventName")
    if name is not None and name != event:
        raise _Ignored(f"hookSpecificOutput is for {name}, not {event}")
    return specific


def _context(specific: dict[str, Any]) -> tuple[str, ...]:
    text = _text(specific, "additionalContext")
    return (text.strip(),) if text and text.strip() else ()


def _block_reason(output: dict[str, Any]) -> str | None:
    """The reason of `decision: "block"`; None when the output does not block."""
    decision = _text(output, "decision")
    if decision is None:
        return None
    if decision != "block":
        raise _Ignored(f"unsupported decision:{decision}")
    reason = (_text(output, "reason") or "").strip()
    if not reason:
        raise _Ignored("decision:block without a non-empty reason")
    return reason


def _no_stop(output: dict[str, Any]) -> None:
    """For events that cannot stop: reject the fields that would."""
    proceed, stop_reason, suppress = _universal(output)
    if not proceed:
        raise _Ignored("unsupported continue:false")
    if "stopReason" in output:
        raise _Ignored("unsupported stopReason")
    if suppress:
        raise _Ignored("unsupported suppressOutput")


def _pre_tool_use(output: dict[str, Any]) -> HookOutcome:
    _no_stop(output)
    specific = _specific(output, PRE_TOOL_USE)
    context = _context(specific)
    decision = _text(specific, "permissionDecision")
    decision_reason = _text(specific, "permissionDecisionReason")
    updated = specific.get("updatedInput")
    if decision is None and decision_reason is None and updated is None:
        reason = _block_reason(output)
        if reason is None and "reason" in output:
            raise _Ignored("reason without decision")
        return HookOutcome(blocked=reason is not None, reason=reason or "", additional_context=context)
    if updated is not None and decision != "allow":
        raise _Ignored("updatedInput without permissionDecision:allow")
    if decision == "allow":
        if not isinstance(updated, dict):
            raise _Ignored("permissionDecision:allow without an updatedInput object")
        return HookOutcome(additional_context=context, updated_input=updated)
    if decision == "deny":
        reason = (decision_reason or "").strip()
        if not reason:
            raise _Ignored("permissionDecision:deny without a non-empty permissionDecisionReason")
        return HookOutcome(blocked=True, reason=reason, additional_context=context)
    if decision is None:
        raise _Ignored("permissionDecisionReason without permissionDecision")
    raise _Ignored(f"unsupported permissionDecision:{decision}")


def _permission_request(output: dict[str, Any]) -> HookOutcome:
    _no_stop(output)
    decision = _object(_specific(output, PERMISSION_REQUEST), "decision")
    if decision is None:
        return HookOutcome()
    for key in ("updatedInput", "updatedPermissions"):
        if key in decision:
            raise _Ignored(f"unsupported {key}")
    if _flag(decision, "interrupt"):
        raise _Ignored("unsupported interrupt:true")
    behavior = _text(decision, "behavior")
    if behavior == "allow":
        return HookOutcome(decision="allow")
    if behavior == "deny":
        return HookOutcome(blocked=True, decision="deny", reason=(_text(decision, "message") or "").strip())
    raise _Ignored(f"unsupported decision.behavior: {behavior!r}")


def _post_tool_use(output: dict[str, Any]) -> HookOutcome:
    proceed, stop_reason, suppress = _universal(output)
    if suppress:
        raise _Ignored("unsupported suppressOutput")
    specific = _specific(output, POST_TOOL_USE)
    if "updatedMCPToolOutput" in specific:
        raise _Ignored("unsupported updatedMCPToolOutput")
    context = _context(specific)
    reason = (_text(output, "reason") or "").strip()
    if not proceed:
        return HookOutcome(stop=True, reason=reason or stop_reason, additional_context=context)
    block = _block_reason(output)
    if block is None and "reason" in output:
        raise _Ignored("reason without decision")
    return HookOutcome(blocked=block is not None, reason=block or "", additional_context=context)


def _user_prompt_submit(output: dict[str, Any]) -> HookOutcome:
    proceed, stop_reason, _ = _universal(output)
    context = _context(_specific(output, USER_PROMPT_SUBMIT))
    if not proceed:
        return HookOutcome(stop=True, reason=stop_reason, additional_context=context)
    block = _block_reason(output)
    return HookOutcome(blocked=block is not None, reason=block or "", additional_context=context)


def _stop(event: str) -> Callable[[dict[str, Any]], HookOutcome]:
    def parse(output: dict[str, Any]) -> HookOutcome:
        proceed, stop_reason, _ = _universal(output)
        _specific(output, event)
        if not proceed:
            return HookOutcome(stop=True, reason=stop_reason)
        block = _block_reason(output)
        return HookOutcome(blocked=block is not None, reason=block or "")

    return parse


def _compact(event: str) -> Callable[[dict[str, Any]], HookOutcome]:
    def parse(output: dict[str, Any]) -> HookOutcome:
        proceed, stop_reason, _ = _universal(output)
        _specific(output, event)
        return HookOutcome(stop=not proceed, reason="" if proceed else stop_reason)

    return parse


def _start(event: str) -> Callable[[dict[str, Any]], HookOutcome]:
    def parse(output: dict[str, Any]) -> HookOutcome:
        _universal(output)
        return HookOutcome(additional_context=_context(_specific(output, event)))

    return parse


def _session_end(output: dict[str, Any]) -> HookOutcome:
    _universal(output)
    _specific(output, SESSION_END)
    return HookOutcome()


_PARSERS: dict[str, Callable[[dict[str, Any]], HookOutcome]] = {
    SESSION_START: _start(SESSION_START),
    SESSION_END: _session_end,
    USER_PROMPT_SUBMIT: _user_prompt_submit,
    PRE_TOOL_USE: _pre_tool_use,
    PERMISSION_REQUEST: _permission_request,
    POST_TOOL_USE: _post_tool_use,
    PRE_COMPACT: _compact(PRE_COMPACT),
    POST_COMPACT: _compact(POST_COMPACT),
    SUBAGENT_START: _start(SUBAGENT_START),
    SUBAGENT_STOP: _stop(SUBAGENT_STOP),
    STOP: _stop(STOP),
}


def parse_output(event: str, returncode: int, stdout: str, stderr: str) -> HookOutcome:
    """What one hook of `event` asked for, from its exit status, stdout and stderr."""
    if returncode == BLOCK_EXIT_CODE and event in BLOCKING_EVENTS:
        reason = stderr.strip()
        # A PreToolUse hook blocks even without a reason; the caller names the hook instead.
        if not reason and event != PRE_TOOL_USE:
            return HookOutcome(error="exited with status 2 without a reason on stderr")
        return HookOutcome(blocked=True, reason=reason, decision="deny" if event == PERMISSION_REQUEST else None)
    if returncode != 0:
        detail = stderr.strip().splitlines()
        suffix = f": {detail[0]}" if detail else ""
        return HookOutcome(error=f"exited with status {returncode}{suffix}")
    text = stdout.strip()
    if not text:
        return HookOutcome()
    try:
        output = json.loads(text)
    except json.JSONDecodeError:
        output = None
    if not isinstance(output, dict):
        if text.startswith(("{", "[")) or event in JSON_ONLY_EVENTS:
            return HookOutcome(error="output ignored: invalid JSON")
        if event in TEXT_CONTEXT_EVENTS:
            return HookOutcome(additional_context=(text,))
        return HookOutcome()
    try:
        message = (_text(output, "systemMessage") or "").strip()
    except _Ignored as exc:
        return HookOutcome(error=f"output ignored: {exc}")
    parser = _PARSERS.get(event)
    if parser is None:
        return HookOutcome(system_message=message)
    try:
        outcome = parser(output)
    except _Ignored as exc:
        return HookOutcome(system_message=message, error=f"output ignored: {exc}")
    return replace(outcome, system_message=message)


def preview(text: str, limit: int) -> str:
    """`text` cut to `limit` characters, keeping its start and its end."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    omitted = len(text) - limit
    note = f"[... {omitted} characters not shown; the full hook output is in the session log]"
    return f"{text[:head]}\n{note}\n{text[len(text) - tail:]}"


def context_item(event: str, text: str) -> Item:
    """The conversation item for context that a hook of `event` added."""
    return Item(Role.USER, Kind.HOOK_CONTEXT, content=text, source=f"hook:{event}")


def prompt_item(event: str, text: str) -> Item:
    """The conversation item for the prompt with which a Stop or SubagentStop hook continues a turn."""
    return Item(Role.USER, Kind.HOOK_PROMPT, content=text, source=f"hook:{event}")
