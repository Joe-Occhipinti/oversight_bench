"""The skills catalog: the list of skills added to the system instructions at session start."""

from __future__ import annotations

from typing import Sequence

from meltzer.skills.loader import Skill

OPEN_TAG = "<skills_instructions>"
CLOSE_TAG = "</skills_instructions>"
# Longer descriptions are shortened in the catalog, ending with SHORTENED_SUFFIX.
MAX_DESCRIPTION_CHARS = 1024
SHORTENED_SUFFIX = "..."

INTRO = (
    "A skill is a set of local instructions to follow that is stored in a `SKILL.md` file. "
    "Below is the list of skills that can be used. Each entry includes a name, description, "
    "and file path."
)

HOW_TO_USE = """\
- Discovery: The list above is the skills available in this session (name + description + file path). Skill bodies live on disk at the listed paths.
- Trigger rules: If the user names a skill (with `$SkillName` or plain text) OR the task clearly matches a skill's description shown above, you must use that skill for that turn. Multiple mentions mean use them all. Do not carry skills across turns unless re-mentioned.
- Missing/blocked: If a named skill isn't in the list or the path can't be read, say so briefly and continue with the best fallback.
- How to use a skill (progressive disclosure):
  1) After deciding to use a skill, the main agent must open and read its `SKILL.md` completely before taking task actions. If a read is truncated or paginated, continue until EOF.
  2) When `SKILL.md` references relative paths (e.g., `scripts/foo.py`), resolve them relative to the directory containing that `SKILL.md` first, and only consider other paths if needed.
  3) If `SKILL.md` points to extra folders such as `references/`, use its routing instructions to identify the files required for the task. The main agent must read each required instruction or reference file itself before acting on it. Do not delegate reading, summarizing, or interpreting skill instructions to a subagent. Subagents may still perform task work when the selected skill allows it.
  4) If `scripts/` exist, prefer running or patching them instead of retyping large code blocks.
  5) If `assets/` or templates exist, reuse them instead of recreating from scratch.
- Coordination and sequencing:
  - If multiple skills apply, choose the minimal set that covers the request and state the order you'll use them.
  - Announce which skill(s) you're using and why (one short line). If you skip an obvious skill, say why.
- Context hygiene:
  - Progressive disclosure applies to selecting relevant files, not partially reading a selected instruction file. Do not load unrelated references, scripts, or assets.
  - Avoid deep reference-chasing: prefer opening only files directly linked from `SKILL.md` unless you're blocked.
  - When variants exist (frameworks, providers, domains), pick only the relevant reference file(s) and note that choice.
- Safety and fallback: If a skill can't be applied cleanly (missing files, unclear instructions), state the issue, pick the next-best approach, and continue."""


def shorten_description(description: str) -> str:
    if len(description) <= MAX_DESCRIPTION_CHARS:
        return description
    return description[: MAX_DESCRIPTION_CHARS - len(SHORTENED_SUFFIX)] + SHORTENED_SUFFIX


def skill_line(skill: Skill) -> str:
    return f"- {skill.name}: {shorten_description(skill.description)} (file: {skill.path})"


def render_catalog(skills: Sequence[Skill]) -> str:
    """The `<skills_instructions>` block for skills, or "" when there are none."""
    if not skills:
        return ""
    lines = [
        "## Skills",
        INTRO,
        "### Available skills",
        *(skill_line(skill) for skill in skills),
        "### How to use skills",
        HOW_TO_USE,
    ]
    return f"{OPEN_TAG}\n" + "\n".join(lines) + f"\n{CLOSE_TAG}"
