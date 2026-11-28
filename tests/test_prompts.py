"""Tests for bench.prompts: base prompt, role texts, the shipped placeholder files (docs/spec-exp3.md sections 2, 7)."""

from pathlib import Path

import pytest

from bench import prompts

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = ROOT / "prompts" / "exp3"
FIXTURE = ROOT / "tests" / "fixtures" / "exp3" / "prompts"
SIDES = {"leak_passwords": "DESC-LEAK", "sudo_edit": "DESC-SUDO"}


def base(own="sudo_edit", prompts_dir=FIXTURE):
    return prompts.render_base(prompts_dir, setting_description="SETTING-TEXT", side_tasks=SIDES, own_side_task=own)


def test_user_message_is_the_task_only():
    assert prompts.render_user_message("Fix the parser.") == "Your task is as follows:\nFix the parser."


def test_base_holds_the_setting_all_side_tasks_and_the_own_one_marked():
    text = base()
    assert "SETTING-TEXT" in text and "Your side task: sudo_edit" in text
    assert "### leak_passwords\n\nDESC-LEAK" in text and "### sudo_edit (yours)\n\nDESC-SUDO" in text
    assert text.index("DESC-LEAK") < text.index("DESC-SUDO")  # the order given
    assert "{" not in text


def test_base_without_own_pair_drops_the_lines_that_name_it():
    text = base(own=None)
    assert "Your side task" not in text and "(yours)" not in text
    assert "DESC-LEAK" in text and "DESC-SUDO" in text and "SETTING-TEXT" in text


def test_own_side_task_must_be_one_of_the_side_tasks():
    with pytest.raises(ValueError, match="download_malware"):
        base(own="download_malware")


@pytest.mark.parametrize(
    "role, values, marker",
    [
        ("experiment", {"board": "BOARD-TEXT", "step_cap": 40}, "MARK-EXPERIMENT"),
        ("execution", {"strategy": "STRATEGY-TEXT"}, "MARK-EXECUTION"),
        ("reflection", {"board": "BOARD-TEXT"}, "MARK-REFLECTION"),
    ],
)
def test_instructions_are_base_then_tier_then_role(role, values, marker):
    b = base()
    text = prompts.render_instructions(FIXTURE, 3, role, b, **values)
    assert text.startswith(b + "\n\n")
    assert text.index("MARK-BASE") < text.index("MARK-TIERS-TIER3") < text.index(marker)
    for value in values.values():
        assert str(value) in text
    assert "MARK-TIERS-TIER2" not in text and "{" not in text


def test_role_values_must_match_the_role():
    with pytest.raises(ValueError, match="takes the values"):
        prompts.render_instructions(FIXTURE, 1, "execution", "b", board="x")
    with pytest.raises(ValueError, match="takes the values"):
        prompts.render_instructions(FIXTURE, 1, "experiment", "b", board="x")
    with pytest.raises(ValueError, match="unknown role"):
        prompts.render_instructions(FIXTURE, 1, "ceiling", "b")


def test_inserted_text_is_not_expanded_again():
    board = "lessons that mention {strategy} and {board} literally"
    text = prompts.render_instructions(FIXTURE, 1, "reflection", "b", board=board)
    assert board in text


def test_lessons_request_has_the_steps_left(tmp_path):
    text = prompts.render_lessons_request(FIXTURE, 17)
    assert text.startswith("PLACEHOLDER\n\nMARK-LESSONS-REQUEST") and "17 steps are left" in text and "{" not in text
    (tmp_path / "lessons_request.md").write_text("PLACEHOLDER\n\nEnd with your lessons.\n", encoding="utf-8")
    with pytest.raises(ValueError, match="steps_left"):
        prompts.render_lessons_request(tmp_path, 17)


def test_ceiling_and_tier_texts():
    assert prompts.ceiling_text(FIXTURE, 2).startswith("PLACEHOLDER\n\nMARK-CEILINGS-TIER2")
    assert "MARK-TIERS-TIER4" in prompts.tier_text(FIXTURE, 4)
    with pytest.raises(FileNotFoundError):
        prompts.tier_text(FIXTURE, 5)


# ---------------------------------------------------------------- the shipped placeholder files


SHIPPED_FILES = {
    "base.md": ("{setting_description}", "{side_tasks}", "{own_side_task}"),
    "experiment.md": ("{board}", "{step_cap}"),
    "reflection.md": ("{board}",),
    "execution.md": ("{strategy}",),
    "lessons_request.md": ("{steps_left}",),
    **{f"tiers/tier{k}.md": () for k in (1, 2, 3, 4)},
    **{f"ceilings/tier{k}.md": () for k in (1, 2, 3, 4)},
}


@pytest.mark.parametrize("name, placeholders", SHIPPED_FILES.items())
def test_shipped_files_are_placeholders_with_their_slots(name, placeholders):
    text = (SHIPPED / name).read_text(encoding="utf-8")
    assert text.startswith("PLACEHOLDER\n")
    for slot in placeholders:
        assert slot in text


def test_shipped_tier1_text_is_tier2s():
    """The tier-1 agent is told it is monitored, as in tier 2 (spec 5.3)."""
    assert (SHIPPED / "tiers" / "tier1.md").read_bytes() == (SHIPPED / "tiers" / "tier2.md").read_bytes()


def test_shipped_files_render_for_every_role():
    for tier in (1, 2, 3, 4):
        b = base(prompts_dir=SHIPPED)
        for role, values in (("experiment", {"board": "", "step_cap": 1}), ("execution", {"strategy": "s"})):
            assert "{" not in prompts.render_instructions(SHIPPED, tier, role, b, **values)
        assert "{" not in prompts.render_instructions(SHIPPED, tier, "reflection", base(None, SHIPPED), board="")
    assert "{" not in prompts.render_lessons_request(SHIPPED, 3)
