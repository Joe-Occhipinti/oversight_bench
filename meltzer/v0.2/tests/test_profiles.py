from __future__ import annotations

from pathlib import Path

import pytest

from meltzer.config import ConfigError, load_config
from meltzer.sandbox import bwrap
from meltzer.sandbox.bwrap import SandboxSetupError, build_bwrap_args
from meltzer.sandbox.policy import NetworkMode, SandboxMode, SandboxPolicy
from meltzer.sandbox.profiles import (
    Access,
    compile_profile,
    load_profiles,
    policy_from_config,
    resolve_profile,
    selected_profile,
)

from conftest import run_sandboxed as run

@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    (path / ".meltzer").mkdir(parents=True)
    return path


def write_user_config(home: Path, text: str) -> None:
    (home / ".meltzer" / "config.toml").write_text(text, encoding="utf-8")


def compile_named(permissions: dict, name: str, root: Path, home: Path, **kwargs) -> SandboxPolicy:
    profile = resolve_profile(load_profiles(permissions), name)
    return compile_profile(profile, root, meltzer_home=home / ".meltzer", home=home, **kwargs)


def fake_bwrap(monkeypatch) -> None:
    monkeypatch.setattr(bwrap.shutil, "which", lambda name: "/usr/bin/bwrap")


# --- built-in profiles and selection ---------------------------------------------------


@pytest.mark.parametrize(
    "name, mode",
    [(":read-only", SandboxMode.READ_ONLY), (":workspace", SandboxMode.WORKSPACE_WRITE), (":full-access", SandboxMode.FULL_ACCESS)],
)
def test_builtin_profiles_match_the_sandbox_modes(root, home, tmp_path, name, mode):
    temp = tmp_path / "session-tmp"
    temp.mkdir()
    policy = compile_named({}, name, root, home, temp_dir=temp)
    assert policy == SandboxPolicy.for_session(mode, root, meltzer_home=home / ".meltzer", temp_dir=temp)


def test_without_default_permissions_the_sandbox_mode_applies(root, home):
    write_user_config(home, 'sandbox_mode = "read-only"\n[permissions.dev]\nextends = ":workspace"\n')
    config = load_config(root, home=home)
    policy = policy_from_config(config, root, meltzer_home=home / ".meltzer", home=home)
    assert policy == SandboxPolicy.for_session(SandboxMode.READ_ONLY, root, meltzer_home=home / ".meltzer")


def test_network_enabled_gives_workspace_write_the_network_proxy(root, home):
    write_user_config(home, 'sandbox_mode = "workspace-write"\n[network]\nenabled = true\n')
    config = load_config(root, home=home)
    assert policy_from_config(config, root, meltzer_home=home / ".meltzer", home=home).network == NetworkMode.PROXY
    write_user_config(home, 'sandbox_mode = "read-only"\n[network]\nenabled = true\n')
    config = load_config(root, home=home)
    assert policy_from_config(config, root, meltzer_home=home / ".meltzer", home=home).network == NetworkMode.NONE


def test_default_permissions_selects_a_profile(root, home):
    write_user_config(
        home,
        'default_permissions = "dev"\n'
        "[permissions.dev]\n"
        'extends = ":workspace"\n'
        "[permissions.dev.network]\n"
        "enabled = true\n",
    )
    config = load_config(root, home=home)
    policy = policy_from_config(config, root, meltzer_home=home / ".meltzer", home=home)
    assert policy.mode == SandboxMode.WORKSPACE_WRITE
    assert policy.network == NetworkMode.PROXY
    assert selected_profile(config).name == "dev"


@pytest.mark.parametrize(
    "overrides, mode",
    [
        ([("default_permissions", ":read-only")], SandboxMode.READ_ONLY),
        ([("default_permissions", ":workspace"), ("sandbox_mode", "read-only")], SandboxMode.READ_ONLY),
    ],
)
def test_default_permissions_can_name_a_builtin_profile_and_the_sandbox_flag_wins(root, home, overrides, mode):
    assert policy_from_config(load_config(root, home=home, overrides=overrides), root).mode == mode


