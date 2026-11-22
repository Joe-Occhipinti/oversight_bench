"""Shell commands the user runs from the interactive prompt with `!cmd`.

The command runs as the user, outside the sandbox, with `bash -c` in the
session root and the shell tool's timeout. Its output is shown, and the command,
exit code and output are added to the conversation as a user shell command
item (see meltzer.context). No model turn starts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from meltzer.utils.truncate import truncate_output

if TYPE_CHECKING:
    from meltzer.session import Session

USER_SHELL_PREFIX = "!"
USER_SHELL_USAGE = "usage: !<command> runs a shell command in the workspace root"


def user_shell_command_text(prompt: str) -> str | None:
    """The command of a `!cmd` prompt, or None for any other prompt."""
    text = prompt.strip()
    if not text.startswith(USER_SHELL_PREFIX):
        return None
    return text[len(USER_SHELL_PREFIX) :].strip()


def run_user_shell_command(session: Session, command: str) -> str:
    """Run a command the user typed after `!` and return what to show them."""
    if not command:
        return USER_SHELL_USAGE
    try:
        result = session.runner.run(command, session.root, sandboxed=False)
    except OSError as exc:
        return f"command not run: failed to start process: {exc}"
    except KeyboardInterrupt:
        return "command interrupted"
    output = truncate_output(result.output)
    if result.timed_out:
        output = f"Command timed out after {session.runner.timeout:g} seconds\n{output}"
    session.add_user_shell_command(command, result.exit_code, output)
    return f"{output.rstrip()}\nExit code: {result.exit_code}".lstrip("\n")
