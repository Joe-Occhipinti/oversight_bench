"""The monitor's system prompt as meltzer builds it for the bench's user config (used by the calibration).

meltzer's prompt is policy_template.md with policy.md in place of the placeholder, and the user config's
environment and trusted-host lines added to policy.md's environment profile
(src/meltzer/monitor/prompt.py::policy_prompt in the meltzer tree).
"""

from __future__ import annotations

from pathlib import Path

from bench.config import MELTZER_DIR, MONITOR_ENVIRONMENT, MONITOR_TRUSTED_HOSTS

MONITOR_DIR = MELTZER_DIR / "src" / "meltzer" / "monitor"
SECURITY_POLICY_PLACEHOLDER = "{{ security_policy }}"
PROFILE_HEADING = "## Environment Profile\n"


def environment_lines(environment: str | None, trusted_hosts: tuple[str, ...]) -> str:
    """The profile lines meltzer adds for `monitor.environment` and `monitor.trusted_hosts`."""
    lines = []
    if environment:
        lines.append(f"- {environment.strip()}\n")
    if len(trusted_hosts) == 1:
        lines.append(f"- The host `{trusted_hosts[0]}` is a trusted internal service of the user's organization.\n")
    elif trusted_hosts:
        hosts = ", ".join(f"`{host}`" for host in trusted_hosts)
        lines.append(f"- The hosts {hosts} are trusted internal services of the user's organization.\n")
    return "".join(lines)


def load_monitor_policy(
    monitor_dir: Path = MONITOR_DIR,
    environment: str | None = MONITOR_ENVIRONMENT,
    trusted_hosts: tuple[str, ...] = MONITOR_TRUSTED_HOSTS,
) -> str:
    """The monitor's system prompt without its output format, as meltzer builds it for the bench's user config."""
    template = (monitor_dir / "policy_template.md").read_text(encoding="utf-8").rstrip()
    policy = (monitor_dir / "policy.md").read_text(encoding="utf-8").strip()
    head, _, rest = policy.partition(PROFILE_HEADING)
    first, _, others = rest.partition("\n")
    policy = f"{head}{PROFILE_HEADING}{first}\n{environment_lines(environment, trusted_hosts)}{others}"
    return template.replace(SECURITY_POLICY_PLACEHOLDER, policy)
