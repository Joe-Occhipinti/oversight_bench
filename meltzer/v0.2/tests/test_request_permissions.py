from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from meltzer.approval.orchestrator import ApprovalOrchestrator
from meltzer.approval.policy import (
    ApprovalPolicy,
)
from meltzer.exec import ExecResult, ProcessRunner
from meltzer.history import SessionRecorder
from meltzer.sandbox.grants import Grant, GrantScope, GrantStore, NetworkTarget, parse_network_target
from meltzer.sandbox.policy import SandboxPolicy
from meltzer.tools.apply_patch import ApplyPatchTool
from meltzer.tools import request_permissions
from meltzer.tools.registry import ToolError
from meltzer.tools.request_permissions import RequestPermissionsTool
from meltzer.tools.shell import ShellTool

from conftest import RecordingReviewer, make_orchestrator, needs_bwrap, needs_posix

class FakeRunner:
    """Records the policy each command is run with."""

    timeout = 30.0

    def __init__(self):
        self.policies: list[SandboxPolicy | None] = []

    def run(self, command, cwd, sandboxed, policy=None, network=None) -> ExecResult:
        self.policies.append(policy)
        return ExecResult(exit_code=0, output="", duration=0.0)

    def start_background(self, command, cwd, sandboxed, policy=None, network=None):
        self.policies.append(policy)
        raise OSError("not started")


@pytest.fixture
def work(root: Path) -> Path:
    return root


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home" / ".meltzer"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    path = tmp_path / "cache"
    path.mkdir()
    return path


def make_tool(orchestrator: ApprovalOrchestrator, recorder: SessionRecorder | None = None) -> RequestPermissionsTool:
    return RequestPermissionsTool(orchestrator.sandbox.root, orchestrator, GrantStore(orchestrator, recorder))


def refused(work: Path, arguments: dict, message: str, **kwargs) -> None:
    """The request is refused before any review and the sandbox policy stays as it was."""
    reviewer = RecordingReviewer()
    orchestrator = make_orchestrator(work, reviewer, **kwargs)
    before = orchestrator.sandbox
    with pytest.raises(ToolError, match=message):
        make_tool(orchestrator).run(arguments)
    assert reviewer.requests == [] and orchestrator.sandbox == before


def granted(output: str) -> dict:
    return json.loads(output)


def symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create symbolic links here")


# --- network targets -------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, host, port",
    [
        ("pypi.org", "pypi.org", None),
        ("Files.PythonHosted.org:443", "files.pythonhosted.org", 443),
        ("pypi.org.", "pypi.org", None),
        ("localhost:8000", "localhost", 8000),
        ("10.0.0.5", "10.0.0.5", None),
        ("10.0.0.5:8080", "10.0.0.5", 8080),
        ("2001:db8::1", "2001:db8::1", None),
        ("[2001:db8::1]:443", "2001:db8::1", 443),
        ("[::1]", "::1", None),
    ],
)
def test_network_target_is_one_host_or_address(text, host, port):
    assert parse_network_target(text) == NetworkTarget(host, port)


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "*",
        "*.example.com",
        "example.*",
        "exa*mple.com",
        "https://example.com",
        "example.com/simple",
        "user@example.com",
        "example.com other.com",
        "example.com,other.com",
        "example.com:0",
        "example.com:65536",
        "example.com:https",
        "example.com:",
        "-example.com",
        "example..com",
        "1.2.3",
        "300.1.1.1",
        "[example.com]:443",
        "[2001:db8::1",
        "a" * 64 + ".com",
    ],
)
def test_network_target_rejects_anything_else(text):
    with pytest.raises(ValueError):
        parse_network_target(text)


def test_network_target_text():
    assert str(NetworkTarget("pypi.org", 443)) == "pypi.org:443"
    assert str(NetworkTarget("pypi.org")) == "pypi.org"
    assert str(NetworkTarget("2001:db8::1", 443)) == "[2001:db8::1]:443"
    assert str(NetworkTarget("2001:db8::1")) == "2001:db8::1"


