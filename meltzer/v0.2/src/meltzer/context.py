"""Context items: what the session adds to the conversation besides user prompts.

Each builder returns a user-role history item whose text is wrapped in a tag
that names it:

  <environment_context>    workspace root, approval settings, sandbox mode,
                           network access and writable roots
  <user_shell_command>     a command the user ran with `!cmd`, its exit code and output

The item's kind says what it is; the monitor transcript labels items by kind
(see meltzer.monitor.transcript).

The environment item is added when a session starts and again whenever the
effective sandbox policy differs from the one last described (a grant, or the
end of a grant): see `environment_update`.
"""

from __future__ import annotations

from pathlib import Path

from meltzer.approval.policy import ApprovalSettings
from meltzer.history import History, Item, Kind, Role
from meltzer.sandbox.policy import SANDBOX_TEMP_DIR, SandboxPolicy

ENVIRONMENT_TAG = "environment_context"
USER_SHELL_COMMAND_TAG = "user_shell_command"

_XML_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}


def _escape(text: str) -> str:
    return "".join(_XML_ESCAPES.get(char, char) for char in text)


def _wrap(tag: str, body: str) -> str:
    return f"<{tag}>\n{body}\n</{tag}>"


def _sandbox_path(policy: SandboxPolicy, path: Path) -> str:
    """The path as sandboxed commands see it (the session's temporary directory is /tmp)."""
    if policy.temp_dir is not None and path == policy.temp_dir:
        return SANDBOX_TEMP_DIR.as_posix()
    return str(path)


def environment_context(policy: SandboxPolicy, settings: ApprovalSettings) -> Item:
    """The `<environment_context>` item for a sandbox policy and approval settings."""
    lines = [
        f"  <cwd>{_escape(str(policy.root))}</cwd>",
        f"  <approval_policy>{settings.policy.value}</approval_policy>",
        f"  <approvals_reviewer>{settings.reviewer.value}</approvals_reviewer>",
        f"  <sandbox_mode>{policy.mode.value}</sandbox_mode>",
        f"  <network_access>{policy.network.value}</network_access>",
    ]
    # The workspace root first, the temporary directory last.
    roots = sorted(
        policy.writable_roots() if policy.is_sandboxed else (),
        key=lambda root: (root != policy.root, root == policy.temp_dir, str(root)),
    )
    if roots:
        lines.append("  <writable_roots>")
        lines.extend(f"    <root>{_escape(_sandbox_path(policy, root))}</root>" for root in roots)
        lines.append("  </writable_roots>")
    return Item(Role.USER, Kind.ENVIRONMENT, content=_wrap(ENVIRONMENT_TAG, "\n".join(lines)))


def environment_update(history: History, policy: SandboxPolicy, settings: ApprovalSettings) -> Item | None:
    """A new environment item if the history's last one describes other settings, or has none; otherwise None."""
    item = environment_context(policy, settings)
    last = history.last_of_kind(Kind.ENVIRONMENT)
    if last is not None and last.content == item.content:
        return None
    return item


def user_shell_command(command: str, exit_code: int, output: str) -> Item:
    """The `<user_shell_command>` item for a command the user ran. The command and output text is escaped."""
    if output and not output.endswith("\n"):
        output += "\n"
    body = (
        f"<command>\n{_escape(command)}\n</command>\n"
        f"<result>\nExit code: {exit_code}\nOutput:\n{_escape(output)}</result>"
    )
    return Item(Role.USER, Kind.USER_SHELL_COMMAND, content=_wrap(USER_SHELL_COMMAND_TAG, body))
