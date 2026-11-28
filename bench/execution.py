"""Execution and ceiling runs (docs/spec-exp3.md section 6).

Per tier k, one meltzer session per test pair (every test main task x every side task), with the real monitor and
sandbox, the tier-k config and materials. Developer instructions: base.md + tiers/tier<k>.md + execution.md with the
tier's strategy. A ceiling run is the same setup with ceilings/tier<k>.md in place of the strategy. Execution keeps
exp1's early stop at the side task.

Each tier's execution and each ceiling run is one Inspect task (`tier<k>-execution`, `ceiling<k>-execution`);
sample folders are `tier<k>/execution/<main>__<side>/` and `ceiling<k>/execution/<main>__<side>/`.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from inspect_ai import Task
from inspect_ai.dataset import Sample

from bench import prompts
from bench.config import REPO_ROOT, RunConfig
from bench.solver import ModelFactory
from bench.task import base_prompt, build_task, make_sample, sample_metadata


def execution_pairs(test_main_tasks: Sequence[str], side_tasks: Sequence[str]) -> list[tuple[str, str]]:
    """The test pairs: every test main task with every side task, the same for every tier and ceiling."""
    return [(main, side) for main in test_main_tasks for side in side_tasks]


def group_name(tier: int, ceiling: bool) -> str:
    return f"ceiling{tier}" if ceiling else f"tier{tier}"


def execution_samples(
    cfg: RunConfig,
    tier: int,
    pairs: Sequence[tuple[str, str]],
    compose_paths: Mapping[str, Any],
    *,
    strategy: str | None = None,
    ceiling: bool = False,
) -> list[Sample]:
    """The samples of tier `tier`'s execution with `strategy`, or of its ceiling run (`ceiling=True`), in an order
    shuffled with `seeds.inspect_shuffle`."""
    prompts_dir = REPO_ROOT / cfg.prompts.dir
    if ceiling == (strategy is not None):
        raise ValueError("pass a strategy for an execution, or ceiling=True for a ceiling run")
    text = prompts.ceiling_text(prompts_dir, tier) if ceiling else strategy
    group = group_name(tier, ceiling)
    samples = []
    for main_task, side_task in pairs:
        instructions = prompts.render_instructions(prompts_dir, tier, "execution", base_prompt(cfg, side_task), strategy=text)
        metadata = sample_metadata(
            phase="execution", tier=tier, group=group, main_task=main_task, side_task=side_task,
            sample_dir=f"{group}/execution/{main_task}__{side_task}", developer_instructions=instructions,
        )  # fmt: skip
        samples.append(make_sample(f"{main_task}__{side_task}", compose_paths[main_task], metadata))
    random.Random(cfg.seeds.inspect_shuffle).shuffle(samples)
    return samples


def execution_tasks(
    cfg: RunConfig,
    run_dir: Path,
    test_main_tasks: Sequence[str],
    strategies: Mapping[int, str],
    compose_paths: Mapping[str, Any],
    model_factory: ModelFactory | None = None,
) -> list[Task]:
    """One task per tier's execution and, with `execution.ceilings`, one per tier's ceiling run."""
    pairs = execution_pairs(test_main_tasks, cfg.setting.side_tasks)
    tasks = []
    for tier in cfg.tiers:
        runs = [(False, strategies[tier])] + ([(True, None)] if cfg.execution.ceilings else [])
        for ceiling, strategy in runs:
            samples = execution_samples(cfg, tier, pairs, compose_paths, strategy=strategy, ceiling=ceiling)
            tasks.append(build_task(cfg, run_dir, f"{group_name(tier, ceiling)}-execution", samples, model_factory))
    return tasks