def test_unknown_default_profile(root, home):
    config = load_config(root, home=home, overrides=[("default_permissions", "missing")])
    with pytest.raises(ConfigError, match="missing"):
        policy_from_config(config, root)


def test_temp_dir_is_created_only_for_sandboxed_profiles(root, home, tmp_path):
    made: list[Path] = []

    def make() -> Path:
        path = tmp_path / f"t{len(made)}"
        path.mkdir()
        made.append(path)
        return path

    full = load_config(root, home=home, overrides=[("default_permissions", ":full-access")])
    assert policy_from_config(full, root, temp_dir=make).temp_dir is None
    assert made == []
    workspace = load_config(root, home=home, overrides=[("default_permissions", ":workspace")])
    assert policy_from_config(workspace, root, temp_dir=make).temp_dir == made[0].resolve()


def test_temp_dir_is_removed_when_the_profile_is_invalid(root, home, tmp_path):
    write_user_config(home, 'default_permissions = "dev"\n[permissions.dev.filesystem]\n"/" = "deny"\n')
    config = load_config(root, home=home)
    temp = tmp_path / "session-tmp"

    def make() -> Path:
        temp.mkdir()
        return temp

    with pytest.raises(ConfigError):
        policy_from_config(config, root, temp_dir=make)
    assert not temp.exists()


# --- extends -------------------------------------------------------------------------


def test_extends_applies_the_parent_first(root, home):
    permissions = {
        "base": {"extends": ":workspace", "filesystem": {"docs": "read", "vendor": "read"}, "network": {"enabled": True}},
        "child": {"extends": "base", "filesystem": {"vendor": "write"}, "description": "child profile"},
    }
    profile = resolve_profile(load_profiles(permissions), "child")
    assert profile.mode == SandboxMode.WORKSPACE_WRITE
    assert profile.filesystem == {"docs": Access.READ, "vendor": Access.WRITE}
    assert profile.network is True
    assert profile.description == "child profile"


def test_profile_without_extends_starts_from_read_only(root, home):
    policy = compile_named({"plain": {}}, "plain", root, home)
    assert policy.mode == SandboxMode.READ_ONLY
    assert not policy.can_write(root / "a.txt")


@pytest.mark.parametrize(
    "permissions, message",
    [
        ({"a": {"extends": "b"}, "b": {"extends": "a"}}, "cycle"),
        ({"a": {"extends": "nothing"}}, "nothing"),
        ({":workspace": {"extends": ":full-access"}}, "reserved"),
    ],
)
def test_extends_cycles_unknown_parents_and_reserved_names_are_errors(permissions, message):
    with pytest.raises(ConfigError, match=message):
        resolve_profile(load_profiles(permissions), "a")


@pytest.mark.parametrize(
    "entry, message",
    [
        ({"sandbox": "full-access"}, "unknown key"),
        ({"extends": 3}, "extends"),
        ({"filesystem": {"docs": "execute"}}, "read, write, deny"),
        ({"filesystem": {"docs": {"x": "read"}}}, "read, write, deny"),
        ({"filesystem": "docs"}, "table"),
        ({"workspace_roots": {"../lib": "yes"}}, "true or false"),
        ({"workspace_roots": ["../lib"]}, "table"),
        ({"network": True}, "table"),
        ({"network": {"enabled": "yes"}}, "true or false"),
        ({"network": {"mode": "full"}}, "unknown key"),
        ({"description": 1}, "description"),
    ],
)
def test_invalid_profile_entries(entry, message):
    with pytest.raises(ConfigError, match=message):
        load_profiles({"bad": entry})


def test_special_paths_are_rejected(root, home):
    with pytest.raises(ConfigError, match=":root"):
        compile_named({"p": {"filesystem": {":root": "read"}}}, "p", root, home)


