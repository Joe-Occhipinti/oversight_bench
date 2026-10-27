from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import pytest

from meltzer.config import ConfigError, load_config
from meltzer.config.trust import (
    ProjectsTableError,
    TrustLevel,
    normalize_path,
    parse_projects,
    project_trust,
    record_project_trust,
    set_trust_level,
)
from meltzer.config.loader import find_project_config, parse_override
from meltzer.config.schema import DEFAULT_MAX_TURN_STEPS, DEFAULT_MODEL, KEYS


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    (path / ".git").mkdir(parents=True)
    return path


def write_user_config(home: Path, text: str) -> Path:
    path = home / ".meltzer" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "text, expected",
    [
        ("model=gpt-x", ("model", "gpt-x")),
        ('model="gpt-x"', ("model", "gpt-x")),
        ("max_turn_steps=7", ("max_turn_steps", 7)),
        ("flag=true", ("flag", True)),
        ('notify=["say", "done"]', ("notify", ["say", "done"])),
        (" model = spaced ", ("model", "spaced")),
        ("model=", ("model", "")),
    ],
)
def test_parse_override(text, expected):
    assert parse_override(text) == expected


@pytest.mark.parametrize("text", ["model", "=value", ""])
def test_parse_override_needs_key_and_equals(text):
    with pytest.raises(ConfigError):
        parse_override(text)


def test_defaults_without_config_files(home, work):
    config = load_config(work, home=home)
    assert config.model == DEFAULT_MODEL
    assert config.max_turn_steps == DEFAULT_MAX_TURN_STEPS
    assert config.notify is None
    assert config.monitor_model is None
    assert set(config.sources.values()) == {"default"}
    assert set(config.summary()) == {key.name for key in KEYS}


def test_user_config_values_are_parsed(home, work):
    path = write_user_config(
        home,
        'model = "gpt-y"\n'
        "max_turn_steps = 12\n"
        'notify = ["notify-send", "meltzer"]\n'
        "[model_provider]\n"
        'base_url = "http://localhost:8000/v1"\n',
    )
    config = load_config(work, home=home)
    assert config.model == "gpt-y"
    assert config.max_turn_steps == 12
    assert config.notify == ("notify-send", "meltzer")
    assert config.base_url == "http://localhost:8000/v1"
    assert config.sources["model"] == str(path)
    assert config.summary()["notify"] == ["notify-send", "meltzer"]


def test_command_line_overrides_user_config(home, work):
    write_user_config(home, 'model = "from-file"\n')
    config = load_config(work, home=home, overrides=[("model", "from-cli")])
    assert config.model == "from-cli"
    assert config.sources["model"] == "command line"


@pytest.mark.parametrize(
    "key, value, message",
    [
        ("model", "", "non-empty string"),
        ("model", 3, "non-empty string"),
        ("max_turn_steps", 0, "positive integer"),
        ("max_turn_steps", True, "positive integer"),
        ("max_turn_steps", "10", "positive integer"),
        ("notify", [], "non-empty list"),
        ("notify", ["ok", ""], "non-empty list"),
        ("sandbox_mode", "maybe", "expected one of"),
        ("approval_policy", "sometimes", "expected one of"),
    ],
)
def test_invalid_values_are_errors(home, work, key, value, message):
    with pytest.raises(ConfigError, match=message):
        load_config(work, home=home, overrides=[(key, value)])


def test_unknown_key_is_an_error(home, work):
    with pytest.raises(ConfigError, match="unknown key 'modle'"):
        load_config(work, home=home, overrides=[("modle", "x")])


def test_unreadable_toml_is_an_error(home, work):
    write_user_config(home, "model = \n")
    with pytest.raises(ConfigError):
        load_config(work, home=home)
    (home / ".meltzer" / "config.toml").write_bytes(b'model = "caf\xe9"\n')
    with pytest.raises(ConfigError):
        load_config(work, home=home)


# --- project config ----------------------------------------------------------------


def write_project_config(directory: Path, text: str) -> Path:
    path = directory / ".meltzer" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def trust(home: Path, project: Path, level: TrustLevel = TrustLevel.TRUSTED) -> None:
    record_project_trust(home / ".meltzer" / "config.toml", normalize_path(project), level)


def test_trusted_project_config_overrides_user_config(home, work):
    write_user_config(home, 'model = "user-model"\nmax_turn_steps = 9\n')
    trust(home, work)
    project = write_project_config(work, 'model = "project-model"\n')
    config = load_config(work, home=home)
    assert config.model == "project-model"
    assert config.max_turn_steps == 9
    assert config.sources["model"] == str(project)
    assert config.project_trusted
    assert load_config(work, home=home, overrides=[("model", "cli-model")]).model == "cli-model"


