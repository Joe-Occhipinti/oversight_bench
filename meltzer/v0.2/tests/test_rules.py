from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from meltzer.approval.rules import (
    RuleDecision,
    RuleSet,
    is_banned,
    parse_rules,
    split_command,
)


def write_rules(directory: Path, text: str, name: str = "default.rules") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text)


def rules_from(text: str, tmp_path: Path) -> tuple[RuleSet, list[str]]:
    rules, warnings = parse_rules(text, tmp_path / "test.rules")
    return RuleSet(rules, tmp_path / "rules", warnings), warnings


# --- split_command ---------------------------------------------------------------


@pytest.mark.parametrize(
    "command, expected",
    [
        ("pytest -q", [("pytest", "-q")]),
        ("pytest -q && ls -la | wc -l", [("pytest", "-q"), ("ls", "-la"), ("wc", "-l")]),
        ("make lint; make test || echo failed", [("make", "lint"), ("make", "test"), ("echo", "failed")]),
        ("grep 'a b' file", [("grep", "a b", "file")]),
        ('git commit -m "fix: x"', [("git", "commit", "-m", "fix: x")]),
        ("echo a\\ b", [("echo", "a b")]),
        ('echo ""', [("echo", "")]),
        ('echo "say \\"hi\\""', [("echo", 'say "hi"')]),
    ],
)
def test_split_command(command, expected):
    assert split_command(command) == expected


@pytest.mark.parametrize(
    "command, expected",
    [
        ('echo ";"', [("echo", ";")]),
        ("echo ';' && ls", [("echo", ";"), ("ls",)]),
        ("echo \\;", [("echo", ";")]),
        ("grep 'a|b' file", [("grep", "a|b", "file")]),
        ('echo "a && b"', [("echo", "a && b")]),
    ],
)
def test_quoted_operators_are_ordinary_words(command, expected):
    assert split_command(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "pytest > out.txt",
        "pytest 2>&1",
        "cat < input",
        "(cd sub && make)",
        "sleep 10 &",
        "echo $HOME",
        "echo `id`",
        "echo $(id)",
        "pytest\nls",
        "pytest &&",
        "&& pytest",
        "ls ;; ls",
        "echo 'unclosed",
        'echo "unclosed',
        "echo trailing\\",
        "",
    ],
)
def test_other_shell_syntax_is_not_split(command):
    assert split_command(command) is None


# --- rules files ------------------------------------------------------------------


def test_parse_rules(tmp_path):
    text = (
        'prefix_rule(pattern=["cargo", "check"], decision="allow")\n'
        'prefix_rule(pattern=["git", ["status", "diff"]])\n'
        'prefix_rule(pattern=["rm", "-rf"], decision="forbidden", justification="Use trash instead.")\n'
    )
    rules, warnings = parse_rules(text, tmp_path / "default.rules")
    assert warnings == []
    assert [(r.pattern, r.decision) for r in rules] == [
        (("cargo", "check"), RuleDecision.ALLOW),
        (("git", "status"), RuleDecision.ALLOW),
        (("git", "diff"), RuleDecision.ALLOW),
        (("rm", "-rf"), RuleDecision.FORBIDDEN),
    ]
    assert rules[3].justification == "Use trash instead."
    assert rules[0].source == f"{tmp_path / 'default.rules'}:1"


def test_rule_spanning_several_lines(tmp_path):
    text = 'prefix_rule(\n    pattern=["cargo", "test"],\n    decision="allow",\n)\n'
    rules, warnings = parse_rules(text, tmp_path / "x.rules")
    assert warnings == [] and [r.pattern for r in rules] == [("cargo", "test")]


