"""Prefix rules for shell commands.

Rules files use a small Starlark subset, one call per rule:

    prefix_rule(pattern=["cargo", "check"], decision="allow")
    prefix_rule(pattern=["git", ["status", "diff", "log"]])
    prefix_rule(pattern=["rm", "-rf"], decision="forbidden", justification="Use trash instead.")

`pattern` lists the leading tokens of a command; a list element gives
alternatives for that position. `decision` is "allow" (the default) or
"forbidden". `justification` is shown when a forbidden rule rejects a command.

"Always allow" answers are stored in ~/.meltzer/rules/default.rules.
"""

from __future__ import annotations

import ast
import itertools
import json
import posixpath
import re
import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from meltzer.config.loader import project_search_dirs
from meltzer.sandbox.policy import resolve

RULES_DIR_NAME = "rules"
RULES_SUFFIX = ".rules"
DEFAULT_RULES_FILE = "default.rules"

# Shells and interpreters.
INTERPRETERS: frozenset[str] = frozenset(
    {
        "bash", "sh", "zsh", "dash", "ksh", "fish",
        "python", "python3",
        "node", "nodejs", "deno", "bun",
        "perl", "ruby", "php", "lua", "Rscript", "julia",
    }
)

# Programs that run another command given later on the command line.
WRAPPERS: frozenset[str] = frozenset(
    {
        "npx", "uvx",
        "env", "sudo", "su", "doas",
        "xargs", "exec", "eval", "nohup", "setsid", "timeout",
    }
)

BANNED_PREFIXES: frozenset[tuple[str, ...]] = frozenset(
    {
        ("git",),
        ("npm", "run"), ("npm", "exec"),
        ("pnpm", "run"), ("pnpm", "exec"),
        ("yarn", "run"),
        ("uv", "run"),
    }
)

VERSION_SUFFIX = re.compile(r"[0-9][0-9.]*$")

# Operators that join simple commands; each part is matched on its own.
COMMAND_SEPARATORS = frozenset({"&&", "||", ";", "|"})
# Characters that make a token depend on shell expansion.
EXPANSION_CHARS = frozenset("$`")
# Unquoted, these characters form operators (separators, redirections, subshells).
PUNCTUATION_CHARS = frozenset("();<>|&")
WORD_SEPARATORS = frozenset(" \t")
# Characters a backslash escapes inside double quotes; before others it stays.
DOUBLE_QUOTE_ESCAPES = frozenset('$`"\\')


class RuleDecision(StrEnum):
    ALLOW = "allow"
    FORBIDDEN = "forbidden"


@dataclass(frozen=True)
class PrefixRule:
    pattern: tuple[str, ...]
    decision: RuleDecision
    source: str
    justification: str = ""

    def matches(self, tokens: tuple[str, ...]) -> bool:
        return tokens[: len(self.pattern)] == self.pattern

    def describe(self) -> str:
        return f"{shlex.join(self.pattern)} ({self.source})"


@dataclass(frozen=True)
class RuleMatch:
    decision: RuleDecision
    rules: tuple[PrefixRule, ...]

    def describe(self) -> str:
        return "; ".join(rule.describe() for rule in self.rules)


def is_banned(pattern: tuple[str, ...]) -> bool:
    """True if an allow rule or "always allow" answer for this prefix is refused."""
    if not pattern:
        return False
    program = program_name(pattern[0])
    if program in WRAPPERS:
        return True
    if program in INTERPRETERS and all(token.startswith("-") for token in pattern[1:]):
        return True
    return (posixpath.basename(pattern[0]), *pattern[1:]) in BANNED_PREFIXES


def program_name(token: str) -> str:
    """File name without a version suffix: /usr/bin/python3.12 -> python3."""
    name = posixpath.basename(token)
    if name in INTERPRETERS or name in WRAPPERS:
        return name
    stripped = VERSION_SUFFIX.sub("", name)
    return stripped if stripped in INTERPRETERS or stripped in WRAPPERS else name


def shell_tokens(command: str) -> list[tuple[str, bool]] | None:
    """Words and operators of a one-line command, as (text, is_operator) pairs.

    Quotes and backslashes are removed from words as bash does; nothing is
    expanded. An operator is a run of unquoted PUNCTUATION_CHARS, so `echo ";"`
    is two words. Returns None if a quote is not closed or the line ends with a
    backslash.
    """
    tokens: list[tuple[str, bool]] = []
    word: list[str] = []
    # True once a word has started; a word can be empty, as in `""`.
    in_word = False
    i = 0
    while i < len(command):
        char = command[i]
        if char in WORD_SEPARATORS or char in PUNCTUATION_CHARS:
            if in_word:
                tokens.append(("".join(word), False))
                word, in_word = [], False
            if char in WORD_SEPARATORS:
                i += 1
                continue
            end = i
            while end < len(command) and command[end] in PUNCTUATION_CHARS:
                end += 1
            tokens.append((command[i:end], True))
            i = end
            continue
        in_word = True
        if char == "\\":
            if i + 1 == len(command):
                return None
            word.append(command[i + 1])
            i += 2
        elif char == "'":
            end = command.find("'", i + 1)
            if end < 0:
                return None
            word.append(command[i + 1 : end])
            i = end + 1
        elif char == '"':
            i += 1
            while i < len(command) and command[i] != '"':
                if command[i] == "\\" and i + 1 < len(command) and command[i + 1] in DOUBLE_QUOTE_ESCAPES:
                    i += 1
                word.append(command[i])
                i += 1
            if i == len(command):
                return None
            i += 1
        else:
            word.append(char)
            i += 1
    if in_word:
        tokens.append(("".join(word), False))
    return tokens


