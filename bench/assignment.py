"""Card assignment for the aware mode `one_card`: each pair of main task and side task gets one exploit card.

Within a side task every card is assigned the same number of times, so the number of main tasks must be a
multiple of the number of cards. Which main task gets which card is a shuffle fixed by `seeds.card_assignment`.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from pathlib import Path

from bench import prompts
from bench.config import REPO_ROOT, ConfigError, RunConfig


def check_balanced(n_cards: int, n_main_tasks: int) -> None:
    """Raise ConfigError unless every card can be assigned the same number of times per side task."""
    if n_cards < 1:
        raise ConfigError("prompts.cards: the folder has no cards")
    if n_main_tasks % n_cards:
        below = n_main_tasks // n_cards * n_cards
        nearest = f"{below} or {below + n_cards}" if below else str(n_cards)
        raise ConfigError(
            f"setting.main_tasks.n: aware_mode one_card needs a multiple of the number of cards "
            f"({n_cards}), got {n_main_tasks}; nearest: {nearest}"
        )


def check_config(cfg: RunConfig) -> None:
    """The check of `check_balanced` for a run config; nothing to check unless the run assigns cards."""
    if cfg.prompts.aware_mode == "one_card" and "aware" in cfg.conditions:
        check_balanced(len(prompts.card_ids(REPO_ROOT / cfg.prompts.cards)), cfg.setting.main_tasks.n)


def assign_cards(
    card_ids: Sequence[str], side_tasks: Sequence[str], main_task_ids: Sequence[str], seed: int
) -> dict[tuple[str, str], str]:
    """Card id by (main task, side task). Each side task gets every card len(main_task_ids) / len(card_ids)
    times, in an order shuffled per side task (the order of `side_tasks` does not matter)."""
    check_balanced(len(card_ids), len(main_task_ids))
    repeats = len(main_task_ids) // len(card_ids)
    assigned = {}
    for side_task in side_tasks:
        cards = sorted(card_ids) * repeats
        random.Random(f"{seed}:{side_task}").shuffle(cards)
        assigned.update({(main_task, side_task): card for main_task, card in zip(main_task_ids, cards)})
    return assigned


def assigned_cards(cfg: RunConfig, main_task_ids: Sequence[str]) -> dict[tuple[str, str], str]:
    """The assignment of a run: empty unless the run is aware in mode `one_card`."""
    if cfg.prompts.aware_mode != "one_card" or "aware" not in cfg.conditions:
        return {}
    ids = prompts.card_ids(Path(REPO_ROOT) / cfg.prompts.cards)
    return assign_cards(ids, cfg.setting.side_tasks, main_task_ids, cfg.seeds.card_assignment)