# --- argument validation ----------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments, message",
    [
        ({"network": {"target": "pypi.org"}}, "'reason'"),
        ({"reason": "  ", "network": {"target": "pypi.org"}}, "'reason'"),
        ({"reason": "install"}, "at least one"),
        ({"reason": "install", "write": []}, "at least one"),
        ({"reason": "install", "network": "pypi.org"}, "'network'"),
        ({"reason": "install", "network": {}}, "'target'"),
        ({"reason": "install", "network": {"target": 443}}, "'target'"),
        ({"reason": "install", "network": {"target": "pypi.org", "port": 443}}, "unknown key"),
        ({"reason": "install", "network": {"target": "*.pypi.org"}}, "wildcard"),
        ({"reason": "install", "network": {"target": "pypi.org files.pythonhosted.org"}}, "single host"),
        ({"reason": "install", "write": "build"}, "'write'"),
        ({"reason": "install", "write": ["build", 3]}, "'write'"),
        ({"reason": "install", "write": [""]}, "'write'"),
        ({"reason": "install", "write": ["does-not-exist"]}, "does not exist"),
    ],
)
def test_invalid_arguments_are_rejected_before_review(work, arguments, message):
    refused(work, arguments, message)


@pytest.mark.parametrize("path", [".git", ".git/hooks", ".meltzer", ".meltzer/rules", "src/.git", "../.git"])
def test_write_to_a_protected_path_is_refused(work, path):
    (work / path).mkdir(parents=True, exist_ok=True)
    refused(work, {"reason": "install a pre-commit hook", "write": [path]}, "protected path")


@pytest.mark.parametrize("nested", [".git", "app/.meltzer", "a/b/c/d/.git/hooks"])
def test_write_to_a_directory_that_contains_a_protected_path_is_refused(work, tmp_path, nested):
    other = tmp_path / "other"
    (other / nested).mkdir(parents=True)
    refused(work, {"reason": "edit the sibling repository", "write": [str(other)]}, "contains the protected path")


@pytest.mark.parametrize("failure", ["too many entries", "cannot read"])
def test_write_to_a_directory_that_cannot_be_checked_is_refused(work, tmp_path, monkeypatch, failure):
    other = tmp_path / "other"
    (other / "docs").mkdir(parents=True)
    if failure == "too many entries":
        monkeypatch.setattr(request_permissions, "MAX_SCAN_ENTRIES", 0)
    else:

        def walk(top, onerror, followlinks):
            onerror(PermissionError(13, "Permission denied", str(top)))

        monkeypatch.setattr(os, "walk", walk)
    refused(work, {"reason": "edit the sibling repository", "write": [str(other)]}, failure)


@needs_posix
@pytest.mark.parametrize("path", ["/", "/run", "/var/run", "/dev", "/dev/shm", "/proc", "/sys", "/tmp"])
def test_write_to_system_directories_is_refused(work, path):
    if not Path(path).exists():
        pytest.skip(f"{path} does not exist here")
    refused(work, {"reason": "write there", "write": [path]}, "system directory")


def test_write_to_the_meltzer_home_is_refused(work, home):
    (home / "rules").mkdir()
    for path in (home, home / "rules"):
        refused(work, {"reason": "save settings", "write": [str(path)]}, "protected path", meltzer_home=home)


def test_write_through_a_symlink_to_a_protected_path_is_refused(work):
    symlink_or_skip(work / "hooks", work / ".git")
    refused(work, {"reason": "install hooks", "write": ["hooks"]}, "protected path")


# --- review -----------------------------------------------------------------------------


def test_review_request_shows_reason_target_and_paths(work, cache):
    (work / "build").mkdir()
    reviewer = RecordingReviewer()
    tool = make_tool(make_orchestrator(work, reviewer))
    tool.run(
        {
            "reason": "download wheels and write them to the cache",
            "network": {"target": "files.pythonhosted.org:443"},
            "write": [str(cache), "build", "build"],
        }
    )
    (request,) = reviewer.requests
    action = request.planned_action()
    assert action["tool"] == "request_permissions"
    assert action["justification"] == "download wheels and write them to the cache"
    assert action["permissions"] == {
        "network": {"target": "files.pythonhosted.org:443"},
        "write": [str(cache.resolve()), str((work / "build").resolve())],
    }
    assert action["cwd"] == str(work.resolve())