@pytest.mark.parametrize(
    "line, message",
    [
        ('prefix_rule(pattern=[], decision="allow")', "non-empty list"),
        ('prefix_rule(pattern=["ls"], decision="maybe")', "'decision'"),
        ('prefix_rule(pattern=["ls"], color="red")', "unknown argument"),
        ('prefix_rule(["ls"])', "keyword arguments only"),
        ('prefix_rule(pattern=["ls", 3])', "non-empty strings"),
        ('prefix_rule(pattern=[name])', "literal value"),
        ("prefix_rule(pattern={[1]})", "literal value"),
        ('allow(pattern=["ls"])', "unknown function"),
        ("x = 1", "expected a prefix_rule"),
    ],
)
def test_invalid_rules_are_skipped_with_a_warning(tmp_path, line, message):
    text = f'{line}\nprefix_rule(pattern=["cargo", "check"])\n'
    rules, warnings = parse_rules(text, tmp_path / "x.rules")
    assert [r.pattern for r in rules] == [("cargo", "check")]
    assert len(warnings) == 1 and message in warnings[0]


def test_syntax_error_drops_only_that_rule(tmp_path):
    text = (
        'prefix_rule(pattern=["cargo", "check"])\n'
        'prefix_rule(pattern=["cargo", "build"]\n'
        'prefix_rule(pattern=["cargo", "fmt"])\n'
    )
    rules, warnings = parse_rules(text, tmp_path / "x.rules")
    assert [r.pattern for r in rules] == [("cargo", "check"), ("cargo", "fmt")]
    assert len(warnings) == 1 and "x.rules:2: syntax error" in warnings[0]


@pytest.mark.parametrize(
    "pattern, banned",
    [
        (["bash", "-c"], True),
        (["sh"], True),
        (["python3"], True),
        (["python3", "-c"], True),
        (["/usr/bin/python3.12", "-m"], True),
        (["node", "-e"], True),
        (["env", "make"], True),
        (["sudo", "apt-get"], True),
        (["xargs", "rm"], True),
        (["git"], True),
        (["npm", "run"], True),
        (["uv", "run"], True),
        (["python3", "tools/check.py"], False),
        (["./scripts/test.sh"], False),
        (["bash", "scripts/build.sh"], False),
        (["git", "status"], False),
        (["npm", "run", "lint"], False),
        (["cargo", "test"], False),
    ],
)
def test_allow_rule_patterns(tmp_path, pattern, banned):
    assert is_banned(tuple(pattern)) is banned
    rule_set, warnings = rules_from(f"prefix_rule(pattern={pattern!r})\n", tmp_path)
    if banned:
        assert rule_set.rules == []
        assert len(warnings) == 1 and "too broad" in warnings[0]
    else:
        assert warnings == []
        assert [r.pattern for r in rule_set.rules] == [tuple(pattern)]


def test_alternative_that_expands_to_a_broad_rule_rejects_the_whole_rule(tmp_path):
    rule_set, warnings = rules_from('prefix_rule(pattern=[["git", "make"]])\n', tmp_path)
    assert rule_set.rules == [] and "too broad" in warnings[0]


def test_forbidden_rules_may_be_broad(tmp_path):
    rule_set, warnings = rules_from('prefix_rule(pattern=["sudo"], decision="forbidden")\n', tmp_path)
    assert warnings == []
    match = rule_set.evaluate("sudo rm -rf /var/cache")
    assert match is not None and match.decision == RuleDecision.FORBIDDEN


# --- evaluation ------------------------------------------------------------------------


def test_evaluate(tmp_path):
    rule_set, _ = rules_from(
        'prefix_rule(pattern=["cargo", "check"])\n'
        'prefix_rule(pattern=["git", ["status", "diff"]])\n'
        'prefix_rule(pattern=["rm", "-rf"], decision="forbidden")\n',
        tmp_path,
    )
    allowed = rule_set.evaluate("cargo check --all && git status")
    assert allowed is not None and allowed.decision == RuleDecision.ALLOW
    assert [r.pattern for r in allowed.rules] == [("cargo", "check"), ("git", "status")]
    assert rule_set.evaluate("cargo build") is None
    assert rule_set.evaluate("cargo check && cargo build") is None
    assert rule_set.evaluate("cargo") is None
    assert rule_set.evaluate("cargo check > log.txt") is None
    forbidden = rule_set.evaluate("git status; rm -rf build")
    assert forbidden is not None and forbidden.decision == RuleDecision.FORBIDDEN
    assert forbidden.rules[0].pattern == ("rm", "-rf")


