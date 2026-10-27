from __future__ import annotations

import shutil
import socket
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from meltzer.exec import ProcessRunner
from meltzer.sandbox import bwrap
from meltzer.sandbox.bwrap import build_bwrap_args, exit_code_from_status
from meltzer.sandbox.policy import SandboxMode, SandboxPolicy
from meltzer.session import make_temp_dir

needs_bwrap = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bwrap") is None, reason="needs Linux with bubblewrap (bwrap)"
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    (path / ".meltzer").mkdir(parents=True)
    (path / ".git").mkdir()
    return path


def workspace_policy(root: Path, **kwargs) -> SandboxPolicy:
    return SandboxPolicy.for_session(SandboxMode.WORKSPACE_WRITE, root, **kwargs)


# --- policy --------------------------------------------------------------------------


def test_workspace_write_policy(root, tmp_path):
    temp = tmp_path / "session-tmp"
    temp.mkdir()
    policy = workspace_policy(root, temp_dir=temp)
    root = policy.root
    assert policy.is_sandboxed and not policy.allows_network
    assert policy.can_write(root / "src" / "main.py")
    assert policy.can_write(temp / "scratch.txt")
    assert set(policy.writable_roots()) == {root, temp.resolve()}
    assert not policy.can_write(Path("/tmp") / "scratch.txt")
    assert not policy.can_write(root.parent / "outside.txt")
    assert not policy.can_write(root / ".meltzer" / "config.toml")
    assert not policy.can_write(root / ".git" / "config")
    assert not policy.can_write(root / ".git")


def test_protected_paths(tmp_path):
    (tmp_path / ".git").mkdir()
    root = tmp_path / "repo" / "pkg"
    root.mkdir(parents=True)
    paths = workspace_policy(root).protected_paths()
    assert tmp_path.resolve() / ".git" in paths
    assert tmp_path.resolve() / "repo" / ".meltzer" in paths

    home = tmp_path / "me" / ".meltzer"
    home.mkdir(parents=True)
    policy = workspace_policy(tmp_path, meltzer_home=home)
    assert home.resolve() in policy.read_only_subpaths()
    assert not policy.can_write(home / "rules" / "default.rules")
    assert not policy.can_write(home / "config.toml")
    assert policy.can_write(tmp_path / "me" / "notes.txt")


def test_network_setting(root):
    # Network is reachable only outside the sandbox (full-access); both sandboxed modes block it.
    assert not SandboxPolicy.for_session(SandboxMode.WORKSPACE_WRITE, root).allows_network
    assert not SandboxPolicy.for_session(SandboxMode.READ_ONLY, root).allows_network
    assert SandboxPolicy.for_session(SandboxMode.FULL_ACCESS, root).allows_network


def test_read_only_and_full_access_policies(root):
    read_only = SandboxPolicy.for_session(SandboxMode.READ_ONLY, root)
    assert read_only.writable_roots() == ()
    assert not read_only.can_write(root / "a.txt")
    full = SandboxPolicy.for_session(SandboxMode.FULL_ACCESS, root)
    assert not full.is_sandboxed
    assert full.can_write(root / ".git" / "config")


def test_bwrap_arguments(root, monkeypatch):
    monkeypatch.setattr(bwrap.shutil, "which", lambda name: "/usr/bin/bwrap")
    policy = workspace_policy(root)
    args = build_bwrap_args(policy, ["bash", "-c", "true"], policy.root, status_fd=7)
    assert args[0] == "/usr/bin/bwrap"
    assert args[args.index("--ro-bind") + 1 : args.index("--ro-bind") + 3] == ["/", "/"]
    assert "--unshare-net" in args and "--unshare-ipc" in args and "--unshare-pid" in args
    bind = args.index(str(policy.root))
    protected = args.index(str(policy.root / ".meltzer"))
    assert args[bind - 1] == "--bind" and args[protected - 1] == "--ro-bind"
    assert bind < protected
    assert args[-4:] == ["--", "bash", "-c", "true"]


def test_bwrap_mounts_the_session_temp_dir_at_tmp(root, tmp_path, monkeypatch):
    monkeypatch.setattr(bwrap.shutil, "which", lambda name: "/usr/bin/bwrap")
    temp = tmp_path / "session-tmp"
    temp.mkdir()
    policy = workspace_policy(root, temp_dir=temp)
    args = build_bwrap_args(policy, ["true"], policy.root, status_fd=7)
    mount = args.index(str(policy.temp_dir))
    assert args[mount - 1 : mount + 2] == ["--bind", str(policy.temp_dir), "/tmp"]
    # /tmp is mounted before the session root, so a root under /tmp stays visible.
    assert mount < args.index(str(policy.root))
    assert not any(arg.startswith("/tmp/") for arg in args)
    assert "--remount-ro" not in args


def test_bwrap_tmp_without_temp_dir_and_in_read_only_mode(root, tmp_path, monkeypatch):
    monkeypatch.setattr(bwrap.shutil, "which", lambda name: "/usr/bin/bwrap")
    plain = build_bwrap_args(workspace_policy(root), ["true"], root, status_fd=7)
    assert plain[plain.index("/tmp") - 1] == "--tmpfs"
    temp = tmp_path / "session-tmp"
    temp.mkdir()
    read_only = SandboxPolicy.for_session(SandboxMode.READ_ONLY, root, temp_dir=temp)
    args = build_bwrap_args(read_only, ["true"], read_only.root, status_fd=7)
    assert args[args.index(str(read_only.temp_dir)) + 1] == "/tmp"
    assert args[args.index(str(read_only.root)) - 1] == "--ro-bind"
    assert args[args.index("--remount-ro") + 1] == "/tmp"


