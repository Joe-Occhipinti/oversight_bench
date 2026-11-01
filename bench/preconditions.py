"""Files written into the agent container before meltzer starts (spec section 6).

`write_preconditions` runs inside a sample (it uses `inspect_ai.util.sandbox()`), after the
BashArena setup (5.4 steps 1-2) and before the model proxy is opened. As root it writes
meltzer's user configuration, the user rules and the three workspace scripts from `preconditions/`,
then records the trust entry of the configured hook with meltzer's own code. Nothing else is left in
the container: the trust entry is computed by `python -c` (no script file), and files are written
through `write_file` (standard input of a shell command, no staging file). R1/R2 of spec section 1a
apply to all of it.
"""

from __future__ import annotations

import re
from string import Template

from inspect_ai.util import sandbox

from bench.config import REPO_ROOT

PRECONDITIONS_DIR = REPO_ROOT / "preconditions"
CONFIG_TEMPLATE = PRECONDITIONS_DIR / "config.toml.tmpl"
RULES_SOURCE = PRECONDITIONS_DIR / "user.rules"

MELTZER_HOME = "/root/.meltzer"
CONFIG_PATH = f"{MELTZER_HOME}/config.toml"
RULES_PATH = f"{MELTZER_HOME}/rules/user.rules"
TRUSTED_HOOKS_PATH = f"{MELTZER_HOME}/trusted_hooks.toml"
SESSION_ROOT = "/app"
MELTZER_PYTHON = "/opt/meltzer-venv/bin/python"

# host file in `preconditions/` -> path in the container
SCRIPT_PATHS = {
    "sync_code_graph.py": "/app/tools/sync_code_graph.py",
    "turn_complete_notify.py": "/app/tools/turn_complete_notify.py",
    "check.py": "/app/scripts/check.py",
}
# Every file `write_preconditions` creates.
WRITTEN_PATHS = (CONFIG_PATH, RULES_PATH, *SCRIPT_PATHS.values(), TRUSTED_HOOKS_PATH)

# OpenRouter-style model ids: letters, digits and . _ - : / @ +
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*")

# Run with `python -c`. Loads the configuration of a session rooted at /app and trusts each of its
# hooks with meltzer's trust store, the same call an interactive "trust this hook" answer makes.
# `-B` keeps it from writing bytecode files.
TRUST_CODE = f"""\
from pathlib import Path
from meltzer.config.loader import load_config
from meltzer.hooks import TrustStore

root = Path({SESSION_ROOT!r})
config = load_config(root, home=Path("/root"))
hooks = [definition.resolve(root) for definition in config.hook_definitions()]
if not hooks:
    raise SystemExit("no hooks are configured")
path = Path({TRUSTED_HOOKS_PATH!r})
store = TrustStore(path)
for hook in hooks:
    store.trust(hook)
if not all(TrustStore(path).is_trusted(hook) for hook in hooks):
    raise SystemExit("trust entries were not recorded")
"""


class PreconditionsError(RuntimeError):
    """The preconditions could not be written. The sample is an infrastructure error."""


def render_config(agent_model_id: str, monitor_model_id: str, max_turn_steps: int) -> str:
    """The text of `/root/.meltzer/config.toml`."""
    for name, value in (("agent_model_id", agent_model_id), ("monitor_model_id", monitor_model_id)):
        if not isinstance(value, str) or not MODEL_ID.fullmatch(value):
            raise ValueError(f"{name} {value!r} is not a plain model id")
    if isinstance(max_turn_steps, bool) or not isinstance(max_turn_steps, int) or max_turn_steps < 1:
        raise ValueError(f"max_turn_steps must be a positive integer, got {max_turn_steps!r}")
    template = Template(CONFIG_TEMPLATE.read_text(encoding="utf-8"))
    return template.substitute(
        agent_model=agent_model_id, monitor_model=monitor_model_id, max_turn_steps=max_turn_steps
    )


def _host_files(agent_model_id: str, monitor_model_id: str, max_turn_steps: int) -> dict[str, str]:
    """Container path -> text, for the files that come from `preconditions/` (not the trust file)."""
    files = {
        CONFIG_PATH: render_config(agent_model_id, monitor_model_id, max_turn_steps),
        RULES_PATH: RULES_SOURCE.read_text(encoding="utf-8"),
    }
    for name, target in SCRIPT_PATHS.items():
        files[target] = (PRECONDITIONS_DIR / name).read_text(encoding="utf-8")
    return files


async def write_preconditions(agent_model_id: str, monitor_model_id: str, max_turn_steps: int) -> None:
    """Write the files of spec section 6 into the agent container, as root.

    Raises `PreconditionsError` if `pytest` is not on the container's `PATH`, if one of the files
    already exists, or if the trust entry cannot be computed. Call it after the BashArena setup.
    """
    files = _host_files(agent_model_id, monitor_model_id, max_turn_steps)
    sb = sandbox()

    pytest = await sb.exec(["sh", "-c", "command -v pytest"], user="root")
    if not pytest.success:
        raise PreconditionsError("pytest is not on PATH in the container")

    existing = await sb.exec(
        ["sh", "-c", 'for f in "$@"; do if [ -e "$f" ]; then echo "$f"; fi; done', "sh", *WRITTEN_PATHS],
        user="root",
    )
    if existing.stdout.strip():
        raise PreconditionsError(f"files already exist: {existing.stdout.split()}")

    for path, text in files.items():
        await sb.write_file(path, text, user="root")

    trust = await sb.exec([MELTZER_PYTHON, "-B", "-c", TRUST_CODE], user="root")
    if not trust.success:
        tail = (trust.stderr or trust.stdout).strip()[-1000:]
        raise PreconditionsError(f"could not record hook trust (exit code {trust.returncode}): {tail}")
