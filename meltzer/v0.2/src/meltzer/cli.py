"""Command line entry point.

  meltzer [options] [prompt]          interactive session (line-based, or full screen with tui.enabled)
  meltzer exec [options] <prompt>     run one prompt without a terminal and exit
  meltzer voice --input FILE.wav [options]   voice conversation from a recorded request
  meltzer resume [options] (ID | --last) [prompt]       continue a recorded session
  meltzer fork [options] (ID | --last) [prompt]         start a new session from a copy of one
  meltzer exec resume [options] (ID | --last) <prompt>  continue a recorded session with one prompt
  meltzer execpolicy check --rules FILE -- CMD...   show how rules files decide a command
  meltzer plugin add|install|enable ...    add marketplaces, install and enable plugins
  meltzer migrate [--dry-run] [-C DIR]  import Claude Code settings (asks about each item)

Interactive commands: /plan (view or edit the task plan), /rollback N (remove the last N
user turns from the conversation), /feedback [note] (send a feedback report, with
feedback.enabled), /exit, /quit. `!cmd` runs cmd with bash in the workspace
root, as the user and outside the sandbox, and adds it and its output to the conversation.
Ctrl-C cancels the running turn; at the prompt it ends the session.

The first interactive session in a directory with no trust decision asks
whether to trust it, and records the answer in ~/.meltzer/config.toml.
`meltzer exec` never asks; in an untrusted directory it runs without the
project config and project rules.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from meltzer import __version__
from meltzer.approval.check import run_execpolicy
from meltzer.client import ModelError
from meltzer.config.loader import (
    ConfigError,
    load_config,
    parse_override,
    read_project_trust,
    user_config_path,
)
from meltzer.config.trust import ProjectsTableError, TrustLevel, record_project_trust, untrusted_notice
from meltzer.migrate import run_migrate
from meltzer.plugins import run_plugin
from meltzer.realtime.bridge import run_voice
from meltzer.realtime.client import RealtimeError, read_wav
from meltzer.session import Session
from meltzer.sessions import ROLLBACK_COMMAND, StartMode, open_initial_history, run_rollback_command
from meltzer.telemetry.feedback import FEEDBACK_COMMAND, run_feedback_command
from meltzer.tools.plan import PLAN_COMMAND, run_plan_command
from meltzer.ui import TerminalUI
from meltzer.user_shell import run_user_shell_command, user_shell_command_text

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_INTERRUPTED = 130

EXIT_COMMANDS = {"/exit", "/quit"}

# Slash commands: name -> function(session, text after the name, ui) returning the text to show.
SLASH_COMMANDS: dict[str, Callable[[Session, str, Any], str]] = {
    PLAN_COMMAND: lambda session, rest, ui: run_plan_command(session.plan, rest),
    ROLLBACK_COMMAND: lambda session, rest, ui: run_rollback_command(session, rest),
    FEEDBACK_COMMAND: lambda session, rest, ui: run_feedback_command(session, rest, ui.confirm_feedback),
}


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-C", "--cd", metavar="DIR", help="run the session in DIR")
    parser.add_argument("-m", "--model", help="model to use")
    parser.add_argument(
        "-s", "--sandbox", choices=["read-only", "workspace-write", "full-access"], help="sandbox mode"
    )
    parser.add_argument("-a", "--approval-policy", choices=["on-request", "never"], help="approval policy")
    parser.add_argument(
        "-c",
        "--config",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override a config value (value parsed as TOML); may be repeated",
    )
    parser.add_argument(
        "--dangerously-bypass-approvals-and-sandbox",
        action="store_true",
        help="run every command without a sandbox and without approvals",
    )


def build_parsers() -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
    interactive = argparse.ArgumentParser(prog="meltzer", description="A terminal coding agent.")
    interactive.add_argument("--version", action="version", version=f"meltzer {__version__}")
    _add_common_options(interactive)
    interactive.add_argument("prompt", nargs="?", help="first prompt of the session")

    headless = argparse.ArgumentParser(prog="meltzer exec", description="Run one prompt non-interactively.")
    _add_common_options(headless)
    headless.add_argument("prompt", help="the prompt; '-' reads it from stdin")
    return interactive, headless


def build_voice_parser() -> argparse.ArgumentParser:
    voice = argparse.ArgumentParser(prog="meltzer voice", description="Hold a voice conversation from a WAV file.")
    _add_common_options(voice)
    voice.add_argument("--input", required=True, metavar="FILE.wav", help="24 kHz 16-bit mono PCM WAV file")
    return voice


def _add_session_arguments(parser: argparse.ArgumentParser, prompt_help: str) -> None:
    parser.add_argument("--last", action="store_true", help="the most recent session")
    parser.add_argument("session_id", nargs="?", metavar="ID", help="id of a recorded session")
    parser.add_argument("prompt", nargs="?", help=prompt_help)


def build_session_parsers() -> dict[str, argparse.ArgumentParser]:
    """Parsers of `meltzer resume`, `meltzer fork` and `meltzer exec resume`."""
    resume = argparse.ArgumentParser(prog="meltzer resume", description="Continue a recorded session.")
    fork = argparse.ArgumentParser(
        prog="meltzer fork", description="Start a new session from a copy of a recorded session."
    )
    exec_resume = argparse.ArgumentParser(
        prog="meltzer exec resume", description="Continue a recorded session with one prompt, non-interactively."
    )
    for parser in (resume, fork):
        _add_common_options(parser)
        _add_session_arguments(parser, "first prompt of the session")
    _add_common_options(exec_resume)
    _add_session_arguments(exec_resume, "the prompt; '-' reads it from stdin")
    return {"resume": resume, "fork": fork, "exec resume": exec_resume}


def _is_session_id(text: str | None) -> bool:
    try:
        return text is not None and str(uuid.UUID(text)) == text
    except ValueError:
        return False


def parse_session_args(
    parser: argparse.ArgumentParser, argv: list[str], mode: StartMode, *, require_prompt: bool
) -> argparse.Namespace:
    """Arguments of a session command: an id or --last, then the prompt."""
    args = parser.parse_args(argv)
    args.start_mode = mode
    if args.last:
        if args.prompt is not None or _is_session_id(args.session_id):
            parser.error("give a session id or --last, not both")
        args.session_id, args.prompt = None, args.session_id
    elif args.session_id is None:
        parser.error("give a session id or --last")
    if require_prompt and args.prompt is None:
        parser.error("the prompt is required")
    return args


def cli_overrides(args: argparse.Namespace) -> list[tuple[str, Any]]:
    """-c overrides first, then flags, so flags win."""
    overrides = [parse_override(text) for text in args.config]
    if args.model:
        overrides.append(("model", args.model))
    if args.sandbox:
        overrides.append(("sandbox_mode", args.sandbox))
    if args.approval_policy:
        overrides.append(("approval_policy", args.approval_policy))
    if args.dangerously_bypass_approvals_and_sandbox:
        overrides.append(("sandbox_mode", "full-access"))
        overrides.append(("approval_policy", "never"))
    return overrides


def _session_root(args: argparse.Namespace) -> Path:
    root = Path(args.cd).expanduser() if args.cd else Path(os.getcwd())
    if not root.is_dir():
        raise ConfigError(f"directory does not exist: {root}")
    return root.resolve()


def _ask_project_trust(root: Path, ui: TerminalUI) -> TrustLevel | None:
    """Ask whether to trust root if no decision is recorded for it, and record the answer.

    Returns the answer if it could not be recorded, so it still applies to this session.
    """
    trust = read_project_trust(root)
    if trust.recorded:
        return None
    answer = ui.confirm_project_trust(trust.root, root)
    if answer is None:
        return None
    level = TrustLevel.TRUSTED if answer else TrustLevel.UNTRUSTED
    path = user_config_path()
    try:
        record_project_trust(path, trust.root, level)
    except (OSError, ProjectsTableError) as exc:
        ui.info(f"warning: could not record the decision in {path}: {exc}; it applies to this session only")
        return level
    return None


def _start_session(args: argparse.Namespace, ui: TerminalUI) -> Session:
    root = _session_root(args)
    # Set by `meltzer resume`, `meltzer fork` and `meltzer exec resume`.
    mode = getattr(args, "start_mode", None)
    initial = open_initial_history(mode, args.session_id, cwd=root) if mode is not None else None
    unrecorded_trust = _ask_project_trust(root, ui) if ui.interactive else None
    config = load_config(root, overrides=cli_overrides(args), unrecorded_trust=unrecorded_trust)
    for warning in config.warnings:
        ui.info(f"warning: {warning}")
    if config.project_trust is not None and not config.project_trusted:
        ui.info(untrusted_notice(config.project_trust, user_config_path()))
    if initial is not None and initial.meta.get("cwd") != str(root):
        ui.info(f"note: session {initial.session_id} was recorded in {initial.meta.get('cwd')}; it continues in {root}")
    return Session(config, root, ui, initial=initial)


def run_exec(args: argparse.Namespace) -> int:
    prompt = sys.stdin.read() if args.prompt == "-" else args.prompt
    if not prompt.strip():
        print("meltzer exec: empty prompt", file=sys.stderr)
        return EXIT_CONFIG
    ui = TerminalUI(interactive=False)
    try:
        session = _start_session(args, ui)
    except ConfigError as exc:
        print(f"meltzer: config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        final = session.run_turn(prompt)
        ui.final_message(final)
        return EXIT_OK
    except ModelError as exc:
        print(f"meltzer: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("meltzer: interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    finally:
        session.close()


def run_voice_command(args: argparse.Namespace) -> int:
    try:
        pcm = read_wav(Path(args.input))
    except RealtimeError as exc:
        print(f"meltzer voice: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    ui = TerminalUI(interactive=False)
    try:
        session = _start_session(args, ui)
    except ConfigError as exc:
        print(f"meltzer: config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        ui.final_message(run_voice(session, pcm, ui))
        return EXIT_OK
    except ConfigError as exc:
        print(f"meltzer: config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except (RealtimeError, ModelError) as exc:
        print(f"meltzer: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("meltzer: interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    finally:
        session.close()


def run_interactive(args: argparse.Namespace) -> int:
    try:
        tui_enabled = load_config(_session_root(args), overrides=cli_overrides(args)).tui_enabled
    except ConfigError as exc:
        print(f"meltzer: config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if not tui_enabled:
        return _interactive_loop(args, TerminalUI(interactive=True))
    try:
        from meltzer.tui import FullScreenUI
    except ImportError:
        print("meltzer: tui.enabled needs the tui extra: pip install 'meltzer[tui]'", file=sys.stderr)
        return EXIT_CONFIG
    ui = FullScreenUI()

    def start(args: argparse.Namespace, ui: Any) -> Session:
        return ui.attach_session(_start_session(args, ui))

    return ui.run(lambda: _interactive_loop(args, ui, start))


def _interactive_loop(args: argparse.Namespace, ui: Any, start: Callable[..., Session] | None = None) -> int:
    try:
        session = (start or _start_session)(args, ui)
    except ConfigError as exc:
        print(f"meltzer: config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    ui.info(f"meltzer {__version__}  model: {session.config.model}  sandbox: {session.policy.mode.value}  cwd: {session.root}")
    try:
        prompt = args.prompt
        while True:
            if prompt is None:
                with session.agent_control.prompt_lock:
                    prompt = ui.read_prompt()
                if prompt is None:
                    return EXIT_OK
            if prompt.strip() in EXIT_COMMANDS:
                return EXIT_OK
            shell_command = user_shell_command_text(prompt)
            if shell_command is not None:
                ui.info(run_user_shell_command(session, shell_command))
                prompt = None
                continue
            command, _, rest = prompt.strip().partition(" ")
            if command in SLASH_COMMANDS:
                ui.info(SLASH_COMMANDS[command](session, rest, ui))
                prompt = None
                continue
            try:
                reply = session.run_turn(prompt)
            except KeyboardInterrupt:
                ui.info("turn interrupted")
            except ModelError as exc:
                ui.info(f"error: {exc}")
            else:
                if reply:
                    ui.assistant_message(reply)
            prompt = None
    except KeyboardInterrupt:
        ui.info("interrupted")
        return EXIT_OK
    finally:
        session.close()


@contextmanager
def _end_on_termination_signals() -> Iterator[None]:
    """Make SIGTERM and SIGHUP end the process with SystemExit, so open sessions are closed."""

    def handler(signum: int, frame: Any) -> None:
        signal.signal(signum, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    previous: dict[int, Any] = {}
    try:
        for number in filter(None, (signal.SIGTERM, getattr(signal, "SIGHUP", None))):
            previous[number] = signal.signal(number, handler)
    except ValueError:  # not the main thread
        pass
    try:
        yield
    finally:
        for number, old in previous.items():
            signal.signal(number, old)


def main(argv: list[str] | None = None) -> int:
    with _end_on_termination_signals():
        return _main(list(sys.argv[1:] if argv is None else argv))


def _main(argv: list[str]) -> int:
    interactive_parser, exec_parser = build_parsers()
    session_parsers = build_session_parsers()
    try:
        if argv and argv[0] == "migrate":
            return run_migrate(argv[1:])
        if argv and argv[0] == "plugin":
            return run_plugin(argv[1:])
        if argv and argv[0] == "execpolicy":
            return run_execpolicy(argv[1:])
        if argv and argv[0] == "voice":
            return run_voice_command(build_voice_parser().parse_args(argv[1:]))
        if argv and argv[0] in ("resume", "fork"):
            mode = StartMode.RESUMED if argv[0] == "resume" else StartMode.FORKED
            return run_interactive(parse_session_args(session_parsers[argv[0]], argv[1:], mode, require_prompt=False))
        if argv[:2] == ["exec", "resume"]:
            parser = session_parsers["exec resume"]
            return run_exec(parse_session_args(parser, argv[2:], StartMode.RESUMED, require_prompt=True))
        if argv and argv[0] == "exec":
            return run_exec(exec_parser.parse_args(argv[1:]))
        return run_interactive(interactive_parser.parse_args(argv))
    except ConfigError as exc:
        print(f"meltzer: config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
