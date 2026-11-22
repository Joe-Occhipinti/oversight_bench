from __future__ import annotations

import os
from pathlib import Path

import pytest

from meltzer.history import Kind, Role
from meltzer.skills import loader as loader_module
from meltzer.skills.catalog import MAX_DESCRIPTION_CHARS, render_catalog
from meltzer.skills.inject import extract_mentions, format_skill, mentioned_skill_items, select_skills, skill_item
from meltzer.skills.loader import (
    Skill,
    SkillError,
    SkillRoot,
    SkillScope,
    SkillSet,
    find_skill_files,
    parse_frontmatter,
    parse_skill,
    skill_roots,
)


def write_skill(directory: Path, name: str | None = None, description: str = "Does a thing.", body: str = "") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    if name is not None:
        lines.append(f"name: {name}")
    lines.append(f"description: {description}")
    lines.append("---")
    path = directory / "SKILL.md"
    path.write_text("\n".join(lines) + "\n" + body, encoding="utf-8")
    return path


def roots(home: Path, work: Path) -> list[SkillRoot]:
    return skill_roots(home / ".meltzer", work, include_project=True)


# Frontmatter.


def test_frontmatter_scalar_forms():
    text = (
        "---\n"
        "# a comment\n"
        "a: 'it''s quoted'\n"
        'b: "line\\none \\"two\\""\n'
        "c: Build for AWS: ECS  # trailing comment\n"
        "d: >\n"
        "  folded\n"
        "  text\n"
        "e: |\n"
        "  literal\n"
        "  text\n"
        "f: plain\n"
        "  continued\n"
        "g: ~\n"
        "tags:\n"
        "  - one\n"
        "  - two\n"
        "h:\n"
        "---\n"
    )
    assert parse_frontmatter(text) == {
        "a": "it's quoted",
        "b": 'line\none "two"',
        "c": "Build for AWS: ECS",
        "d": "folded text\n",
        "e": "literal\ntext\n",
        "f": "plain continued",
        "g": None,
        "tags": ["one", "two"],
        "h": None,
    }


@pytest.mark.parametrize(
    "text",
    [
        "no frontmatter\n",
        "---\nname: x\n",  # not closed
        "---\n---\nbody\n",  # empty
        "---\nname x\n---\n",
        "---\nname: 'open\n---\n",
        "---\nname: x\n   description: y\n---\n",
    ],
)
def test_invalid_frontmatter(text):
    with pytest.raises(SkillError):
        parse_frontmatter(text)


def test_frontmatter_ignores_a_byte_order_mark():
    assert parse_frontmatter("\ufeff---\ndescription: d\n---\n") == {"description": "d"}


# Skill files.


def test_parse_skill(tmp_path):
    path = tmp_path / "deploy" / "SKILL.md"
    text = "---\nname: deploy\ndescription: >\n  Deploy the\n  service\nmetadata:\n  short-description: Deploy\n---\nbody"
    assert parse_skill(text, path, SkillScope.USER) == Skill(
        name="deploy",
        description="Deploy the service",
        path=path,
        scope=SkillScope.USER,
        short_description="Deploy",
    )
    # The name defaults to the directory name; whitespace in name and description is collapsed.
    skill = parse_skill("---\ndescription: d\n---\n", tmp_path / "lint-fix" / "SKILL.md", SkillScope.REPO)
    assert skill.name == "lint-fix"
    skill = parse_skill("---\nname: '  my   skill '\ndescription: \"a\\n  b\"\n---\n", path, SkillScope.USER)
    assert (skill.name, skill.description) == ("my skill", "a b")


@pytest.mark.parametrize(
    "text, message",
    [
        ("---\nname: x\n---\n", "description"),
        ("---\nname: x\ndescription: '   '\n---\n", "description"),
        ("---\nname: " + "n" * 65 + "\ndescription: d\n---\n", "name"),
        ("---\nname:\n  - a\ndescription: d\n---\n", "name"),
        ("---\ndescription: d\nmetadata: text\n---\n", "metadata"),
    ],
)
def test_invalid_skill(tmp_path, text, message):
    with pytest.raises(SkillError, match=message):
        parse_skill(text, tmp_path / "x" / "SKILL.md", SkillScope.USER)


# Roots and discovery.


def test_roots_are_the_project_and_user_skill_directories(home, work):
    found = skill_roots(home / ".meltzer", work, include_project=True)
    assert found == [
        SkillRoot(work / ".meltzer" / "skills", SkillScope.REPO),
        SkillRoot(home / ".meltzer" / "skills", SkillScope.USER),
    ]
    assert skill_roots(home / ".meltzer", work, include_project=False) == [
        SkillRoot(home / ".meltzer" / "skills", SkillScope.USER)
    ]