@pytest.mark.parametrize("never", [True, False])
def test_unapproved_request_gives_an_empty_grant(work, cache, never):
    reviewer = RecordingReviewer(approved=False)
    policy = ApprovalPolicy.NEVER if never else ApprovalPolicy.ON_REQUEST
    orchestrator = make_orchestrator(work, reviewer, policy=policy)
    tool = make_tool(orchestrator)
    before = orchestrator.sandbox
    output = tool.run({"reason": "cache", "network": {"target": "pypi.org"}, "write": [str(cache)]})
    assert granted(output) == {"permissions": {}, "scope": "turn"}
    assert (reviewer.requests == []) == never
    assert orchestrator.sandbox == before
    assert tool.grants.grants == ()


# --- grants -----------------------------------------------------------------------------


def test_write_grant_widens_the_writable_paths(work, cache):
    orchestrator = make_orchestrator(work, RecordingReviewer())
    base = orchestrator.sandbox
    tool = make_tool(orchestrator)
    output = tool.run({"reason": "cache", "write": [str(cache)]})
    assert granted(output) == {"permissions": {"write": [str(cache.resolve())]}, "scope": "turn"}
    assert orchestrator.sandbox.can_write(cache / "pkg.whl")
    assert not base.can_write(cache / "pkg.whl")
    assert orchestrator.sandbox.root == base.root and orchestrator.sandbox.mode == base.mode


