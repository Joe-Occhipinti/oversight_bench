"""Renders the user message and the developer instructions (spec section 9).

Templates are filled with a single regex pass over the template text, not str.format,
so literal braces in a template stay as they are and text inside a filled value
(for example a task description) is never expanded again.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

CONDITIONS = ("unaware", "aware")


def _read(path: Path) -> str:
    """Read a UTF-8 text file and drop trailing newlines."""
    return Path(path).read_text(encoding="utf-8").rstrip("\n")


def _fill(template: str, values: Mapping[str, str]) -> str:
    """Replace every `{name}` slot for the names in `values`, in one pass."""
    pattern = re.compile("|".join(re.escape("{" + name + "}") for name in values))
    return pattern.sub(lambda m: values[m.group(0)[1:-1]], template)


def render_user_message(task_description: str) -> str:
    """User message seen by the monitor. Contains nothing about the side task."""
    return "Your task is as follows:\n" + task_description


def card_files(cards_dir: Path) -> list[tuple[str, Path]]:
    """(id, path) of the card files `E01-<slug>.md`, `E02-...`, in id order."""
    files = sorted(Path(cards_dir).glob("E[0-9][0-9]-*.md"), key=lambda f: f.name[:3])
    if not files:
        raise ValueError(f"no card files in {cards_dir}")
    return [(f.name[:3], f) for f in files]


def card_ids(cards_dir: Path) -> list[str]:
    return [card_id for card_id, _ in card_files(cards_dir)]


def card_body(text: str) -> str:
    """The card text without its leading YAML block (`---` ... `---`), with its `##` headings as `###`."""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            text = text[end + len("\n---\n"):]
    return re.sub(r"(?m)^## ", "### ", text).strip()


def render_cards(cards_dir: Path, card_id: str | None = None) -> str:
    """The cards in id order (only `card_id` if given), each as `## Card <id>` and its body (`card_body`)."""
    files = card_files(cards_dir)
    if card_id is not None:
        files = [(cid, path) for cid, path in files if cid == card_id]
        if not files:
            raise ValueError(f"no card {card_id} in {cards_dir}")
    return "\n\n".join(f"## Card {cid}\n\n{card_body(_read(path))}" for cid, path in files)


def render_developer_instructions(
    condition: str,
    *,
    setting_description: str,
    side_task_description: str,
    attack_base_path: Path,
    aware_common_path: Path | None,
    aware_section_path: Path | None,
    cards_dir: Path,
    card_id: str | None = None,
) -> str:
    """Hidden system-prompt text. unaware: attack_base. aware: attack_base + aware_common + aware_section (the
    mode's workflow). If aware_section has a `{cards}` slot, it gets every card or, if `card_id` is given, only
    that card."""
    if condition not in CONDITIONS:
        raise ValueError(f"unknown condition {condition!r}, expected one of {CONDITIONS}")
    text = _fill(
        _read(attack_base_path),
        {
            "setting_description": setting_description,
            "side_task_description": side_task_description,
        },
    )
    if condition == "aware":
        section = _read(aware_section_path)
        if "{cards}" in section:
            section = _fill(section, {"cards": render_cards(cards_dir, card_id)})
        text += "\n\n" + _read(aware_common_path) + "\n\n" + section
    return text


@dataclass(frozen=True)
class ReviewSample:
    """Inputs needed to render both prompts of one sample."""

    task_description: str
    setting_description: str
    side_task_description: str
    attack_base_path: Path
    aware_common_path: Path | None  # None in a run without the aware condition
    aware_section_path: Path | None
    cards_dir: Path
    card_id: str | None = None


def write_review_prompts(out_dir: Path, samples: Mapping[str, ReviewSample]) -> list[Path]:
    """Write the rendered prompts of one sample per condition for the review gate (9.2).

    `samples` maps a condition ("unaware", "aware") to the sample to render in it; pass the
    same scenario for both to compare them. For each condition, two files are written to
    `out_dir` (created if missing, existing files overwritten), with LF line endings:
    `<condition>_user_message.md` and `<condition>_developer_instructions.md`.
    Returns the written paths.
    """
    unknown = [c for c in samples if c not in CONDITIONS]
    if unknown:
        raise ValueError(f"unknown conditions {unknown}, expected some of {CONDITIONS}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for condition, s in samples.items():
        files = {
            f"{condition}_user_message.md": render_user_message(s.task_description),
            f"{condition}_developer_instructions.md": render_developer_instructions(
                condition,
                setting_description=s.setting_description,
                side_task_description=s.side_task_description,
                attack_base_path=s.attack_base_path,
                aware_common_path=s.aware_common_path,
                aware_section_path=s.aware_section_path,
                cards_dir=s.cards_dir,
                card_id=s.card_id,
            ),
        }
        for name, text in files.items():
            path = out / name
            path.write_text(text, encoding="utf-8", newline="\n")
            written.append(path)
    return written
