from __future__ import annotations

from pathlib import Path

import pytest

from meltzer.approval.policy import ApprovalPolicy, ApprovalSettings, ReviewerKind
from meltzer.context import environment_context, environment_update, user_shell_command
from meltzer.history import History, Item, Kind, Role
from meltzer.sandbox.policy import NetworkMode, SandboxMode, SandboxPolicy

ON_REQUEST = ApprovalSettings(policy=ApprovalPolicy.ON_REQUEST, reviewer=ReviewerKind.MONITOR)


def workspace_policy(work: Path, tmp_path: Path, **kwargs) -> SandboxPolicy:
    temp_dir = tmp_path / "meltzer-tmp"
    temp_dir.mkdir(exist_ok=True)
    return SandboxPolicy.for_session(SandboxMode.WORKSPACE_WRITE, work, temp_dir=temp_dir, **kwargs)


# --- environment item ----------------------------------------------------------------


def test_environment_item_describes_a_workspace_write_session(work, tmp_path):
    policy = workspace_policy(work, tmp_path)
    item = environment_context(policy, ON_REQUEST)

    assert (item.role, item.kind) == (Role.USER, Kind.ENVIRONMENT)
    assert item.content == (
        "<environment_context>\n"
        f"  <cwd>{policy.root}</cwd>\n"
        "  <approval_policy>on-request</approval_policy>\n"
        "  <approvals_reviewer>monitor</approvals_reviewer>\n"
        "  <sandbox_mode>workspace-write</sandbox_mode>\n"
        "  <network_access>none</network_access>\n"
        "  <writable_roots>\n"
        f"    <root>{policy.root}</root>\n"
        "    <root>/tmp</root>\n"
        "  </writable_roots>\n"
        "</environment_context>"
    )


def test_environment_item_lists_extra_writable_directories(work, tmp_path):
    extra = tmp_path / "cache"
    extra.mkdir()
    policy = workspace_policy(work, tmp_path, extra_writable=(extra,))
    content = environment_context(policy, ON_REQUEST).content
    assert f"    <root>{policy.extra_writable[0]}</root>\n" in content
    read_only = SandboxPolicy.for_session(SandboxMode.READ_ONLY, work)
    content = environment_context(read_only, ON_REQUEST).content
    assert "<sandbox_mode>read-only</sandbox_mode>" in content and "writable_roots" not in content
    granted = environment_context(read_only.with_grants(write=(extra,)), ON_REQUEST).content
    assert f"<root>{extra}</root>" in granted


def test_environment_item_of_a_full_access_session(work):
    policy = SandboxPolicy.for_session(SandboxMode.FULL_ACCESS, work)
    settings = ApprovalSettings(policy=ApprovalPolicy.NEVER, reviewer=ReviewerKind.USER)
    content = environment_context(policy, settings).content
    assert "<sandbox_mode>full-access</sandbox_mode>" in content
    assert "<network_access>full</network_access>" in content
    assert "<approval_policy>never</approval_policy>" in content
    assert "<approvals_reviewer>user</approvals_reviewer>" in content
    assert "writable_roots" not in content


@pytest.mark.parametrize("network", [NetworkMode.NONE, NetworkMode.PROXY, NetworkMode.FULL])
def test_environment_item_shows_the_network_mode(work, tmp_path, network):
    policy = workspace_policy(work, tmp_path, network=network)
    assert f"<network_access>{network.value}</network_access>" in environment_context(policy, ON_REQUEST).content


def test_environment_item_escapes_paths(tmp_path):
    work = tmp_path / "R&D"
    work.mkdir()
    policy = SandboxPolicy.for_session(SandboxMode.READ_ONLY, work)
    content = environment_context(policy, ON_REQUEST).content
    assert "R&amp;D</cwd>" in content
    assert "R&D" not in content


# --- environment updates -------------------------------------------------------------


def test_first_update_adds_an_environment_item(work, tmp_path):
    item = environment_update(History(), workspace_policy(work, tmp_path), ON_REQUEST)
    assert item is not None and item.kind == Kind.ENVIRONMENT


def test_no_update_while_the_policy_is_unchanged(work, tmp_path):
    policy = workspace_policy(work, tmp_path)
    history = History()
    history.append(environment_context(policy, ON_REQUEST))
    history.append(Item.user("run the tests"))
    assert environment_update(history, policy, ON_REQUEST) is None


def test_update_after_a_grant_and_after_the_grant_ends(work, tmp_path):
    extra = tmp_path / "out"
    extra.mkdir()
    base = workspace_policy(work, tmp_path)
    granted = base.with_grants(write=(extra,))
    history = History()
    history.append(environment_context(base, ON_REQUEST))

    widened = environment_update(history, granted, ON_REQUEST)
    assert widened is not None
    assert f"<root>{granted.extra_writable[0]}</root>" in widened.content
    history.append(widened)

    restored = environment_update(history, base, ON_REQUEST)
    assert restored is not None
    assert restored.content == environment_context(base, ON_REQUEST).content


def test_update_after_the_environment_item_was_removed(work, tmp_path):
    policy = workspace_policy(work, tmp_path)
    history = History()
    history.append(environment_context(policy, ON_REQUEST))
    history.replace([Item.user("summary")])
    assert environment_update(history, policy, ON_REQUEST) is not None


# --- user shell commands -------------------------------------------------------------


def test_user_shell_command_item():
    item = user_shell_command("ls -a", 0, ".\n..\nsrc\n")
    assert (item.role, item.kind) == (Role.USER, Kind.USER_SHELL_COMMAND)
    assert item.content == (
        "<user_shell_command>\n"
        "<command>\nls -a\n</command>\n"
        "<result>\nExit code: 0\nOutput:\n.\n..\nsrc\n</result>\n"
        "</user_shell_command>"
    )


@pytest.mark.parametrize("output, shown", [("", ""), ("done", "done\n")])
def test_user_shell_command_output_ends_with_a_newline(output, shown):
    assert f"Output:\n{shown}</result>" in user_shell_command("false", 1, output).content


def test_user_shell_command_text_is_escaped():
    item = user_shell_command("echo '</command>'", 0, "</result>\n</user_shell_command>\n<command>")
    for tag in ("</command>", "</result>", "</user_shell_command>"):
        assert item.content.count(tag) == 1
    assert "&lt;/result&gt;" in item.content