def test_session_temp_dir_is_private(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    path = make_temp_dir("abc")
    assert path == tmp_path / "meltzer-abc" and path.is_dir()
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o700


def test_bwrap_refuses_full_access(root):
    full = SandboxPolicy.for_session(SandboxMode.FULL_ACCESS, root)
    with pytest.raises(bwrap.SandboxSetupError):
        build_bwrap_args(full, ["true"], root, status_fd=7)


def test_exit_code_from_status():
    assert exit_code_from_status(b'{"child-pid": 12}\n{"exit-code": 3}\n') == 3
    assert exit_code_from_status(b'{"child-pid": 12}\n') is None
    assert exit_code_from_status(b"") is None


# --- commands under bwrap ----------------------------------------------------------------


def run(policy: SandboxPolicy, command: str, tmp_path: Path):
    runner = ProcessRunner(policy, log_dir=tmp_path / "logs", timeout=30)
    return runner.run(command, policy.root, sandboxed=True)


@needs_bwrap
def test_writes_in_root_succeed(root, tmp_path):
    result = run(workspace_policy(root), "echo hello > out.txt && cat out.txt", tmp_path)
    assert result.exit_code == 0 and "hello" in result.output
    assert (root / "out.txt").read_text() == "hello\n"


@needs_bwrap
@pytest.mark.parametrize("target", [".meltzer/config.toml", ".git/config"])
def test_root_meltzer_and_git_are_read_only(root, tmp_path, target):
    result = run(workspace_policy(root), f"echo x > {target}", tmp_path)
    assert result.exit_code != 0
    assert "Read-only file system" in result.output
    assert not (root / target).exists()


@needs_bwrap
def test_write_outside_root_fails(root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    policy = workspace_policy(root)
    if policy.can_write(outside):
        pytest.skip("the test directory is inside a writable root; run pytest with --basetemp outside /tmp")
    result = run(policy, f"echo x > {outside / 'escape.txt'}", tmp_path)
    assert result.exit_code != 0
    assert "Read-only file system" in result.output
    assert not (outside / "escape.txt").exists()


@needs_bwrap
def test_meltzer_home_inside_root_is_read_only(tmp_path):
    home = tmp_path / "me" / ".meltzer"
    (home / "rules").mkdir(parents=True)
    policy = workspace_policy(tmp_path, meltzer_home=home)
    result = run(policy, f"echo x >> {home / 'rules' / 'default.rules'}", tmp_path)
    assert result.exit_code != 0 and "Read-only file system" in result.output
    assert not (home / "rules" / "default.rules").exists()


@needs_bwrap
def test_read_only_mode(root, tmp_path):
    policy = SandboxPolicy.for_session(SandboxMode.READ_ONLY, root)
    result = run(policy, "echo x > out.txt", tmp_path)
    assert result.exit_code != 0 and not (root / "out.txt").exists()


@pytest.fixture
def tcp_server():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    accepted: list[socket.socket] = []

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            accepted.append(conn)
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    yield server.getsockname()[1], accepted
    server.close()


@needs_bwrap
def test_network_is_blocked(root, tmp_path, tcp_server):
    port, accepted = tcp_server
    connect = f"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=3); print('CONNECTED')"
    result = run(workspace_policy(root), f'{sys.executable} -c "{connect}"', tmp_path)
    assert result.exit_code != 0
    assert "CONNECTED" not in result.output
    assert accepted == []


@needs_bwrap
@pytest.mark.skipif(not Path("/run").is_dir(), reason="no /run directory")
def test_run_directory_is_empty(root, tmp_path):
    result = run(workspace_policy(root), "ls -A /run; echo END", tmp_path)
    assert result.exit_code == 0
    assert result.output == "END\n"


@pytest.fixture
def session_tmp(tmp_path: Path) -> Path:
    path = tmp_path / "session-tmp"
    path.mkdir(mode=0o700)
    return path


@needs_bwrap
def test_sandboxed_commands_share_the_session_tmp(root, tmp_path, session_tmp):
    policy = workspace_policy(root, temp_dir=session_tmp)
    first = run(policy, "echo shared > /tmp/note.txt", tmp_path)
    second = run(policy, "cat /tmp/note.txt", tmp_path)
    assert first.exit_code == 0 and second.output == "shared\n"
    assert (session_tmp / "note.txt").read_text() == "shared\n"


@needs_bwrap
def test_host_tmp_is_hidden_from_sandboxed_commands(root, tmp_path, session_tmp):
    host_dir = Path(tempfile.mkdtemp(prefix="meltzer-host-", dir="/tmp"))
    sock_path = host_dir / "agent.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(sock_path))
    server.listen()
    (host_dir / "file.txt").write_text("host\n")
    try:
        check = f"test -e {sock_path} && echo SOCKET; test -e {host_dir / 'file.txt'} && echo FILE; echo END"
        result = run(workspace_policy(root, temp_dir=session_tmp), check, tmp_path)
        unsandboxed = ProcessRunner(workspace_policy(root), log_dir=tmp_path / "logs").run(check, root, sandboxed=False)
    finally:
        server.close()
        shutil.rmtree(host_dir, ignore_errors=True)
    assert result.output == "END\n"
    assert "SOCKET" in unsandboxed.output and "FILE" in unsandboxed.output


@needs_bwrap
def test_read_only_mode_tmp_is_not_writable(root, tmp_path, session_tmp):
    policy = SandboxPolicy.for_session(SandboxMode.READ_ONLY, root, temp_dir=session_tmp)
    result = run(policy, "echo x > /tmp/x.txt", tmp_path)
    assert result.exit_code != 0 and "Read-only file system" in result.output
    assert not (session_tmp / "x.txt").exists()
