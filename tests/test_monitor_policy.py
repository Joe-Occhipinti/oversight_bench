"""bench.monitor_policy: the bench's copy of the monitor's system prompt is meltzer's own."""

from __future__ import annotations

import sys

from bench import monitor_policy as M


def test_monitor_policy_is_the_monitor_system_prompt():
    """The copy is meltzer's own system prompt for the bench's user config, without the output contract."""
    src = str(M.MONITOR_DIR.parent.parent)
    if src not in sys.path:
        sys.path.insert(0, src)
    from meltzer.monitor.prompt import OUTPUT_CONTRACT, policy_prompt

    text = M.load_monitor_policy()
    expected = policy_prompt(M.MONITOR_ENVIRONMENT, M.MONITOR_TRUSTED_HOSTS)
    assert expected == f"{text}\n\n{OUTPUT_CONTRACT}\n"
    assert M.SECURITY_POLICY_PLACEHOLDER not in text
    assert f"- {M.MONITOR_ENVIRONMENT}\n" in text and "`192.0.2.10` is a trusted internal service" in text
    assert text.startswith("You are judging one planned coding-agent action.")


def test_environment_lines():
    assert M.environment_lines(None, ()) == ""
    assert M.environment_lines(" a server ", ("h1", "h2")) == (
        "- a server\n- The hosts `h1`, `h2` are trusted internal services of the user's organization.\n"
    )
