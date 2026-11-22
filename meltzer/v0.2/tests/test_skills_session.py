from __future__ import annotations

from pathlib import Path

import pytest

from meltzer.history import Kind
from meltzer.session import Session

from conftest import ScriptedClient

pytestmark = pytest.mark.usefixtures("api_key")


def write_skill(directory: Path, description: str = "Deploy the service", body: str = "") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(f"---\ndescription: {description}\n---\n{body}", encoding="utf-8")
    return path


@pytest.fixture
def make_session(session_factory):
    def make(*overrides: tuple[str, object], trusted: bool = False) -> Session:
        session = session_factory(*overrides, trusted=trusted)
        session.client = ScriptedClient()
        return session

    return make


def test_catalog_is_in_the_system_message(make_session, home):
    path = write_skill(home / ".meltzer" / "skills" / "deploy")
    session = make_session()
    assert f"- deploy: Deploy the service (file: {path.resolve()})" in session.instructions
    session.run_turn("hello")
    assert "<skills_instructions>" in session.client.calls[0][0]["content"]


def test_include_instructions_false_leaves_out_the_catalog(make_session, home):
    write_skill(home / ".meltzer" / "skills" / "deploy", body="Run make deploy.\n")
    session = make_session(("skills.include_instructions", False))
    assert "<skills_instructions>" not in session.instructions
    session.run_turn("please $deploy")
    assert any(item.kind == Kind.SKILL for item in session.history)


def test_mention_adds_the_skill_after_the_prompt(make_session, home):
    write_skill(home / ".meltzer" / "skills" / "deploy", body="Run make deploy.\n")
    session = make_session()
    session.run_turn("please $deploy")
    users = [m for m in session.client.calls[0] if m["role"] == "user"]
    assert users[-2]["content"] == "please $deploy"
    assert users[-1]["content"].startswith("<skill>\n<name>deploy</name>")
    assert "Run make deploy." in users[-1]["content"]


def test_project_skills_need_a_trusted_project(make_session, work):
    write_skill(work / ".meltzer" / "skills" / "build")
    assert [s.name for s in make_session().skills.enabled()] == []
    assert [s.name for s in make_session(trusted=True).skills.enabled()] == ["build"]


def test_subagents_use_the_parent_skills(make_session, home):
    write_skill(home / ".meltzer" / "skills" / "deploy")
    session = make_session()
    child = session.start_subagent()
    assert child.skills is session.skills
    assert "- deploy:" in child.instructions
