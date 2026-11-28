"""Tests for bench.execution: test pairs, execution and ceiling samples and tasks (docs/spec-exp3.md section 6)."""

from __future__ import annotations

import dataclasses

import pytest

from bench import basharena, execution, prompts
from bench.config import REPO_ROOT, load_config

CONFIG = REPO_ROOT / "tests" / "fixtures" / "exp3" / "config.yaml"
TEST_IDS = basharena.pool_ids()[3:5]
SIDES = ("download_malware", "leak_passwords", "disable_firewall", "sudo_edit")


@pytest.fixture(scope="module")
def cfg():
    return load_config(CONFIG)


def compose(ids):
    return {task_id: None for task_id in ids}


def test_test_pairs_are_every_test_main_task_with_every_side_task():
    pairs = execution.execution_pairs(["a", "b"], SIDES)
    assert pairs == [(m, s) for m in ("a", "b") for s in SIDES]


def test_execution_samples_carry_the_strategy(cfg):
    pairs = execution.execution_pairs(TEST_IDS, SIDES)
    samples = execution.execution_samples(cfg, 2, pairs, compose(TEST_IDS), strategy="STRATEGY-OF-TIER-2")
    assert sorted(s.id for s in samples) == sorted(f"{m}__{s}" for m, s in pairs)
    for sample in samples:
        meta = sample.metadata
        assert (meta["phase"], meta["tier"], meta["group"], meta["wave"]) == ("execution", 2, "tier2", None)
        assert meta["dir"] == f"tier2/execution/{meta['main_task']}__{meta['side_task']}"
        text = meta["developer_instructions"]
        assert text.index("MARK-BASE") < text.index("MARK-TIERS-TIER2") < text.index("MARK-EXECUTION") < text.index("STRATEGY-OF-TIER-2")
        assert f"### {meta['side_task']} (yours)" in text and "MARK-CEILINGS" not in text
        assert sample.input == "Your task is as follows:\n" + basharena.load_task(meta["main_task"]).description


def test_ceiling_samples_have_the_ceiling_text_in_place_of_the_strategy(cfg):
    pairs = execution.execution_pairs(TEST_IDS, SIDES)
    ceiling = execution.execution_samples(cfg, 3, pairs, compose(TEST_IDS), ceiling=True)
    strategy = execution.execution_samples(cfg, 3, pairs, compose(TEST_IDS), strategy=prompts.ceiling_text(REPO_ROOT / cfg.prompts.dir, 3))
    for c, s in zip(ceiling, strategy):
        assert c.metadata["group"] == "ceiling3" and c.metadata["dir"].startswith("ceiling3/execution/")
        assert "MARK-CEILINGS-TIER3" in c.metadata["developer_instructions"]
        # the same setup as the tier-3 execution: only the text in place of the strategy differs
        assert c.metadata["developer_instructions"] == s.metadata["developer_instructions"]
        assert c.metadata["tier"] == 3 and c.metadata["phase"] == "execution"
    with pytest.raises(ValueError, match="strategy"):
        execution.execution_samples(cfg, 3, pairs, compose(TEST_IDS), strategy="x", ceiling=True)
    with pytest.raises(ValueError, match="strategy"):
        execution.execution_samples(cfg, 3, pairs, compose(TEST_IDS))


def test_sample_order_is_shuffled_by_the_seed(cfg):
    pairs = execution.execution_pairs(TEST_IDS, SIDES)
    order = lambda c: [s.id for s in execution.execution_samples(c, 1, pairs, compose(TEST_IDS), strategy="s")]
    assert order(cfg) == order(cfg) and sorted(order(cfg)) == sorted(f"{m}__{s}" for m, s in pairs)
    other = dataclasses.replace(cfg, seeds=dataclasses.replace(cfg.seeds, inspect_shuffle=1))
    assert order(other) != order(cfg)


def test_one_task_per_tier_and_per_ceiling(cfg, tmp_path):
    strategies = {k: f"S{k}" for k in cfg.tiers}
    tasks = execution.execution_tasks(cfg, tmp_path, TEST_IDS, strategies, compose(TEST_IDS))
    names = [t.name for t in tasks]
    assert names == ["tier1-execution", "ceiling1-execution", "tier2-execution", "ceiling2-execution",
                     "tier3-execution", "ceiling3-execution", "tier4-execution", "ceiling4-execution"]  # fmt: skip
    assert all(len(t.dataset) == 8 for t in tasks)
    no_ceilings = dataclasses.replace(cfg, execution=dataclasses.replace(cfg.execution, ceilings=False))
    tasks = execution.execution_tasks(no_ceilings, tmp_path, TEST_IDS, strategies, compose(TEST_IDS))
    assert [t.name for t in tasks] == [f"tier{k}-execution" for k in (1, 2, 3, 4)]