def test_relative_paths_and_symlinks_are_resolved_when_requested(work, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    link = work / "out"
    symlink_or_skip(link, first)
    orchestrator = make_orchestrator(work, RecordingReviewer())
    tool = make_tool(orchestrator)
    tool.run({"reason": "build output", "write": ["out"]})
    (grant,) = tool.grants.grants
    assert grant.write == (first.resolve(),)
    link.unlink()
    link.symlink_to(second, target_is_directory=True)
    assert orchestrator.sandbox.can_write(first / "a.o")
    assert not orchestrator.sandbox.can_write(second / "a.o")


def test_protected_paths_stay_read_only_under_a_write_grant(work, tmp_path):
    other = tmp_path / "other"
    (other / ".git" / "hooks").mkdir(parents=True)
    (other / ".meltzer").mkdir()
    (other / "src").mkdir()
    orchestrator = make_orchestrator(work, RecordingReviewer())
    make_tool(orchestrator).run({"reason": "edit the sibling source", "write": [str(other / "src")]})
    sandbox = orchestrator.sandbox
    assert sandbox.can_write(other / "src" / "main.py")
    for path in (other / ".git" / "hooks" / "pre-commit", other / ".meltzer" / "config.toml",
                 work / ".git" / "config", work / ".meltzer" / "config.toml"):
        assert not sandbox.can_write(path)


def test_network_grant_records_the_target(work, tmp_path):
    recorder = SessionRecorder(tmp_path / "session.jsonl")
    orchestrator = make_orchestrator(work, RecordingReviewer())
    tool = make_tool(orchestrator, recorder)
    output = tool.run({"reason": "install the package from PyPI", "network": {"target": "pypi.org:443"}})
    recorder.close()
    assert granted(output) == {"permissions": {"network": {"target": "pypi.org:443"}}, "scope": "turn"}
    (grant,) = tool.grants.grants
    assert grant.network == NetworkTarget("pypi.org", 443)
    assert grant.reason == "install the package from PyPI"
    records = [json.loads(line) for line in (tmp_path / "session.jsonl").read_text().splitlines()]
    (event,) = [r for r in records if r["type"] == "permissions_granted"]
    assert event["permissions"] == {"network": {"target": "pypi.org:443"}}
    assert event["scope"] == "turn"
    assert event["reason"] == "install the package from PyPI"


def test_turn_grants_end_with_the_turn(work, cache, tmp_path):
    kept = tmp_path / "kept"
    kept.mkdir()
    orchestrator = make_orchestrator(work, RecordingReviewer())
    base = orchestrator.sandbox
    store = GrantStore(orchestrator)
    store.add(Grant(scope=GrantScope.TURN, write=(cache.resolve(),)))
    store.add(Grant(scope=GrantScope.SESSION, write=(kept.resolve(),), network=NetworkTarget("pypi.org")))
    assert orchestrator.sandbox.can_write(cache / "a") and orchestrator.sandbox.can_write(kept / "a")
    store.end_turn()
    assert not orchestrator.sandbox.can_write(cache / "a")
    assert orchestrator.sandbox.can_write(kept / "a")
    assert [(g.scope, g.network) for g in store.grants] == [(GrantScope.SESSION, NetworkTarget("pypi.org"))]
    store.clear()
    assert orchestrator.sandbox == base and store.grants == ()


def test_later_commands_and_patches_use_the_granted_policy_without_review(work, cache):
    reviewer = RecordingReviewer()
    orchestrator = make_orchestrator(work, reviewer)
    runner = FakeRunner()
    shell = ShellTool(orchestrator.sandbox.root, orchestrator, runner)
    shell.run({"command": "true"})
    assert runner.policies == [orchestrator.sandbox]

    make_tool(orchestrator).run({"reason": "cache", "write": [str(cache)]})
    assert len(reviewer.requests) == 1
    shell.run({"command": f"cp dist/pkg.whl {cache}"})
    assert runner.policies[-1] == orchestrator.sandbox
    assert runner.policies[-1].can_write(cache / "pkg.whl")
    with pytest.raises(ToolError):
        shell.run({"command": "sleep 100", "background": True})
    assert runner.policies[-1] == orchestrator.sandbox
    assert len(reviewer.requests) == 1


@needs_posix
def test_patch_inside_a_granted_path_is_applied_without_review(work, cache):
    reviewer = RecordingReviewer()
    orchestrator = make_orchestrator(work, reviewer)
    make_tool(orchestrator).run({"reason": "cache", "write": [str(cache)]})
    patch = f"*** Begin Patch\n*** Add File: {cache / 'index.txt'}\n+pkg\n*** End Patch\n"
    ApplyPatchTool(orchestrator.sandbox.root, orchestrator).run({"patch": patch})
    assert (cache / "index.txt").read_text() == "pkg\n"
    assert len(reviewer.requests) == 1


def test_grants_are_not_shared_between_agents(work, cache, tmp_path):
    parent = make_orchestrator(work, RecordingReviewer())
    child = make_orchestrator(work, RecordingReviewer())
    runner = ProcessRunner(parent.sandbox, log_dir=tmp_path / "logs")
    make_tool(child).run({"reason": "cache", "write": [str(cache)]})
    assert child.sandbox.can_write(cache / "a")
    assert not parent.sandbox.can_write(cache / "a")
    assert not runner.policy.can_write(cache / "a")


# --- in the sandbox -----------------------------------------------------------------------


@needs_bwrap
def test_sandboxed_commands_use_the_grant(work, cache, tmp_path):
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    (other / "src").mkdir()
    parent = make_orchestrator(work, RecordingReviewer())
    child = make_orchestrator(work, RecordingReviewer())
    runner = ProcessRunner(parent.sandbox, log_dir=tmp_path / "logs", timeout=30)
    parent_shell = ShellTool(parent.sandbox.root, parent, runner)
    child_shell = ShellTool(child.sandbox.root, child, runner)
    make_tool(child).run({"reason": "cache", "write": [str(cache), str(other / "src")]})

    assert "Exit code: 0" in child_shell.run({"command": f"echo x > {cache / 'a.txt'}"})
    assert (cache / "a.txt").read_text() == "x\n"
    assert "Exit code: 0" not in child_shell.run({"command": f"echo x > {other / '.git' / 'config'}"})
    assert not (other / ".git" / "config").exists()
    assert "Exit code: 0" not in parent_shell.run({"command": f"echo y > {cache / 'b.txt'}"})
    assert not (cache / "b.txt").exists()
