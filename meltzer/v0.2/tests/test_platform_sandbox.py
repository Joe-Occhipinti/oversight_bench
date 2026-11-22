from __future__ import annotations

import sys
from pathlib import Path

import pytest

from meltzer.sandbox import SandboxBackend, select_backend
from meltzer.sandbox.bwrap import SandboxSetupError
from meltzer.sandbox.policy import NetworkMode, SandboxMode, SandboxPolicy
from meltzer.sandbox.seatbelt import SEATBELT_EXECUTABLE, build_seatbelt_policy, seatbelt_command
from meltzer.sandbox.windows import WindowsSandboxMode, compute_allow_deny_paths


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    (path / ".git").mkdir(parents=True)
    return path


def workspace_policy(root: Path, **kwargs) -> SandboxPolicy:
    return SandboxPolicy.for_session(SandboxMode.WORKSPACE_WRITE, root, **kwargs)


def param_key(params: dict[str, str], path: Path) -> str:
    return next(key for key, value in params.items() if value == str(path))


# --- Seatbelt ------------------------------------------------------------------------


def test_seatbelt_command_layout(root):
    policy = workspace_policy(root)
    text, params = build_seatbelt_policy(policy, root)
    argv = seatbelt_command(policy, ["bash", "-c", "true"], root)
    assert argv[:3] == [SEATBELT_EXECUTABLE, "-p", text]
    assert argv[3:-4] == [f"-D{key}={value}" for key, value in params.items()]
    assert argv[-4:] == ["--", "bash", "-c", "true"]


def test_policy_denies_by_default_and_reads_everywhere(root):
    text, _ = build_seatbelt_policy(workspace_policy(root), root)
    assert text.startswith("(version 1)") and "(deny default)" in text and "(allow file-read*)" in text
    assert "network-outbound" not in text and "network-inbound" not in text and "com.apple.SecurityServer" not in text


def test_writable_root_excludes_protected_names(root):
    policy = workspace_policy(root)
    text, params = build_seatbelt_policy(policy, root)
    root_key = param_key(params, policy.root)
    assert f'(subpath (param "{root_key}"))' in text
    for name in (".git", ".meltzer"):
        key = param_key(params, policy.root / name)
        assert f'(require-not (literal (param "{key}")))' in text
        assert f'(require-not (subpath (param "{key}")))' in text


def test_extra_writable_and_read_only_paths(root, tmp_path):
    extra = tmp_path / "extra"
    extra.mkdir()
    policy = workspace_policy(root, extra_writable=(extra,), read_only_extra=(root / "docs",))
    text, params = build_seatbelt_policy(policy, root)
    assert f'(subpath (param "{param_key(params, policy.extra_writable[0])}"))' in text
    assert f'(require-not (subpath (param "{param_key(params, policy.read_only_extra[0])}")))' in text
    assert f'(require-not (subpath (param "{param_key(params, policy.extra_writable[0] / ".git")}")))' in text


def test_read_only_mode_has_no_write_rule(root):
    text, params = build_seatbelt_policy(SandboxPolicy.for_session(SandboxMode.READ_ONLY, root), root)
    assert "(allow file-write*\n" not in text
    assert params == {}


def test_denied_paths_are_excluded_from_reads(root):
    policy = workspace_policy(root, deny_read=(root / "secrets",))
    text, params = build_seatbelt_policy(policy, root)
    assert "(allow file-read*)" not in text and params["READABLE_ROOT_0"] == "/"
    assert f'(require-not (subpath (param "{param_key(params, policy.deny_read[0])}")))' in text


def test_network_rules(root, tmp_path):
    text, _ = build_seatbelt_policy(workspace_policy(root, network=NetworkMode.FULL), root)
    assert "(allow network-outbound)" in text and "com.apple.SecurityServer" in text
    policy = workspace_policy(root, network=NetworkMode.PROXY)
    text, params = build_seatbelt_policy(policy, root, proxy_dir=tmp_path / "net")
    assert params["PROXY_SOCKET_DIR"] == str(tmp_path / "net")
    assert '(allow network-outbound (remote unix-socket (subpath (param "PROXY_SOCKET_DIR"))))' in text
    assert "(allow network-outbound)" not in text and "remote ip" not in text
    assert "network-outbound" not in build_seatbelt_policy(policy, root)[0]


def test_paths_are_parameters_not_policy_text(tmp_path):
    name = 'w ) (allow default) ; #' + ('"' if sys.platform != "win32" else "")
    policy = workspace_policy(tmp_path / name)
    text, params = build_seatbelt_policy(policy, policy.root)
    assert name not in text
    assert "(allow default)" not in text
    assert str(policy.root) in params.values()


def test_full_access_and_denied_cwd_are_refused(root):
    with pytest.raises(SandboxSetupError):
        build_seatbelt_policy(SandboxPolicy.for_session(SandboxMode.FULL_ACCESS, root), root)
    with pytest.raises(SandboxSetupError):
        build_seatbelt_policy(workspace_policy(root, deny_read=(root / "secrets",)), root / "secrets")


@pytest.mark.skipif(sys.platform == "win32", reason="needs symlinks")
def test_symlinked_protected_path_is_refused(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / ".git").symlink_to(tmp_path)
    with pytest.raises(SandboxSetupError):
        build_seatbelt_policy(workspace_policy(work), work)


# --- Windows -------------------------------------------------------------------------


def test_windows_allow_and_deny_paths(root, tmp_path):
    extra = tmp_path / "extra"
    extra.mkdir()
    (root / "secrets").mkdir()
    policy = workspace_policy(root, extra_writable=(extra, tmp_path / "missing"), deny_read=(root / "secrets",))
    paths = compute_allow_deny_paths(policy, root)
    assert set(paths.allow) == {policy.root, policy.extra_writable[0]}
    # Only existing paths get an ACL change: .meltzer does not exist.
    assert set(paths.deny_write) == {policy.root / ".git", policy.deny_read[0]}
    assert paths.deny_read == (policy.deny_read[0],)


def test_windows_read_only_and_full_access(root):
    paths = compute_allow_deny_paths(SandboxPolicy.for_session(SandboxMode.READ_ONLY, root), root)
    assert paths.allow == () and paths.deny_write == () and WindowsSandboxMode("unelevated") == WindowsSandboxMode.UNELEVATED
    with pytest.raises(SandboxSetupError):
        compute_allow_deny_paths(SandboxPolicy.for_session(SandboxMode.FULL_ACCESS, root), root)


# --- backend selection ---------------------------------------------------------------


def test_linux_selects_bubblewrap(monkeypatch):
    assert select_backend("linux") == SandboxBackend.BWRAP
    monkeypatch.setattr(sys, "platform", "linux")
    assert select_backend() == SandboxBackend.BWRAP


def test_platform_without_backend_refuses(monkeypatch):
    for platform in ("win32", "freebsd14", "cygwin"):
        with pytest.raises(SandboxSetupError):
            select_backend(platform)
    monkeypatch.setattr("meltzer.sandbox.os.path.exists", lambda path: False)
    with pytest.raises(SandboxSetupError):
        select_backend("darwin")
    monkeypatch.setattr("meltzer.sandbox.os.path.exists", lambda path: path == SEATBELT_EXECUTABLE)
    assert select_backend("darwin") == SandboxBackend.SEATBELT
