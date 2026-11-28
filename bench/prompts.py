"""Renders the user message and the developer instructions of each role (docs/spec-exp3.md sections 2, 5, 6, 7).

Prompt folder (`prompts.dir` of the run config):

    base.md              {setting_description} {side_tasks} {own_side_task}
    experiment.md        {board} {step_cap}
    reflection.md        {board}
    execution.md         {strategy}
    tiers/tier<k>.md     what the agent can access in tier k
    ceilings/tier<k>.md  the designer text used in place of the strategy in ceiling k

Developer instructions of a role: base.md, then tiers/tier<k>.md, then the role file, separated by blank lines.
The reflection's base has no own pair: every line of base.md that holds `{own_side_task}` is left out.

Templates are filled with a single regex pass over the template text, not str.format, so literal braces in a
template stay as they are and text inside a filled value (a task description, lessons) is never expanded again.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

ROLES = ("experiment", "execution", "reflection")
# The placeholders each role file is filled with.
ROLE_VALUES = {"experiment": ("board", "step_cap"), "execution": ("strategy",), "reflection": ("board",)}
OWN_PLACEHOLDER = "{own_side_task}"
OWN_MARK = " (yours)"  # after the agent's own side task in {side_tasks}


def _read(path: Path) -> str:
    """Read a UTF-8 text file and drop trailing newlines."""
    return Path(path).read_text(encoding="utf-8").rstrip("\n")


def _fill(template: str, values: Mapping[str, str]) -> str:
    """Replace every `{name}` slot for the names in `values`, in one pass."""
    if not values:
        return template
    pattern = re.compile("|".join(re.escape("{" + name + "}") for name in values))
    return pattern.sub(lambda m: values[m.group(0)[1:-1]], template)


def render_user_message(task_description: str) -> str:
    """User message seen by the monitor. Contains nothing about the side task."""
    return "Your task is as follows:\n" + task_description


def render_side_tasks(descriptions: Mapping[str, str], own: str | None = None) -> str:
    """Each side task as `### <name>` and its description; the agent's own one marked with OWN_MARK."""
    return "\n\n".join(
        f"### {name}{OWN_MARK if name == own else ''}\n\n{text.strip()}" for name, text in descriptions.items()
    )


def render_base(
    prompts_dir: Path, *, setting_description: str, side_tasks: Mapping[str, str], own_side_task: str | None
) -> str:
    """base.md with the setting, all side tasks and the agent's own side task (its name). With no own side task
    (reflection), the lines that name it are left out."""
    if own_side_task is not None and own_side_task not in side_tasks:
        raise ValueError(f"own side task {own_side_task!r} is not among {list(side_tasks)}")
    text = _read(Path(prompts_dir) / "base.md")
    if own_side_task is None:
        text = "\n".join(line for line in text.split("\n") if OWN_PLACEHOLDER not in line)
    return _fill(
        text,
        {
            "setting_description": setting_description,
            "side_tasks": render_side_tasks(side_tasks, own_side_task),
            "own_side_task": own_side_task or "",
        },
    )


def tier_text(prompts_dir: Path, tier: int) -> str:
    return _read(Path(prompts_dir) / "tiers" / f"tier{tier}.md")


def ceiling_text(prompts_dir: Path, tier: int) -> str:
    return _read(Path(prompts_dir) / "ceilings" / f"tier{tier}.md")


def render_instructions(prompts_dir: Path, tier: int, role: str, base: str, **values: object) -> str:
    """The developer instructions (or, for the reflection, the request text) of `role` in `tier`: `base`, the tier
    text and the role file filled with `values`, which must be exactly the role's placeholders (ROLE_VALUES)."""
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}, expected one of {ROLES}")
    if set(values) != set(ROLE_VALUES[role]):
        raise ValueError(f"{role} takes the values {ROLE_VALUES[role]}, got {sorted(values)}")
    text = _fill(_read(Path(prompts_dir) / f"{role}.md"), {key: str(value) for key, value in values.items()})
    return "\n\n".join((base, tier_text(prompts_dir, tier), text))
