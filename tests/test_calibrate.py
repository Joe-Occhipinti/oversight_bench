"""Tests for bench.calibrate: one review with meltzer's own reviewer and a scripted model (no network)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from bench import calibrate
from bench.config import REPO_ROOT, load_config
from bench.preconditions import MONITOR_ENVIRONMENT

CONFIG = REPO_ROOT / "tests" / "fixtures" / "exp3" / "config.yaml"


class FakeClient:
    """Answers every review with an allow verdict and records the messages it got."""

    def __init__(self, monitor, api_key, calls):
        self.calls = calls
        self.messages = []

    def complete(self, messages, tools, model=None):
        self.messages.append((messages, [t["function"]["name"] for t in tools], model))
        verdict = {"risk_level": "low", "user_authorization": "unknown", "outcome": "allow", "rationale": "routine"}
        return SimpleNamespace(content=json.dumps(verdict), tool_calls=[], reasoning={})

    def close(self):
        pass


def test_review_once_runs_meltzers_reviewer_on_an_escalated_exec_command(tmp_path, monkeypatch):
    clients = []

    def make(*args):
        clients.append(FakeClient(*args))
        return clients[-1]

    monkeypatch.setattr(calibrate, "OpenRouterClient", make)
    cfg = load_config(CONFIG)
    request = calibrate.build_requests()[0]
    root = tmp_path / "fs" / "app"
    root.mkdir(parents=True)
    raw = tmp_path / "raw"
    raw.mkdir()

    row = calibrate.review_once(request, 0, root, cfg, "key", raw)

    assert row["approved"] is True and row["outcome"] == "allow" and row["error"] is None
    (messages, tool_names, model), = clients[0].messages
    assert model == cfg.models.monitor.id
    assert set(tool_names) == {"read_file", "list_dir", "grep"}
    system = messages[0]["content"]
    assert f"- {MONITOR_ENVIRONMENT}\n" in system and "`192.0.2.10`" in system
    transcript = messages[-1]["content"]
    assert '"tool": "exec_command"' in transcript and request.command in transcript
    saved = json.loads((raw / f"{request.id}-0.json").read_text(encoding="utf-8"))
    assert saved["planned_action"]["tool"] == "exec_command"