# --- filesystem entries ----------------------------------------------------------------


def test_workspace_roots_are_writable_with_their_protected_names(root, home, tmp_path):
    lib = tmp_path / "lib"
    (lib / ".git").mkdir(parents=True)
    (lib / ".meltzer").mkdir()
    permissions = {
        "dev": {"extends": ":workspace", "workspace_roots": {str(lib): True, "../unused": False}},
        "ro": {"workspace_roots": {str(lib): True}},
    }
    policy = compile_named(permissions, "dev", root, home)
    assert policy.can_write(lib / "src" / "a.py")
    assert not policy.can_write(lib / ".git" / "config") and not policy.can_write(lib / ".meltzer" / "config.toml")
    assert policy.extra_writable == (lib.resolve(),)
    assert not compile_named(permissions, "ro", root, home).can_write(lib / "a.py")


def test_relative_and_home_paths(root, home):
    permissions = {"dev": {"filesystem": {"out": "write", "~/cache": "write"}}}
    policy = compile_named(permissions, "dev", root, home)
    assert set(policy.extra_writable) == {(root / "out").resolve(), (home / "cache").resolve()}


def test_write_and_read_entries(root, home, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    policy = compile_named({"ro": {"extends": ":read-only", "filesystem": {str(out): "write"}}}, "ro", root, home)
    assert policy.can_write(out / "report.txt") and not policy.can_write(root / "a.txt")
    assert policy.writable_roots() == (out.resolve(),)
    policy = compile_named({"dev": {"extends": ":workspace", "filesystem": {"docs": "read"}}}, "dev", root, home)
    assert not policy.can_write(root / "docs" / "index.md") and policy.can_write(root / "src" / "main.py")


@pytest.mark.parametrize("base", [":workspace", ":read-only"])
@pytest.mark.parametrize("entry", [".git", ".git/hooks", ".meltzer", ".meltzer/rules"])
def test_write_entries_never_remove_a_protected_path(root, home, base, entry):
    (root / ".git" / "hooks").mkdir()
    policy = compile_named({"p": {"extends": base, "filesystem": {entry: "write"}}}, "p", root, home)
    assert not policy.can_write(root / entry / "file")
    assert any(p in policy.read_only_subpaths() for p in (root.resolve() / ".git", root.resolve() / ".meltzer"))


@pytest.mark.parametrize("entry", ["~/.meltzer", "~/.meltzer/rules"])
def test_write_entries_never_cover_the_meltzer_home(root, home, entry):
    (home / ".meltzer" / "rules").mkdir()
    policy = compile_named({"p": {"extends": ":workspace", "filesystem": {entry: "write"}}}, "p", root, home)
    assert not policy.can_write(home / ".meltzer" / "rules" / "default.rules")
    assert not policy.can_write(home / ".meltzer" / "config.toml")
    assert (home / ".meltzer").resolve() in policy.read_only_subpaths()


def test_deny_entries(root, home, tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (root / ".env").write_text("TOKEN=1\n")
    permissions = {"dev": {"extends": ":workspace", "filesystem": {str(secrets): "deny", ".env": "deny"}}}
    policy = compile_named(permissions, "dev", root, home)
    assert not policy.can_read(secrets / "key.pem")
    assert not policy.can_read(root / ".env")
    assert policy.can_read(root / "README.md")
    assert not policy.can_write(root / ".env")
    assert set(policy.deny_read) == {secrets.resolve(), (root / ".env").resolve()}


@pytest.mark.parametrize(
    "filesystem, message",
    [
        ({".": "deny"}, "workspace root"),
        ({"..": "deny"}, "workspace root"),
        ({".": "read"}, "workspace root"),
        ({"/": "deny"}, "workspace root"),
        ({"docs": "read", "docs/gen": "write"}, "'docs' = 'read' contains the writable path"),
        ({"private": "deny", "private/public": "read"}, "'private' = 'deny' contains the read-only path"),
    ],
)
def test_entries_that_contain_entries_with_more_access_are_rejected(root, home, filesystem, message):
    with pytest.raises(ConfigError, match=message):
        compile_named({"dev": {"extends": ":workspace", "filesystem": filesystem}}, "dev", root, home)


def test_full_access_profiles_take_no_entries(root, home):
    for entry in ({"filesystem": {"x": "read"}}, {"workspace_roots": {"x": True}}, {"network": {"enabled": False}}):
        with pytest.raises(ConfigError, match=":full-access"):
            compile_named({"p": {"extends": ":full-access", **entry}}, "p", root, home)


def test_network_off(root, home):
    policy = compile_named({"p": {"extends": ":workspace", "network": {"enabled": False}}}, "p", root, home)
    assert policy.network == NetworkMode.NONE


# --- bubblewrap arguments --------------------------------------------------------------


def test_bwrap_masks_denied_paths_and_skips_missing_ones(root, home, tmp_path, monkeypatch):
    fake_bwrap(monkeypatch)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (root / ".env").write_text("TOKEN=1\n")
    filesystem = {str(secrets): "deny", ".env": "deny", "gone": "deny", "build": "write"}
    policy = compile_named({"dev": {"extends": ":workspace", "filesystem": filesystem}}, "dev", root, home)
    args = build_bwrap_args(policy, ["true"], policy.root, status_fd=7)
    text = " ".join(args)
    assert f"--tmpfs {secrets.resolve()} --remount-ro {secrets.resolve()}" in text
    assert f"--ro-bind /dev/null {(root / '.env').resolve()}" in text
    assert str((root / "gone").resolve()) not in text and str((root / "build").resolve()) not in args
    assert text.index(f"--tmpfs {secrets.resolve()}") > text.index(f"--bind {policy.root} {policy.root}")


@pytest.mark.parametrize("access, base", [("write", ":workspace"), ("deny", None)])
def test_bwrap_refuses_a_path_that_became_a_symlink(root, home, tmp_path, monkeypatch, access, base):
    fake_bwrap(monkeypatch)
    target = tmp_path / "target"
    target.mkdir()
    entry = {"filesystem": {str(target): access}, **({"extends": base} if base else {})}
    policy = compile_named({"dev": entry}, "dev", root, home)
    target.rmdir()
    try:
        target.symlink_to(home, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create symlinks here")
    with pytest.raises(SandboxSetupError, match="symlink"):
        build_bwrap_args(policy, ["true"], policy.root, status_fd=7)


# --- commands under bwrap ----------------------------------------------------------------


def test_denied_directory_and_file_are_not_readable(root, home, tmp_path):
    secrets = root / "private"
    secrets.mkdir()
    (secrets / "key.pem").write_text("PRIVATE\n")
    (root / ".env").write_text("TOKEN=1\n")
    permissions = {"dev": {"extends": ":workspace", "filesystem": {str(secrets): "deny", ".env": "deny"}}}
    policy = compile_named(permissions, "dev", root, home)
    result = run(policy, f"cat {secrets / 'key.pem'}; cat .env; ls {secrets}; echo done", tmp_path)
    assert "PRIVATE" not in result.output and "TOKEN" not in result.output
    assert "done" in result.output
    assert run(policy, "echo x > .env", tmp_path).exit_code != 0
    assert run(policy, f"echo x > {secrets / 'new.txt'}", tmp_path).exit_code != 0
    assert (root / ".env").read_text() == "TOKEN=1\n"
    assert sorted(p.name for p in secrets.iterdir()) == ["key.pem"]


def test_write_entry_of_a_read_only_profile_is_writable(root, home, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    policy = compile_named({"ro": {"filesystem": {str(out): "write"}}}, "ro", root, home)
    assert run(policy, f"echo x > {out / 'a.txt'}", tmp_path).exit_code == 0
    assert (out / "a.txt").read_text() == "x\n"
    assert run(policy, "echo x > a.txt", tmp_path).exit_code != 0
