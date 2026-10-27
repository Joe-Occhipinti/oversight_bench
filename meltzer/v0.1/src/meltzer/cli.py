"""Command line entry point.

  meltzer [options] [prompt]          interactive session (line-based)
  meltzer exec [options] <prompt>     run one prompt without a terminal and exit

Interactive commands: /plan (view or edit the task plan), /exit, /quit.
Ctrl-C cancels the running turn; at the prompt it ends the session.

The first interactive session in a directory with no trust decision asks
whether to trust it, and records the answer in ~/.meltzer/config.toml.
`meltzer exec` never asks; in an untrusted directory it runs without the
project config and project rules.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from meltzer import __version__
from meltzer.client import ModelError
from meltzer.config.loader import (
    ConfigError,
    load_config,
    parse_override,
    read_project_trust,
    user_config_path,
)
from meltzer.config.trust import ProjectsTableError, TrustLevel, record_project_trust, untrusted_notice
from meltzer.session import Session
from meltzer.tools.plan import PLAN_COMMAND, run_plan_command
from meltzer.ui import TerminalUI

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_INTERRUPTED = 130

EXIT_COMMANDS = {"/exit", "/quit"}


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
    unrecorded_trust = _ask_project_trust(root, ui) if ui.interactive else None
    config = load_config(root, overrides=cli_overrides(args), unrecorded_trust=unrecorded_trust)
    for warning in config.warnings:
        ui.info(f"warning: {warning}")
    if config.project_trust is not None and not config.project_trusted:
        ui.info(untrusted_notice(config.project_trust, user_config_path()))
    return Session(config, root, ui)


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


def run_interactive(args: argparse.Namespace) -> int:
    ui = TerminalUI(interactive=True)
    try:
        session = _start_session(args, ui)
    except ConfigError as exc:
        print(f"meltzer: config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    ui.info(f"meltzer {__version__}  model: {session.config.model}  sandbox: {session.policy.mode.value}  cwd: {session.root}")
    try:
        prompt = args.prompt
        while True:
            if prompt is None:
                prompt = ui.read_prompt()
                if prompt is None:
                    return EXIT_OK
            if prompt.strip() in EXIT_COMMANDS:
                return EXIT_OK
            command, _, rest = prompt.strip().partition(" ")
            if command == PLAN_COMMAND:
                ui.info(run_plan_command(session.plan, rest))
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


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    interactive_parser, exec_parser = build_parsers()
    try:
        if argv and argv[0] == "exec":
            return run_exec(exec_parser.parse_args(argv[1:]))
        return run_interactive(interactive_parser.parse_args(argv))
    except ConfigError as exc:
        print(f"meltzer: config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
