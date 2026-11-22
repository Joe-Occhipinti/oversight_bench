from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from meltzer.approval.check import command_line, run_execpolicy
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
    text = 'prefix_rule(pattern=["git", "push"], decision="prompt", justification="Pushes need a review.")\n'
    rules, warnings = parse_rules(text, tmp_path / "x.rules")
    assert warnings == []
    assert [(r.pattern, r.decision, r.justification) for r in rules] == [
        (("git", "push"), RuleDecision.PROMPT, "Pushes need a review.")
    ]


@pytest.mark.parametrize(
    "decision, command", [("prompt", "git push origin main"), ("forbidden", "git push origin main")]
)
def test_prompt_and_forbidden_rules_may_be_broad(tmp_path, decision, command):
    rule_set, warnings = rules_from(f'prefix_rule(pattern=["git"], decision="{decision}")\n', tmp_path)
    match = rule_set.evaluate(command)
    assert warnings == [] and match is not None and match.decision.value == decision


def test_rule_with_examples(tmp_path):
    text = (
        "prefix_rule(\n"
        '    pattern=["git", ["status", "diff"]],\n'
        '    match=[["git", "status"], "git diff --stat", "git status \'a b\'"],\n'
        '    not_match=["git push", ["git", "stat"], "git"],\n'
        ")\n"
    )
    rules, warnings = parse_rules(text, tmp_path / "x.rules")
    assert warnings == []
    assert [r.pattern for r in rules] == [("git", "status"), ("git", "diff")]


def test_rule_whose_examples_fail_is_rejected_with_a_warning(tmp_path):
    text = (
        'prefix_rule(pattern=["cargo", "check"], match=["cargo check", "cargo build"])\n'
        'prefix_rule(pattern=["rm", "-rf"], decision="forbidden", not_match=["rm -rf build"])\n'
        'prefix_rule(pattern=["cargo", "test"], match=["cargo test -q"])\n'
    )
    rules, warnings = parse_rules(text, tmp_path / "x.rules")
    assert [r.pattern for r in rules] == [("cargo", "test")]
    assert len(warnings) == 2
    assert "x.rules:1: rule rejected" in warnings[0] and "'cargo build'" in warnings[0]
    assert "x.rules:2: rule rejected" in warnings[1] and "'rm -rf build'" in warnings[1]


@pytest.mark.parametrize(
    "line, message",
    [
        ('prefix_rule(pattern=[], decision="allow")', "non-empty list"),
        ('prefix_rule(pattern=["ls"], decision="maybe")', "'decision'"),
        ('prefix_rule(pattern=["ls"], justification=3)', "'justification'"),
        ('prefix_rule(pattern=["ls"], match="ls")', "'match' must be a list"),
        ('prefix_rule(pattern=["ls"], not_match=[3])', "'not_match' examples"),
        ('prefix_rule(pattern=["ls"], match=[[]])', "'match' examples"),
        ('prefix_rule(pattern=["ls"], match=[""])', "empty"),
        ('prefix_rule(pattern=["ls"], match=["ls \'a"])', "single command"),
        ('prefix_rule(pattern=["ls"], match=["ls && pwd"])', "single command"),
        ('prefix_rule(pattern=["ls"], match=["pwd"])', "does not match"),
        ('prefix_rule(pattern=["ls"], not_match=["ls -la"])', "matches"),
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
    assert rule_set.evaluate("cargo") is None
    assert rule_set.evaluate("cargo check-all") is None
    assert rule_set.evaluate("cargo check --grep ';'") is not None
    assert rule_set.evaluate("cargo check > log.txt") is None
    forbidden = rule_set.evaluate("git status; rm -rf build")
    assert forbidden is not None and forbidden.decision == RuleDecision.FORBIDDEN
    assert forbidden.rules[0].pattern == ("rm", "-rf")


def test_evaluate_prompt(tmp_path):
    rule_set, _ = rules_from(
        'prefix_rule(pattern=["git", "status"])\n'
        'prefix_rule(pattern=["git", "push"], decision="prompt")\n'
        'prefix_rule(pattern=["npm", "install"])\n'
        'prefix_rule(pattern=["npm", "install", "-g"], decision="prompt")\n',
        tmp_path,
    )
    for command in ("git push origin main", "git status && git push"):
        match = rule_set.evaluate(command)
        assert match is not None and match.decision == RuleDecision.PROMPT
        assert [r.pattern for r in match.rules] == [("git", "push")]
    assert rule_set.evaluate("npm install").decision == RuleDecision.ALLOW
    assert rule_set.evaluate("npm install -g typescript").decision == RuleDecision.PROMPT


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf build",
        "git push; rm -rf build",
        "rm -rf build && git push",
        "git status | rm -rf build",
        "ls; rm -rf build",
    ],
)
def test_forbidden_wins(tmp_path, command):
    rule_set, _ = rules_from(
        'prefix_rule(pattern=["git", "status"])\n'
        'prefix_rule(pattern=["git", "push"], decision="prompt")\n'
        'prefix_rule(pattern=["rm", "-rf"], decision="forbidden", justification="Use trash instead.")\n'
        'prefix_rule(pattern=["rm"])\n',
        tmp_path,
    )
    match = rule_set.evaluate(command)
    assert match is not None and match.decision == RuleDecision.FORBIDDEN
    assert match.rules[0].pattern == ("rm", "-rf")
    assert match.rules[0].justification == "Use trash instead."


