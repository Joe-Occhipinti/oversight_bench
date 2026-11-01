"""Tests for bench.assignment: one exploit card per pair of main task and side task (aware mode one_card)."""

import dataclasses
from collections import Counter
from pathlib import Path

import pytest

from bench import assignment, config

ROOT = Path(__file__).resolve().parents[1]
RUN_CONFIG = ROOT / "runs" / "exp1-01.yaml"
SIDE_TASKS = ["download_malware", "leak_passwords", "disable_firewall", "sudo_edit"]
CARDS = [f"E{i:02d}" for i in range(1, 15)]


def main_tasks(n):
    return [f"task{i:02d}" for i in range(n)]


def with_prompts(cfg, **changes):
    return dataclasses.replace(cfg, prompts=dataclasses.replace(cfg.prompts, **changes))


def with_main_tasks(cfg, n):
    return dataclasses.replace(
        cfg, setting=dataclasses.replace(cfg.setting, main_tasks=dataclasses.replace(cfg.setting.main_tasks, n=n))
    )


@pytest.mark.parametrize("n_cards, n_main_tasks", [(14, 14), (14, 28), (14, 98), (3, 12), (15, 30), (1, 7)])
def test_every_card_is_assigned_equally_often_within_each_side_task(n_cards, n_main_tasks):
    cards = [f"E{i:02d}" for i in range(1, n_cards + 1)]
    mains = main_tasks(n_main_tasks)
    assigned = assignment.assign_cards(cards, SIDE_TASKS, mains, seed=0)
    assert set(assigned) == {(m, s) for m in mains for s in SIDE_TASKS}
    for side_task in SIDE_TASKS:
        counts = Counter(assigned[(m, side_task)] for m in mains)
        assert set(counts) == set(cards)
        assert set(counts.values()) == {n_main_tasks // n_cards}


def test_assignment_is_fixed_by_the_seed():
    mains = main_tasks(28)
    first = assignment.assign_cards(CARDS, SIDE_TASKS, mains, seed=3)
    assert assignment.assign_cards(CARDS, SIDE_TASKS, mains, seed=3) == first
    assert assignment.assign_cards(CARDS, SIDE_TASKS, mains, seed=4) != first


def test_assignment_does_not_depend_on_the_order_of_side_tasks_or_card_ids():
    mains = main_tasks(14)
    first = assignment.assign_cards(CARDS, SIDE_TASKS, mains, seed=0)
    assert assignment.assign_cards(CARDS[::-1], SIDE_TASKS[::-1], mains, seed=0) == first
    assert assignment.assign_cards(CARDS, SIDE_TASKS[:2], mains, seed=0) == {
        key: card for key, card in first.items() if key[1] in SIDE_TASKS[:2]
    }


def test_side_tasks_get_different_shuffles():
    mains = main_tasks(14)
    assigned = assignment.assign_cards(CARDS, SIDE_TASKS, mains, seed=0)
    orders = {tuple(assigned[(m, s)] for m in mains) for s in SIDE_TASKS}
    assert len(orders) == len(SIDE_TASKS)


@pytest.mark.parametrize(
    "n_cards, n_main_tasks, nearest",
    [(14, 20, "14 or 28"), (14, 30, "28 or 42"), (14, 13, "14"), (14, 1, "14"), (15, 20, "15 or 30")],
)
def test_unbalanced_counts_are_rejected_with_the_nearest_valid_counts(n_cards, n_main_tasks, nearest):
    with pytest.raises(config.ConfigError) as error:
        assignment.check_balanced(n_cards, n_main_tasks)
    message = str(error.value)
    assert "setting.main_tasks.n" in message and f"got {n_main_tasks}" in message
    assert f"({n_cards})" in message and message.endswith(f"nearest: {nearest}")


def test_assign_cards_rejects_unbalanced_counts():
    with pytest.raises(config.ConfigError):
        assignment.assign_cards(CARDS, SIDE_TASKS, main_tasks(20), seed=0)


def test_no_cards_rejected():
    with pytest.raises(config.ConfigError, match="no cards"):
        assignment.check_balanced(0, 14)


def test_check_config_follows_the_mode_and_the_conditions():
    cfg = config.load_config(RUN_CONFIG)  # aware mode all_cards, 30 main tasks (not a multiple of 14)
    assignment.check_config(cfg)
    one = with_prompts(cfg, aware_mode="one_card")
    with pytest.raises(config.ConfigError, match="multiple of the number of cards"):
        assignment.check_config(one)
    assignment.check_config(with_main_tasks(one, 28))
    assignment.check_config(dataclasses.replace(one, conditions=("unaware",)))  # no aware samples, no cards


def test_assigned_cards_of_a_run():
    cfg = config.load_config(RUN_CONFIG)
    mains = main_tasks(28)
    assert assignment.assigned_cards(cfg, mains) == {}  # all_cards
    one = with_main_tasks(with_prompts(cfg, aware_mode="one_card"), 28)
    assigned = assignment.assigned_cards(one, mains)
    assert len(assigned) == 28 * 4 and set(assigned.values()) == set(CARDS)
    assert assigned == assignment.assign_cards(CARDS, one.setting.side_tasks, mains, one.seeds.card_assignment)
    assert assignment.assigned_cards(dataclasses.replace(one, conditions=("unaware",)), mains) == {}
    other_seed = dataclasses.replace(one, seeds=dataclasses.replace(one.seeds, card_assignment=1))
    assert assignment.assigned_cards(other_seed, mains) != assigned


def test_the_number_of_cards_comes_from_the_card_files(tmp_path):
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()
    for i in range(1, 8):
        (cards_dir / f"E{i:02d}-x.md").write_text(f"---\nid: E{i:02d}\n---\n## Summary\nx\n", encoding="utf-8")
    cfg = config.load_config(RUN_CONFIG)
    cfg = dataclasses.replace(with_prompts(cfg, aware_mode="one_card", cards=str(cards_dir)), conditions=("aware",))
    assignment.check_config(with_main_tasks(cfg, 7))
    assignment.check_config(with_main_tasks(cfg, 21))
    with pytest.raises(config.ConfigError, match=r"\(7\)"):
        assignment.check_config(with_main_tasks(cfg, 30))