def test_find_skill_files_scans_nested_directories(tmp_path):
    root = tmp_path / "skills"
    first = write_skill(root / "a")
    second = write_skill(root / "group" / "b")
    write_skill(root / ".hidden")
    (root / "notes").mkdir()
    (root / "notes" / "README.md").write_text("x", encoding="utf-8")
    files, warnings = find_skill_files(root)
    assert files == sorted([first.resolve(), second.resolve()])
    assert warnings == []
    assert find_skill_files(tmp_path / "missing") == ([], [])


def test_find_skill_files_depth_limit(tmp_path):
    root = tmp_path / "skills"
    deepest = root
    for level in range(loader_module.MAX_SCAN_DEPTH):
        deepest = deepest / f"d{level}"
    inside = write_skill(deepest)
    write_skill(deepest / "too-deep")
    files, _ = find_skill_files(root)
    assert files == [inside.resolve()]


def test_find_skill_files_directory_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(loader_module, "MAX_SKILL_DIRS_PER_ROOT", 3)
    root = tmp_path / "skills"
    for name in ("a", "b", "c", "d", "e"):
        write_skill(root / name)
    files, warnings = find_skill_files(root)
    assert len(files) == 2
    assert len(warnings) == 1 and "limit" in warnings[0]


def test_find_skill_files_does_not_follow_symbolic_links(tmp_path):
    outside = tmp_path / "outside"
    write_skill(outside / "linked")
    root = tmp_path / "skills"
    root.mkdir()
    try:
        os.symlink(outside / "linked", root / "linked", target_is_directory=True)
        (root / "file").mkdir()
        os.symlink(outside / "linked" / "SKILL.md", root / "file" / "SKILL.md")
    except OSError:
        pytest.skip("symbolic links are not available")
    files, _ = find_skill_files(root)
    assert files == []


# SkillSet.


def test_skill_set_loads_both_roots_in_catalog_order(home, work):
    write_skill(home / ".meltzer" / "skills" / "zeta")
    write_skill(home / ".meltzer" / "skills" / "alpha")
    write_skill(work / ".meltzer" / "skills" / "mid")
    skills = SkillSet.load(roots(home, work))
    assert [(s.name, s.scope) for s in skills.enabled()] == [
        ("mid", SkillScope.REPO),
        ("alpha", SkillScope.USER),
        ("zeta", SkillScope.USER),
    ]
    assert skills.warnings == []


def test_skill_set_reports_invalid_skills_and_loads_the_rest(home, work):
    user = home / ".meltzer" / "skills"
    write_skill(user / "good")
    (user / "bad").mkdir(parents=True)
    (user / "bad" / "SKILL.md").write_text("no frontmatter", encoding="utf-8")
    skills = SkillSet.load(roots(home, work))
    assert [s.name for s in skills.enabled()] == ["good"]
    assert len(skills.warnings) == 1
    assert "bad" in skills.warnings[0] and "skill rejected" in skills.warnings[0]


def test_a_directory_that_is_both_the_home_and_the_project_is_read_once(home):
    write_skill(home / ".meltzer" / "skills" / "one")
    skills = SkillSet.load(skill_roots(home / ".meltzer", home, include_project=True))
    assert [s.name for s in skills.enabled()] == ["one"]


def test_config_rules_disable_by_name_and_path(home, work):
    user = home / ".meltzer" / "skills"
    write_skill(user / "deploy")
    lint = write_skill(user / "lint")
    write_skill(user / "test")
    write_skill(user / "docs")
    rules = (
        {"name": "deploy", "enabled": False},
        {"path": str(lint), "enabled": False},
        {"path": "~/.meltzer/skills/docs/SKILL.md", "enabled": False},
        {"name": "deploy", "enabled": True},
    )
    skills = SkillSet.load(roots(home, work), rules, home=home)
    assert [s.name for s in skills.enabled()] == ["deploy", "test"]
    assert skills.warnings == []