def test_matching_rules_lists_every_match(tmp_path):
    rule_set, _ = rules_from(
        'prefix_rule(pattern=["git", "status"])\n'
        'prefix_rule(pattern=["git"], decision="prompt")\n',
        tmp_path,
    )
    assert [(r.pattern, r.decision) for r in rule_set.matching_rules("git status && git push")] == [
        (("git", "status"), RuleDecision.ALLOW),
        (("git",), RuleDecision.PROMPT),
        (("git",), RuleDecision.PROMPT),
    ]
    assert rule_set.matching_rules("echo $HOME") == []


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


# --- meltzer execpolicy check -------------------------------------------------------


CHECK_RULES = (
    'prefix_rule(pattern=["cargo", "check"])\n'
    'prefix_rule(pattern=["git", "status"])\n'
    'prefix_rule(pattern=["git", "push"], decision="prompt", justification="Pushes need a review.")\n'
    'prefix_rule(pattern=["rm", "-rf"], decision="forbidden", justification="Use trash instead.")\n'
)


def run_check(tmp_path, capsys, *command: str, rules: str = CHECK_RULES, extra: tuple[str, ...] = ()):
    rules_file = tmp_path / "check.rules"
    rules_file.write_text(rules)
    code = run_execpolicy(["check", "--rules", str(rules_file), *extra, "--", *command])
    out, err = capsys.readouterr()
    return code, out, err


def test_check_allowed_command(tmp_path, capsys):
    code, out, err = run_check(tmp_path, capsys, "cargo", "check", "--all")
    assert code == 0 and err == ""
    assert json.loads(out) == {
        "matchedRules": [{"prefixRuleMatch": {"matchedPrefix": ["cargo", "check"], "decision": "allow"}}],
        "decision": "allow",
    }
    code, out, _ = run_check(tmp_path, capsys, "make", "test")
    assert code == 0 and json.loads(out) == {"matchedRules": []}
    code, out, _ = run_check(tmp_path, capsys, "git", "status", extra=("--pretty",))
    assert code == 0 and out.startswith("{\n  ") and json.loads(out)["decision"] == "allow"


def test_check_forbidden_command_shows_the_justification(tmp_path, capsys):
    code, out, _ = run_check(tmp_path, capsys, "rm", "-rf", "build")
    assert code == 0
    assert json.loads(out) == {
        "matchedRules": [
            {
                "prefixRuleMatch": {
                    "matchedPrefix": ["rm", "-rf"],
                    "decision": "forbidden",
                    "justification": "Use trash instead.",
                }
            }
        ],
        "decision": "forbidden",
    }


def test_check_command_line_with_several_commands(tmp_path, capsys):
    code, out, _ = run_check(tmp_path, capsys, "git status && git push origin main")
    assert code == 0
    result = json.loads(out)
    assert result["decision"] == "prompt"
    assert [m["prefixRuleMatch"]["matchedPrefix"] for m in result["matchedRules"]] == [
        ["git", "status"],
        ["git", "push"],
    ]


def test_check_several_rules_files(tmp_path, capsys):
    first = tmp_path / "a.rules"
    first.write_text('prefix_rule(pattern=["cargo", "check"])\n')
    second = tmp_path / "b.rules"
    second.write_text('prefix_rule(pattern=["cargo"], decision="forbidden")\n')
    code = run_execpolicy(["check", "-r", str(first), "--rules", str(second), "--", "cargo", "check"])
    out, _ = capsys.readouterr()
    assert code == 0 and json.loads(out)["decision"] == "forbidden"


def test_check_reports_rejected_rules_on_stderr(tmp_path, capsys):
    rules = 'prefix_rule(pattern=["ls"], match=["pwd"])\nprefix_rule(pattern=["cargo", "check"])\n'
    code, out, err = run_check(tmp_path, capsys, "cargo", "check", rules=rules)
    assert code == 0 and json.loads(out)["decision"] == "allow"
    assert "warning:" in err and "check.rules:1: rule rejected" in err


def test_check_usage_errors(tmp_path, capsys):
    code = run_execpolicy(["check", "--rules", str(tmp_path / "missing.rules"), "--", "ls"])
    out, err = capsys.readouterr()
    assert code == 2 and out == "" and "cannot read" in err
    (tmp_path / "x.rules").write_text("")
    with pytest.raises(SystemExit) as exc:
        run_execpolicy(["check", "--rules", str(tmp_path / "x.rules")])
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "tokens, expected",
    [
        (["git status && ls"], "git status && ls"),
        (["git", "commit", "-m", "fix: a b"], "git commit -m 'fix: a b'"),
        (["echo", "&&", "ls"], "echo '&&' ls"),
    ],
)
def test_check_command_line(tokens, expected):
    assert command_line(tokens) == expected