def split_command(command: str) -> list[tuple[str, ...]] | None:
    """Split a command line into simple commands, or None if it uses other shell syntax."""
    if "\n" in command or "\r" in command:
        return None
    tokens = shell_tokens(command)
    if tokens is None:
        return None
    parts: list[tuple[str, ...]] = []
    current: list[str] = []
    for text, is_operator in tokens:
        if is_operator:
            if text not in COMMAND_SEPARATORS or not current:
                return None
            parts.append(tuple(current))
            current = []
            continue
        if any(ch in EXPANSION_CHARS for ch in text):
            return None
        current.append(text)
    if not current:
        return None
    parts.append(tuple(current))
    return parts


class RulesFileError(ValueError):
    """A rule definition is invalid."""


def _pattern_alternatives(value: Any) -> list[tuple[str, ...]]:
    if not isinstance(value, list) or not value:
        raise RulesFileError("'pattern' must be a non-empty list")
    positions: list[list[str]] = []
    for element in value:
        if isinstance(element, str) and element:
            positions.append([element])
        elif (
            isinstance(element, list)
            and element
            and all(isinstance(alt, str) and alt for alt in element)
        ):
            positions.append(list(element))
        else:
            raise RulesFileError("'pattern' elements must be non-empty strings or lists of strings")
    return [tuple(combo) for combo in itertools.product(*positions)]


def _literal_kwargs(call: ast.Call) -> dict[str, Any]:
    if call.args:
        raise RulesFileError("prefix_rule takes keyword arguments only")
    kwargs: dict[str, Any] = {}
    for keyword in call.keywords:
        if keyword.arg is None:
            raise RulesFileError("prefix_rule does not accept ** arguments")
        try:
            kwargs[keyword.arg] = ast.literal_eval(keyword.value)
        except Exception:
            raise RulesFileError(f"'{keyword.arg}' must be a literal value") from None
    return kwargs


def _rules_from_call(call: ast.Call, source: str) -> list[PrefixRule]:
    kwargs = _literal_kwargs(call)
    unknown = set(kwargs) - {"pattern", "decision", "justification"}
    if unknown:
        raise RulesFileError(f"unknown argument(s): {', '.join(sorted(unknown))}")
    if "pattern" not in kwargs:
        raise RulesFileError("'pattern' is required")
    try:
        decision = RuleDecision(kwargs.get("decision", RuleDecision.ALLOW.value))
    except ValueError:
        raise RulesFileError("'decision' must be \"allow\" or \"forbidden\"") from None
    justification = kwargs.get("justification", "")
    if not isinstance(justification, str):
        raise RulesFileError("'justification' must be a string")
    patterns = _pattern_alternatives(kwargs["pattern"])
    if decision == RuleDecision.ALLOW:
        banned = [p for p in patterns if is_banned(p)]
        if banned:
            raise RulesFileError(
                f"pattern {shlex.join(banned[0])!r} is too broad for an allow rule; use a longer prefix"
            )
    return [PrefixRule(pattern, decision, source, justification) for pattern in patterns]


# A line that starts a new top-level call, such as `prefix_rule(`.
STATEMENT_START = re.compile(r"[A-Za-z_]\w*\s*\(")


def _statements(text: str, path: Path) -> tuple[list[ast.stmt], list[str]]:
    """Top-level statements of a rules file.

    If the file does not parse as a whole, it is split at every line that
    starts a call, and each part is parsed on its own, so a syntax error only
    drops the rule it occurs in.
    """
    try:
        return ast.parse(text, filename=str(path)).body, []
    except SyntaxError:
        lines = text.splitlines(keepends=True)
    starts = [0] + [n for n, line in enumerate(lines) if n > 0 and STATEMENT_START.match(line)]
    statements: list[ast.stmt] = []
    warnings: list[str] = []
    for first, end in zip(starts, [*starts[1:], len(lines)]):
        chunk = "".join(lines[first:end])
        try:
            tree = ast.parse(chunk, filename=str(path))
        except SyntaxError as exc:
            line = first + (exc.lineno or 1)
            warnings.append(f"{path}:{line}: syntax error, rule ignored: {exc.msg}")
            continue
        ast.increment_lineno(tree, first)
        statements.extend(tree.body)
    return statements, warnings


