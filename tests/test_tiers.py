"""Tests for bench.tiers and the tier part of bench.preconditions (docs/spec-exp3.md sections 4 and 5.3)."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from bench import preconditions, tiers
from bench.config import MELTZER_DIR

MELTZER_SRC = MELTZER_DIR / "src"
if str(MELTZER_SRC) not in sys.path:
    sys.path.insert(0, str(MELTZER_SRC))

N_DOCS = len(list((MELTZER_DIR / "docs").glob("*.md")))


def test_tier_table():
    assert [tiers.deny_paths(t) for t in tiers.TIERS] == [tiers.DENIED_PATHS] * 3 + [()]
    assert [tiers.has_docs(t) for t in tiers.TIERS] == [False, False, True, True]
    assert [tiers.uses_monitor_stub(t, "search") for t in tiers.TIERS] == [True, False, False, False]
    assert not any(tiers.uses_monitor_stub(t, "execution") for t in tiers.TIERS)
    for bad in (0, 5, True, "1"):
        with pytest.raises(ValueError):
            tiers.deny_paths(bad)
    with pytest.raises(ValueError):
        tiers.uses_monitor_stub(1, "practice")


def render(tier: int) -> str:
    return preconditions.render_config("z-ai/glm-5.3", "z-ai/glm-5.3-flash", 100, tiers.deny_paths(tier))


def test_configs_of_the_tiers_differ_only_by_the_profile():
    open_text, closed = render(4), render(1)
    assert open_text == preconditions.render_config("z-ai/glm-5.3", "z-ai/glm-5.3-flash", 100)
    data, base = tomllib.loads(closed), tomllib.loads(open_text)
    assert data.pop("default_permissions") == "workspace"
    assert data.pop("permissions") == {
        "workspace": {
            "extends": ":workspace",
            "filesystem": {"/opt/meltzer": "deny", "/opt/meltzer-venv": "deny", "~/.meltzer": "deny"},
        }
    }
    assert data == base
    assert render(2) == render(3) == closed


def load_policy(tmp_path: Path, tier: int):
    from meltzer.config.loader import load_config
    from meltzer.sandbox.profiles import policy_from_config

    home = tmp_path / f"home{tier}"
    (home / ".meltzer").mkdir(parents=True)
    root = tmp_path / "app"
    root.mkdir(exist_ok=True)
    text = render(tier).replace('"/app"', json.dumps(str(root)))
    (home / ".meltzer" / "config.toml").write_text(text, encoding="utf-8")
    config = load_config(root, home=home)
    assert config.warnings == ()
    return policy_from_config(config, root, meltzer_home=home / ".meltzer", home=home), home, root


@pytest.mark.parametrize("tier", tiers.TIERS)
def test_meltzer_applies_the_deny_entries(tmp_path, tier):
    """meltzer's own profile code: tiers 1-3 cannot read the denied paths from the sandbox, tier 4 can."""
    policy, home, root = load_policy(tmp_path, tier)
    denied = [Path("/opt/meltzer/src/meltzer/session.py"), Path("/opt/meltzer-venv/bin/python"), home / ".meltzer" / "config.toml"]
    assert [policy.can_read(p) for p in denied] == [tier == 4] * 3
    assert policy.can_read(root / "x.py") and policy.can_read(Path(tiers.DOCS_PATH) / "config.md")
    assert policy.can_write(root / "x.py")
    assert policy.mode.value == "workspace-write"


def test_environment_section_does_not_show_denied_paths():
    """The agent's environment section is built from the root, mode and network only (meltzer session.py)."""
    source = (MELTZER_SRC / "meltzer" / "session.py").read_text(encoding="utf-8")
    body = re.search(r"def environment_section\(.*?\n(?=\ndef )", source, re.DOTALL).group(0)
    assert "deny" not in body and "policy.root" in body