@pytest.mark.parametrize(
    "entry",
    [
        {"enabled": False},
        {"name": "deploy", "path": "/x/SKILL.md", "enabled": False},
        {"name": "deploy"},
        {"name": "deploy", "enabled": "no"},
        {"name": "  ", "enabled": False},
        {"name": "deploy", "enabled": False, "scope": "user"},
    ],
)
def test_invalid_config_rules_are_ignored_with_a_warning(home, work, entry):
    write_skill(home / ".meltzer" / "skills" / "deploy")
    skills = SkillSet.load(roots(home, work), (entry,))
    assert [s.name for s in skills.enabled()] == ["deploy"]
    assert len(skills.warnings) == 1 and "skills.config" in skills.warnings[0]


# Catalog.


def test_catalog_lists_skills_with_their_files(tmp_path):
    deploy = Skill("deploy", "Deploy the service", tmp_path / "deploy" / "SKILL.md", SkillScope.USER)
    text = render_catalog([deploy])
    assert text.startswith("<skills_instructions>\n## Skills\n")
    assert text.endswith("</skills_instructions>")
    assert f"### Available skills\n- deploy: Deploy the service (file: {deploy.path})\n" in text
    assert "### How to use skills" in text
    assert "`$SkillName`" in text
    assert render_catalog([]) == ""


def test_catalog_shortens_long_descriptions(tmp_path):
    skill = Skill("long", "x" * 2000, tmp_path / "SKILL.md", SkillScope.USER)
    line = next(line for line in render_catalog([skill]).splitlines() if line.startswith("- long:"))
    description = line.removeprefix("- long: ").rsplit(" (file: ", 1)[0]
    assert len(description) == MAX_DESCRIPTION_CHARS
    assert description.endswith("...")


# Mentions.


def make_skills(tmp_path: Path, *names: str) -> list[Skill]:
    return [Skill(name, f"{name} skill", tmp_path / name / "SKILL.md", SkillScope.USER) for name in names]


def test_extract_mentions():
    mentions = extract_mentions("use $deploy, then [$lint](/s/lint/SKILL.md) and $PATH or $ alone; $a:b-c_1.")
    assert mentions.names == {"deploy", "a:b-c_1"}
    assert mentions.paths == {"/s/lint/SKILL.md"}


def test_select_skills_by_name_in_skill_order(tmp_path):
    skills = make_skills(tmp_path, "alpha", "beta", "gamma")
    selected = select_skills(skills, extract_mentions("$gamma and $alpha and $alpha and $unknown"))
    assert [s.name for s in selected] == ["alpha", "gamma"]


def test_select_skills_by_path(tmp_path):
    skills = make_skills(tmp_path, "deploy")
    path = skills[0].path
    assert select_skills(skills, extract_mentions(f"[$deploy]({path})")) == skills
    assert select_skills(skills, extract_mentions(f"[$deploy](skill://{path})")) == skills
    assert select_skills(skills, extract_mentions("[$deploy](/elsewhere/SKILL.md)")) == []


def test_a_shared_name_needs_a_path(tmp_path):
    first = Skill("deploy", "repo", tmp_path / "repo" / "SKILL.md", SkillScope.REPO)
    second = Skill("deploy", "user", tmp_path / "user" / "SKILL.md", SkillScope.USER)
    assert select_skills([first, second], extract_mentions("$deploy")) == []
    assert select_skills([first, second], extract_mentions(f"[$deploy]({second.path})")) == [second]


def test_skill_item(tmp_path):
    path = write_skill(tmp_path / "deploy", name="deploy", body="Run make deploy.\n")
    skill = Skill("deploy", "Does a thing.", path, SkillScope.USER)
    item = skill_item(skill)
    assert (item.role, item.kind, item.source) == (Role.USER, Kind.SKILL, "skill:deploy")
    contents = path.read_text(encoding="utf-8")
    assert item.content == f"<skill>\n<name>deploy</name>\n<path>{path}</path>\n{contents}\n</skill>"
    assert item.content == format_skill(skill, contents)


def test_skill_body_is_read_when_the_prompt_is_submitted(tmp_path):
    path = write_skill(tmp_path / "deploy", name="deploy", body="old body\n")
    skills = [Skill("deploy", "d", path, SkillScope.USER)]
    path.write_text("---\ndescription: d\n---\nnew body\n", encoding="utf-8")
    items = mentioned_skill_items(skills, "please $deploy", notice=lambda message: None)
    assert len(items) == 1 and "new body" in items[0].content


def test_unreadable_skill_gives_a_notice(tmp_path):
    skills = [Skill("deploy", "d", tmp_path / "gone" / "SKILL.md", SkillScope.USER)]
    notices: list[str] = []
    assert mentioned_skill_items(skills, "$deploy", notice=notices.append) == []
    assert len(notices) == 1 and "deploy" in notices[0]

