from __future__ import annotations

import shutil
import subprocess
import sys
import time

import pytest

from meltzer.utils import git
from meltzer.utils.git import GitResult, git_info, run_git
from meltzer.utils.truncate import truncate_output

from conftest import process_stops

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


@pytest.fixture(autouse=True)
def no_parent_repository(tmp_path, monkeypatch):
    # git must not find a repository above the test directory.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))


def make_repo(path):
    def run(*args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
            cwd=path, check=True, capture_output=True,
        )

    run("init", "-q", "-b", "main")
    (path / "a.txt").write_text("one\n")
    run("add", "a.txt")
    run("commit", "-q", "-m", "first")
    return run


def test_truncate_keeps_short_text_and_the_start_and_end_of_long_text():
    assert truncate_output("abc", 3) == "abc"
    cut = truncate_output("a" * 50 + "b" * 50, 10)
    assert cut.startswith("aaaaa") and cut.endswith("bbbbb") and "[... 90 characters omitted ...]" in cut


def test_git_runs_with_hardening_options_and_environment(tmp_path, monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))
            self.returncode = 0

        def communicate(self, timeout=None):
            return b"out", b""

    monkeypatch.setattr(git.shutil, "which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(git.subprocess, "Popen", FakePopen)
    assert run_git(["status"], tmp_path) == GitResult(0, b"out", b"")
    (argv, kwargs), = calls
    assert argv[:5] == ["git", "-c", "core.fsmonitor=false", "-c", "safe.bareRepository=explicit"]
    assert argv[-1] == "status"
    assert kwargs["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["cwd"] == tmp_path
    assert kwargs["start_new_session"] is True


@needs_git
def test_command_line_options_override_repository_config(tmp_path):
    run = make_repo(tmp_path)
    run("config", "core.fsmonitor", "./monitor.sh")
    assert run_git(["config", "core.fsmonitor"], tmp_path).stdout.strip() == b"false"


def test_no_git_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(git.shutil, "which", lambda name: None)
    assert run_git(["status"], tmp_path) is None
    assert git_info(tmp_path) is None


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX only")
def test_timeout_kills_the_process_group(tmp_path, monkeypatch):
    pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"open({str(pid_file)!r}, 'w').write(str(p.pid)); time.sleep(60)"
    )
    popen = subprocess.Popen
    monkeypatch.setattr(git.subprocess, "Popen", lambda argv, **kw: popen([sys.executable, "-c", script], **kw))
    started = time.monotonic()
    assert run_git(["status"], tmp_path, timeout=1.0) is None
    assert time.monotonic() - started < 10
    assert process_stops(int(pid_file.read_text())), "the child of the timed-out process is still running"


@needs_git
def test_git_info_outside_a_repository(tmp_path):
    assert git_info(tmp_path) is None


@needs_git
def test_git_info_of_a_repository_leaves_out_credentials_of_remotes(tmp_path):
    run = make_repo(tmp_path)
    run("remote", "add", "origin", "https://user:token@example.com/org/repo.git")
    run("remote", "add", "fork", "git@example.com:me/repo.git")
    info = git_info(tmp_path)
    assert len(info["commit"]) == 40
    assert info["branch"] == "main"
    assert info["remotes"] == {"fork": "git@example.com:me/repo.git", "origin": "https://example.com/org/repo.git"}


@needs_git
def test_git_info_with_a_detached_head_and_no_remotes(tmp_path):
    run = make_repo(tmp_path)
    run("checkout", "-q", "--detach")
    assert set(git_info(tmp_path)) == {"commit"}