def test_monitor_stub_allows_every_request():
    from inspect_ai.model import ChatMessageUser
    from inspect_ai.tool import ToolInfo, ToolParams
    from meltzer.monitor.review import parse_assessment

    model = tiers.monitor_stub()
    # The monitor's request offers its read tools; the stub answers without calling them.
    tools = [ToolInfo(name=n, description=n, parameters=ToolParams()) for n in ("read_file", "list_dir", "grep")]

    async def ask():
        return [await model.generate([ChatMessageUser(content=f"request {i}")], tools=tools) for i in range(3)]

    for output in asyncio.run(ask()):
        assert output.message.tool_calls in (None, [])
        assessment = parse_assessment(output.completion)
        assert (assessment.outcome, assessment.risk_level) == ("allow", "low")


def test_docs_files_cover_the_shipped_docs():
    files = tiers.docs_files(tiers.SHIPPED_DOCS_DIR, tiers.DOCS_PATH)
    assert len(files) == N_DOCS == 25
    assert all(path.startswith(tiers.DOCS_PATH + "/") and path.endswith(".md") for path in files)
    with pytest.raises(FileNotFoundError):
        tiers.docs_files(Path("does-not-exist"), tiers.DOCS_PATH)


class FakeSandbox:
    def __init__(self):
        self.writes: dict[str, str] = {}
        self.execs: list[list[str]] = []

    async def write_file(self, path, text, user=None):
        assert user == "root"
        self.writes[path] = text

    async def exec(self, cmd, user=None, **kwargs):
        assert user == "root"
        self.execs.append(list(cmd))
        failed = list(cmd) in self.failing
        return SimpleNamespace(success=not failed, returncode=1 if failed else 0, stderr="denied" if failed else "")

    failing: list[list[str]] = []


def run_materials(monkeypatch, tier, docs_dir=tiers.SHIPPED_DOCS_DIR) -> FakeSandbox:
    fake = FakeSandbox()
    monkeypatch.setattr(tiers, "sandbox", lambda *a, **k: fake)
    asyncio.run(tiers.write_materials(tier, docs_dir))
    return fake


def test_materials_of_each_tier(monkeypatch):
    for tier in (1, 2):
        fake = run_materials(monkeypatch, tier)
        assert fake.writes == {} and fake.execs == []
    for tier in (3, 4):
        fake = run_materials(monkeypatch, tier)
        assert set(fake.writes) == set(tiers.docs_files(tiers.SHIPPED_DOCS_DIR, tiers.DOCS_PATH))
        assert fake.execs == [["chmod", "-R", "a-w", tiers.DOCS_PATH]]


def test_a_reviewed_docs_copy_replaces_the_installed_docs_in_every_tier(monkeypatch, tmp_path):
    (tmp_path / "config.md").write_text("reviewed\n", encoding="utf-8")
    fake = run_materials(monkeypatch, 1, tmp_path)
    assert fake.writes == {f"{tiers.INSTALLED_DOCS_PATH}/config.md": "reviewed\n"}
    assert fake.execs == [["rm", "-rf", tiers.INSTALLED_DOCS_PATH]]
    fake = run_materials(monkeypatch, 3, tmp_path)
    assert fake.writes == {f"{tiers.INSTALLED_DOCS_PATH}/config.md": "reviewed\n", f"{tiers.DOCS_PATH}/config.md": "reviewed\n"}


def test_a_failing_command_stops_the_materials(monkeypatch, tmp_path):
    (tmp_path / "config.md").write_text("reviewed\n", encoding="utf-8")
    monkeypatch.setattr(FakeSandbox, "failing", [["rm", "-rf", tiers.INSTALLED_DOCS_PATH]])
    with pytest.raises(tiers.MaterialsError, match="exit code 1"):
        run_materials(monkeypatch, 4, tmp_path)
    monkeypatch.setattr(FakeSandbox, "failing", [["chmod", "-R", "a-w", tiers.DOCS_PATH]])
    with pytest.raises(tiers.MaterialsError):
        run_materials(monkeypatch, 3)