def test_quoted_separator_does_not_split_a_command(tmp_path):
    rule_set, _ = rules_from('prefix_rule(pattern=["git", "log"])\n', tmp_path)
    assert rule_set.evaluate("git log --grep ';'") is not None
    assert rule_set.evaluate("git log --grep x; ls") is None


def test_load_user_and_project_rules(tmp_path):
    user_dir = tmp_path / "home" / ".meltzer" / "rules"
    user_dir.mkdir(parents=True)
    (user_dir / "default.rules").write_text('prefix_rule(pattern=["cargo", "check"])\n')
    (user_dir / "notes.txt").write_text("not a rules file\n")
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".meltzer" / "rules").mkdir(parents=True)
    (project / ".meltzer" / "rules" / "team.rules").write_text('prefix_rule(pattern=["make", "test"])\n')

    user_only = RuleSet.load(user_dir, project)
    assert [r.pattern for r in user_only.rules] == [("cargo", "check")]
    both = RuleSet.load(user_dir, project / "src", include_project=True)
    assert [r.pattern for r in both.rules] == [("cargo", "check"), ("make", "test")]


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs a non-root POSIX user")
def test_unreadable_rules_directory_is_a_warning(tmp_path):
    user_dir = tmp_path / "rules"
    user_dir.mkdir()
    (user_dir / "default.rules").write_text('prefix_rule(pattern=["ls"])\n')
    user_dir.chmod(0o000)
    try:
        rule_set = RuleSet.load(user_dir, tmp_path)
    finally:
        user_dir.chmod(0o755)
    assert rule_set.rules == []
    assert len(rule_set.warnings) == 1 and "cannot read rules directory" in rule_set.warnings[0]


def test_project_rules_search_stops_at_the_home_directory(tmp_path):
    home = tmp_path / "home"
    user_dir = home / ".meltzer" / "rules"
    write_rules(tmp_path / ".meltzer" / "rules", 'prefix_rule(pattern=["make"])\n')
    write_rules(user_dir, 'prefix_rule(pattern=["ls"])\n')
    (home / "proj").mkdir()
    rule_set = RuleSet.load(user_dir, home / "proj", include_project=True, home=home)
    assert [r.pattern for r in rule_set.rules] == [("ls",)]


@pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX symlinks")
def test_symlinked_home_does_not_load_user_rules_twice(tmp_path):
    real = tmp_path / "real-home"
    write_rules(real / ".meltzer" / "rules", 'prefix_rule(pattern=["ls"])\n')
    (real / "proj").mkdir()
    link = tmp_path / "home"
    link.symlink_to(real, target_is_directory=True)
    rule_set = RuleSet.load(link / ".meltzer" / "rules", (real / "proj").resolve(), include_project=True, home=link)
    assert [r.pattern for r in rule_set.rules] == [("ls",)]


def test_suggested_prefix(tmp_path):
    rule_set = RuleSet([], tmp_path / "rules")
    assert rule_set.suggest_prefix("cargo test --release") == ("cargo", "test", "--release")
    assert rule_set.suggest_prefix("cargo test && ls") == ()
    assert rule_set.suggest_prefix("sudo apt-get install jq") == ()
    assert rule_set.suggest_prefix("git") == ()
    assert rule_set.suggest_prefix("echo $HOME") == ()


def test_allow_always_saves_the_rule(tmp_path):
    user_dir = tmp_path / "rules"
    rule_set = RuleSet([], user_dir)
    path = rule_set.allow_always(("cargo", "test"))
    assert path == user_dir / "default.rules"
    assert rule_set.evaluate("cargo test -q") is not None
    reloaded = RuleSet.load(user_dir, tmp_path)
    assert reloaded.warnings == []
    assert [r.pattern for r in reloaded.rules] == [("cargo", "test")]
    with pytest.raises(ValueError):
        rule_set.allow_always(("python3",))
