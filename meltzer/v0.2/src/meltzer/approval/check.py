"""`meltzer execpolicy check`: show how rules files decide a command.

    meltzer execpolicy check --rules FILE [--rules FILE ...] [--pretty] -- CMD...

The command is split and matched as the `shell` and `exec_command` tools do.
A single CMD argument is read as a command line (`-- "cargo check && cargo test"`);
several arguments are the words of one command (`-- git commit -m "fix: x"`).

The result is printed as JSON:

    {"matchedRules":[{"prefixRuleMatch":{"matchedPrefix":["git","push"],
      "decision":"prompt","justification":"..."}}],"decision":"prompt"}

`matchedRules` lists every rule that matches a part of the command; `decision`
is the decision meltzer applies, and is left out when no rule applies. Rules
that are rejected when a file is loaded are reported on stderr.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

from meltzer.approval.rules import PrefixRule, RuleSet, parse_rules

EXIT_OK = 0
EXIT_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meltzer execpolicy", description="Work with prefix rules files.")
    commands = parser.add_subparsers(dest="action", required=True)
    check = commands.add_parser("check", help="check a command against rules files")
    check.add_argument(
        "-r",
        "--rules",
        action="append",
        required=True,
        type=Path,
        metavar="FILE",
        help="rules file to load; may be repeated",
    )
    check.add_argument("--pretty", action="store_true", help="pretty-print the JSON output")
    check.add_argument("command", nargs="+", metavar="CMD", help="the command to check (after --)")
    return parser


def command_line(tokens: list[str]) -> str:
    """The command line to check: a single argument as given, several arguments quoted as words."""
    if len(tokens) == 1:
        return tokens[0]
    return shlex.join(tokens)


def load_rules_files(paths: list[Path]) -> RuleSet:
    """Rules from the given files, in order. Raises OSError or UnicodeDecodeError if a file cannot be read."""
    rules: list[PrefixRule] = []
    warnings: list[str] = []
    for path in paths:
        found, problems = parse_rules(path.read_text(encoding="utf-8"), path)
        rules.extend(found)
        warnings.extend(problems)
    return RuleSet(rules, paths[0].parent, warnings)


def _rule_json(rule: PrefixRule) -> dict[str, Any]:
    match: dict[str, Any] = {"matchedPrefix": list(rule.pattern), "decision": rule.decision.value}
    if rule.justification:
        match["justification"] = rule.justification
    return {"prefixRuleMatch": match}


def check_command(rule_set: RuleSet, command: str) -> dict[str, Any]:
    """The matched rules and the decision for a command, as printed by `meltzer execpolicy check`."""
    result: dict[str, Any] = {"matchedRules": [_rule_json(rule) for rule in rule_set.matching_rules(command)]}
    match = rule_set.evaluate(command)
    if match is not None:
        result["decision"] = match.decision.value
    return result


def run_execpolicy(argv: list[str]) -> int:
    """Entry point for `meltzer execpolicy ...`; argv excludes `execpolicy`."""
    args = build_parser().parse_args(argv)
    try:
        rule_set = load_rules_files(args.rules)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"meltzer execpolicy check: cannot read rules file: {exc}", file=sys.stderr)
        return EXIT_ERROR
    for warning in rule_set.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    result = check_command(rule_set, command_line(args.command))
    if args.pretty:
        print(json.dumps(result, indent=2))
    else:
        print(json.dumps(result, separators=(",", ":")))
    return EXIT_OK