@pytest.mark.parametrize("level", [None, TrustLevel.UNTRUSTED])
def test_project_config_ignored_unless_trusted(home, work, level):
    write_user_config(home, 'model = "user-model"\n')
    if level is not None:
        trust(home, work, level)
    write_project_config(work, 'model = "project-model"\n')
    config = load_config(work, home=home)
    assert config.model == "user-model"
    assert not config.project_trusted


def test_trust_answer_that_was_not_recorded_applies(home, work):
    write_project_config(work, 'model = "project-model"\n')
    assert load_config(work, home=home, unrecorded_trust=TrustLevel.TRUSTED).model == "project-model"
    assert load_config(work, home=home, unrecorded_trust=TrustLevel.UNTRUSTED).model == DEFAULT_MODEL


def test_nearest_project_config_is_used(home, work):
    trust(home, work)
    write_project_config(work, 'model = "repo"\n')
    write_project_config(work / "pkg", 'model = "pkg"\n')
    (work / "pkg" / "src").mkdir()
    assert load_config(work / "pkg" / "src", home=home).model == "pkg"
    assert load_config(work, home=home).model == "repo"


def test_project_config_search_stops_at_the_repository_root(home, tmp_path):
    write_project_config(tmp_path, 'model = "outside"\n')
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    trust(home, tmp_path)
    assert load_config(repo, home=home).model == DEFAULT_MODEL


def test_user_config_is_not_read_twice_as_project_config(tmp_path):
    home = tmp_path / "home"
    write_user_config(home, 'model = "user-model"\n')
    trust(home, home)
    config = load_config(home / "src", home=home)
    assert config.model == "user-model"
    assert config.sources["model"] == str(home / ".meltzer" / "config.toml")


def test_project_config_search_stops_at_the_home_directory(tmp_path):
    home = tmp_path / "home"
    write_project_config(tmp_path, 'model = "above-home"\n')
    (home / "src" / "app").mkdir(parents=True)
    trust(home, tmp_path)
    assert load_config(home / "src" / "app", home=home).model == DEFAULT_MODEL
    assert find_project_config(home / "src" / "app", home) is None
    write_project_config(home / "src", 'model = "src"\n')
    assert load_config(home / "src" / "app", home=home).model == "src"


@pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX symlinks")
def test_symlinked_home_does_not_read_the_user_config_twice(tmp_path):
    real = tmp_path / "real-home"
    write_user_config(real, 'model = "user-model"\n')
    (real / "src").mkdir()
    home = tmp_path / "home"
    home.symlink_to(real, target_is_directory=True)
    trust(home, real)
    assert find_project_config((real / "src").resolve(), home) is None


def test_monitor_settings_are_a_table(home, work):
    write_user_config(home, '[monitor]\nmodel = "judge"\ntranscript_items = 5\n')
    config = load_config(work, home=home)
    assert config.monitor_model == "judge" and config.monitor_transcript_items == 5
    with pytest.raises(ConfigError, match="unknown key 'monitor_model'"):
        load_config(work, home=home, overrides=[("monitor_model", "judge")])


def test_hook_lists_are_combined_across_layers(home, work):
    user_path = write_user_config(home, '[[hooks.Stop]]\ncommand = ["user-hook"]\n')
    trust(home, work)
    project_path = write_project_config(
        work,
        '[[hooks.Stop]]\ncommand = ["project-hook"]\n[[hooks.Stop]]\ncommand = ["user-hook"]\n',
    )
    config = load_config(work, home=home, overrides=[("hooks.Stop", [{"command": ["cli-hook"]}])])
    assert [hook.command for hook in config.hooks_stop] == [("user-hook",), ("project-hook",), ("cli-hook",)]
    assert config.sources["hooks.Stop"] == f"{user_path}, {project_path}, command line"


def test_equivalent_hooks_from_two_layers_are_kept_once(home, work):
    write_user_config(home, '[[hooks.Stop]]\ncommand = ["notify"]\n')
    trust(home, work)
    write_project_config(work, '[[hooks.Stop]]\ncommand = ["notify"]\ncwd = "."\n')
    assert [hook.command for hook in load_config(work, home=home).hooks_stop] == [("notify",)]


def test_empty_hook_list_keeps_hooks_of_earlier_layers(home, work):
    write_user_config(home, '[[hooks.PreToolUse]]\ncommand = ["check"]\nmatcher = "shell"\n')
    trust(home, work)
    write_project_config(work, "[hooks]\nPreToolUse = []\n")
    config = load_config(work, home=home, overrides=[("hooks.PreToolUse", [])])
    assert [(hook.command, hook.matcher) for hook in config.hooks_pre_tool_use] == [(("check",), "shell")]