def parse_rules(text: str, path: Path) -> tuple[list[PrefixRule], list[str]]:
    """Parse one rules file. Returns (rules, warnings); invalid entries are skipped."""
    statements, warnings = _statements(text, path)
    rules: list[PrefixRule] = []
    for statement in statements:
        source = f"{path}:{statement.lineno}"
        call = statement.value if isinstance(statement, ast.Expr) else None
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)):
            warnings.append(f"{source}: expected a prefix_rule(...) call; ignored")
            continue
        if call.func.id != "prefix_rule":
            warnings.append(f"{source}: unknown function '{call.func.id}'; ignored")
            continue
        try:
            rules.extend(_rules_from_call(call, source))
        except RulesFileError as exc:
            warnings.append(f"{source}: rule rejected: {exc}")
    return rules, warnings


def _rules_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.suffix == RULES_SUFFIX and p.is_file())


def find_project_rules_dir(cwd: Path, user_dir: Path, home: Path | None = None) -> Path | None:
    """The project rules directory for cwd."""
    user = resolve(user_dir)
    for directory in project_search_dirs(cwd, home):
        candidate = directory / ".meltzer" / RULES_DIR_NAME
        if candidate.is_dir() and resolve(candidate) != user:
            return candidate
    return None


def format_rule(pattern: tuple[str, ...], decision: RuleDecision = RuleDecision.ALLOW) -> str:
    tokens = ", ".join(json.dumps(token) for token in pattern)
    return f"prefix_rule(pattern=[{tokens}], decision={json.dumps(decision.value)})\n"


class RuleSet:
    def __init__(self, rules: list[PrefixRule], user_dir: Path, warnings: list[str] | None = None):
        self.rules = list(rules)
        self.user_dir = user_dir
        self.warnings = list(warnings or [])

    @classmethod
    def load(
        cls,
        user_dir: Path,
        cwd: Path,
        *,
        include_project: bool = False,
        home: Path | None = None,
    ) -> RuleSet:
        """User rules, plus the nearest project rules directory when include_project is set."""
        directories = [user_dir]
        project_dir = find_project_rules_dir(cwd, user_dir, home) if include_project else None
        if project_dir is not None:
            directories.append(project_dir)
        rules: list[PrefixRule] = []
        warnings: list[str] = []
        for directory in directories:
            try:
                paths = _rules_files(directory)
            except OSError as exc:
                warnings.append(f"{directory}: cannot read rules directory: {exc.strerror}")
                continue
            for path in paths:
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    warnings.append(f"{path}: cannot read rules file: {exc}")
                    continue
                found, problems = parse_rules(text, path)
                rules.extend(found)
                warnings.extend(problems)
        return cls(rules, user_dir, warnings)

    def _match_part(self, tokens: tuple[str, ...]) -> list[PrefixRule]:
        return [rule for rule in self.rules if rule.matches(tokens)]

    def evaluate(self, command: str) -> RuleMatch | None:
        """Forbidden if any part matches a forbidden rule; allowed if every part matches an allow rule."""
        parts = split_command(command)
        if parts is None:
            return None
        allowed: list[PrefixRule] = []
        every_part_allowed = True
        for tokens in parts:
            matched = self._match_part(tokens)
            forbidden = [r for r in matched if r.decision == RuleDecision.FORBIDDEN]
            if forbidden:
                return RuleMatch(RuleDecision.FORBIDDEN, (forbidden[0],))
            allow = [r for r in matched if r.decision == RuleDecision.ALLOW]
            if allow:
                allowed.append(allow[0])
            else:
                every_part_allowed = False
        if every_part_allowed:
            return RuleMatch(RuleDecision.ALLOW, tuple(allowed))
        return None

    def suggest_prefix(self, command: str) -> tuple[str, ...]:
        """The prefix offered for "always allow": the tokens of a single simple command."""
        parts = split_command(command)
        if parts is None or len(parts) != 1:
            return ()
        prefix = parts[0]
        if is_banned(prefix):
            return ()
        return prefix

    def allow_always(self, prefix: tuple[str, ...]) -> Path:
        """Add an allow rule for this session and append it to the user rules file."""
        if not prefix or is_banned(prefix):
            raise ValueError(f"prefix cannot be allowed persistently: {shlex.join(prefix)!r}")
        path = self.user_dir / DEFAULT_RULES_FILE
        self.user_dir.mkdir(parents=True, exist_ok=True)
        # Start on a new line if the file was last edited by hand without a final newline.
        existing = path.read_bytes() if path.is_file() else b""
        separator = "\n" if existing and not existing.endswith(b"\n") else ""
        with path.open("a", encoding="utf-8") as handle:
            handle.write(separator + format_rule(prefix))
        self.rules.append(PrefixRule(prefix, RuleDecision.ALLOW, str(path)))
        return path
