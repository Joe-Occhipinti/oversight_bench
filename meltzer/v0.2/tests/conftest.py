"""Fixtures and test doubles shared by the test modules."""

from __future__ import annotations

import io
import json
import shutil
import sys
import time
from pathlib import Path

import pytest

from meltzer.approval.orchestrator import ApprovalOrchestrator
from meltzer.approval.policy import (
    ApprovalPolicy,
    ApprovalRequest,
    ApprovalSettings,
    ReviewerKind,
    ReviewVerdict,
)
from meltzer.approval.rules import RuleSet
from meltzer.client import ModelResponse
from meltzer.config import load_config
from meltzer.exec import ProcessRunner
from meltzer.sandbox.policy import SandboxMode, SandboxPolicy
from meltzer.session import Session
from meltzer.ui import TerminalUI

needs_posix = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX processes and file APIs")
needs_bwrap = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bwrap") is None, reason="needs Linux with bubblewrap (bwrap)"
)


class ScriptedClient:
    """Stands in for the model client: answers with the given responses, then with "done"."""

    def __init__(self, *responses: ModelResponse):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    def complete(self, messages, tools, *, model=None) -> ModelResponse:
        self.calls.append([dict(m) for m in messages])
        return self.responses.pop(0) if self.responses else ModelResponse(content="done")

    def close(self) -> None:
        pass


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("MELTZER_API_KEY", "test-key")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


def skip_without_bwrap() -> None:
    if sys.platform != "linux" or shutil.which("bwrap") is None:
        pytest.skip("needs Linux with bubblewrap (bwrap)")


def run_sandboxed(policy: SandboxPolicy, command: str, tmp_path: Path):
    skip_without_bwrap()
    runner = ProcessRunner(policy, log_dir=tmp_path / "logs", timeout=30)
    return runner.run(command, policy.root, sandboxed=True)


def process_stops(pid: int, timeout: float = 5.0) -> bool:
    """Whether the process exits within `timeout` seconds (Linux /proc; a zombie counts as exited)."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            return True
        if state == "Z" or time.monotonic() > deadline:
            return state == "Z"
        time.sleep(0.05)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    (path / ".meltzer").mkdir(parents=True)
    (path / ".git").mkdir()
    return path


class RecordingReviewer:
    """Answers every review the same way and keeps the requests."""

    def __init__(self, approved: bool = True, **verdict_fields):
        self.approved = approved
        self.verdict_fields = verdict_fields
        self.requests: list[ApprovalRequest] = []

    def review(self, request: ApprovalRequest) -> ReviewVerdict:
        self.requests.append(request)
        rationale = "approved by user" if self.approved else "denied by user"
        return ReviewVerdict(approved=self.approved, rationale=rationale, **self.verdict_fields)


def make_orchestrator(
    work: Path,
    reviewer,
    *,
    policy: ApprovalPolicy = ApprovalPolicy.ON_REQUEST,
    mode: SandboxMode = SandboxMode.WORKSPACE_WRITE,
    rules=(),
    hooks=None,
    **sandbox,
) -> ApprovalOrchestrator:
    """An orchestrator with the given sandbox mode and approval policy that sends reviews to `reviewer`."""
    return ApprovalOrchestrator(
        ApprovalSettings(policy=policy, reviewer=ReviewerKind.USER),
        SandboxPolicy.for_session(mode, work, **sandbox),
        reviewer,
        RuleSet(list(rules), work.parent / "rules"),
        hooks=hooks,
    )


@pytest.fixture
def session_factory(tmp_path, home, work, monkeypatch):
    """Opens sessions in `work` with approval_policy never; the sessions are closed at the end of the test."""
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))  # the session's private /tmp is created under tmp_path
    sessions: list[Session] = []

    def make(*overrides: tuple[str, object], trusted: bool = False, ui: TerminalUI | None = None, **kwargs) -> Session:
        if trusted:
            (home / ".meltzer").mkdir(exist_ok=True)
            (home / ".meltzer" / "config.toml").write_text(
                f'[projects.{json.dumps(str(work))}]\ntrust_level = "trusted"\n', encoding="utf-8"
            )
        config = load_config(work, home=home, overrides=[("approval_policy", "never"), *overrides])
        ui = ui or TerminalUI(interactive=False, stderr=io.StringIO())
        session = Session(config, work, ui, home=home, **kwargs)
        sessions.append(session)
        return session

    yield make
    for session in reversed(sessions):
        session.close()