def test_projects_table_in_project_config_is_ignored(home, work):
    trust(home, work)
    write_project_config(work, f'[projects.{json.dumps(str(work.parent))}]\ntrust_level = "trusted"\n')
    config = load_config(work, home=home)
    assert any("'projects' can only be set in user config" in warning for warning in config.warnings)


# --- [projects] table -----------------------------------------------------------


def projects_toml(entries: dict[Path, str]) -> str:
    return "".join(
        f"[projects.{json.dumps(str(path))}]\ntrust_level = {json.dumps(level)}\n" for path, level in entries.items()
    )


def test_projects_table_is_not_a_config_key(home, work):
    write_user_config(home, 'model = "m"\n' + projects_toml({work: "trusted"}))
    config = load_config(work, home=home)
    assert config.model == "m"
    assert not any(name.startswith("projects") for name in config.summary())
    assert config.project_trusted


def test_trust_lookup(tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    deep = inner / "deep"
    deep.mkdir(parents=True)
    levels, warnings = parse_projects(
        {str(outer): {"trust_level": "trusted"}, str(inner): {"trust_level": "untrusted"}}
    )
    assert warnings == []
    assert project_trust(outer / "other", levels).trusted
    trust = project_trust(deep, levels)
    assert trust.level == TrustLevel.UNTRUSTED
    assert trust.entry == normalize_path(inner)
    assert not project_trust(tmp_path, levels).recorded


def test_new_decision_is_recorded_for_the_git_root(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "pkg").mkdir()
    assert project_trust(repo / "pkg", {}).root == normalize_path(repo)
    (tmp_path / "plain").mkdir()
    assert project_trust(tmp_path / "plain", {}).root == normalize_path(tmp_path / "plain")


@pytest.mark.parametrize(
    "value, message",
    [
        ({"/x": {"trust_level": "maybe"}}, 'expected "trusted" or "untrusted"'),
        ({"/x": {"trust_level": "trusted", "extra": 1}}, "unknown key"),
        ({"/x": "trusted"}, "must be a table"),
        ("trusted", "table of project tables"),
    ],
)
def test_invalid_projects_table(value, message):
    with pytest.raises(ProjectsTableError, match=message):
        parse_projects(value)


def test_invalid_projects_table_in_user_config_is_a_config_error(home, work):
    write_user_config(home, '[projects."/x"]\ntrust_level = "yes"\n')
    with pytest.raises(ConfigError, match="trust_level"):
        load_config(work, home=home)


def test_relative_project_path_is_ignored_with_warning():
    levels, warnings = parse_projects({"src/app": {"trust_level": "trusted"}})
    assert levels == {}
    assert len(warnings) == 1 and "not an absolute path" in warnings[0]


def test_set_trust_level_appends_a_table():
    text = 'model = "m"\n[model_provider]\nbase_url = "http://x"'
    updated = set_trust_level(text, "/src/app", TrustLevel.TRUSTED)
    assert updated.startswith(text + "\n")
    data = tomllib.loads(updated)
    assert data["model_provider"] == {"base_url": "http://x"}
    assert data["projects"] == {"/src/app": {"trust_level": "trusted"}}


def test_set_trust_level_updates_only_that_table():
    text = (
        "# my settings\n"
        '[projects."/a"]\ntrust_level = "untrusted"\n\n'
        '[projects."/b"]\ntrust_level = "untrusted"\n'
        '[[hooks.Stop]]\ncommand = ["x"]\n'
    )
    updated = set_trust_level(text, "/a", TrustLevel.TRUSTED)
    assert updated == text.replace('trust_level = "untrusted"', 'trust_level = "trusted"', 1)
    header_only = set_trust_level('[projects."/c"]\n', "/c", TrustLevel.TRUSTED)
    assert tomllib.loads(header_only)["projects"]["/c"] == {"trust_level": "trusted"}


def test_record_project_trust_refuses_a_table_it_cannot_extend(tmp_path):
    path = tmp_path / "config.toml"
    original = f'[projects]\n{json.dumps(str(Path("/a")))} = {{ trust_level = "untrusted" }}\n'
    path.write_text(original, encoding="utf-8")
    with pytest.raises(ProjectsTableError):
        record_project_trust(path, Path("/a"), TrustLevel.TRUSTED)
    assert path.read_text(encoding="utf-8") == original
    new = tmp_path / "new" / "config.toml"
    record_project_trust(new, Path("/b"), TrustLevel.TRUSTED)
    assert tomllib.loads(new.read_text(encoding="utf-8")) == {"projects": {str(Path("/b")): {"trust_level": "trusted"}}}
