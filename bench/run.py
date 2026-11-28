"""The run (docs/spec-exp3.md sections 3 and 12.7): what `python -m bench run` and `resume` call.

`run_experiment(cfg, run_dir)` runs, after `run` created `run_dir` with `config.yaml` and `manifest.json`:

0. a check of the prompt files and the docs folder (`check_inputs`);
1. the main tasks from `preflight.json` (search and test, disjoint), the images and the task workspaces;
2. the search pairs (the same in every tier);
3. for each wave in order, one eval set with that wave of every tier (`inspect_logs/search-wave<w>/`); after it,
   the lessons, scores and board of every tier are written. Search experiments are not retried by Inspect: a
   failed experiment has an empty board entry;
4. the reflection of every tier without `strategy.md`, unless a tier has no experiment that left a meltzer
   session (the run stops there);
5. one eval set with every tier's execution and ceiling run (`inspect_logs/execution/`).

`samples.jsonl` is rewritten after each eval set with every row so far. The run is resumable and idempotent:
called again with the same config, each eval set continues where it stopped (finished samples are kept), the
boards are rebuilt from the finished logs, and the reflection is skipped where `strategy.md` exists.
"""

from __future__ import annotations

from pathlib import Path

import httpx
from inspect_ai.model import Model

from bench import basharena, execution, image, prompts, search, task, tiers
from bench.config import REPO_ROOT, SIDE_TASKS, ConfigError, RunConfig
from bench.solver import ModelFactory

EXECUTION_LOGS = "execution"


def wave_logs(wave: int) -> str:
    return f"search-wave{wave}"


class InputError(ConfigError):
    """A file the run needs is missing or does not render (the CLI prints it as `error: ...`)."""


def check_inputs(cfg: RunConfig) -> None:
    """Fail before anything is run or paid for: every prompt file the run needs exists and renders (base,
    experiment, reflection and execution, the tier text of every configured tier, the ceiling texts when the run
    has ceilings, the lessons request) and `materials.docs` holds docs."""
    prompts_dir = REPO_ROOT / cfg.prompts.dir
    try:
        base = prompts.render_base(
            prompts_dir, setting_description="", side_tasks=dict.fromkeys(SIDE_TASKS, ""), own_side_task=SIDE_TASKS[0]
        )
        reflection_base = prompts.render_base(
            prompts_dir, setting_description="", side_tasks=dict.fromkeys(SIDE_TASKS, ""), own_side_task=None
        )
        for k in cfg.tiers:
            prompts.render_instructions(prompts_dir, k, "experiment", base, board="", step_cap=cfg.search.step_cap)
            prompts.render_instructions(prompts_dir, k, "reflection", reflection_base, board="")
            prompts.render_instructions(prompts_dir, k, "execution", base, strategy="")
            if cfg.execution.ceilings:
                prompts.ceiling_text(prompts_dir, k)
        prompts.render_lessons_request(prompts_dir, cfg.search.step_cap)
        tiers.docs_files(REPO_ROOT / cfg.materials.docs, tiers.DOCS_PATH)
    except (OSError, ValueError, UnicodeDecodeError) as e:
        raise InputError(f"the run's inputs are not usable: {e}") from None


def run_experiment(
    cfg: RunConfig,
    run_dir: Path,
    *,
    model_factory: ModelFactory | None = None,
    eval_model: str | Model | None = None,
    max_samples: int | None = None,
    reflection_transport: httpx.BaseTransport | None = None,
    api_key: str | None = None,
) -> dict[str, list[str]]:
    """Run the search, the reflections and the execution; return the main task ids (`{"search": ..., "test": ...}`).

    `max_samples` replaces `parallel.max_samples` for this call only. `model_factory`, `eval_model`,
    `reflection_transport` and `api_key` replace OpenRouter in tests. Raises RuntimeError when an eval set or a
    reflection did not finish; calling again continues the run.
    """
    run_dir = Path(run_dir)
    check_inputs(cfg)
    search_ids, test_ids = task.main_task_split(cfg, run_dir)
    compose_paths = workspaces(cfg, run_dir, search_ids + test_ids)
    pairs = search.search_pairs(
        search_ids, cfg.setting.side_tasks, cfg.search.waves * cfg.search.per_wave, cfg.seeds.search_pairs
    )
    options = dict(model_factory=model_factory, eval_model=eval_model, max_samples=max_samples)

    rows: list[dict] = []
    entries: dict[int, list[search.Entry]] = {tier: [] for tier in cfg.tiers}
    for wave in range(1, cfg.search.waves + 1):
        boards = {tier: search.render_board(entries[tier]) for tier in cfg.tiers}
        tasks = search.search_tasks(cfg, run_dir, wave, pairs, boards, compose_paths, model_factory)
        # A failed experiment is not run again (spec 5.1): no Inspect retry. Accepted limit: an experiment that was
        # in progress when the process stopped does start again on resume, so whether an errored experiment runs
        # again depends on when the crash happened.
        wave_rows, error = task.run_tasks(cfg, run_dir, wave_logs(wave), tasks, retry_on_error=0, **options)
        rows += wave_rows
        task.write_samples_jsonl(run_dir, rows)
        if error:
            raise RuntimeError(error)
        for tier, new in search.collect_wave(cfg, run_dir, wave, pairs, wave_rows).items():
            entries[tier] += new
            search.write_board(run_dir, tier, entries[tier])

    empty = search.tiers_without_sessions(cfg, run_dir)
    if empty:
        raise RuntimeError(
            f"no search experiment of tier {', '.join(map(str, empty))} produced a meltzer session; "
            "stopping before the reflections and the execution"
        )
    boards = {tier: search.render_board(entries[tier]) for tier in cfg.tiers}
    strategies = search.run_reflections(cfg, run_dir, boards, transport=reflection_transport, api_key=api_key)

    tasks = execution.execution_tasks(cfg, run_dir, test_ids, strategies, compose_paths, model_factory)
    execution_rows, error = task.run_tasks(cfg, run_dir, EXECUTION_LOGS, tasks, retry_on_error=1, **options)
    task.write_samples_jsonl(run_dir, rows + execution_rows)
    if error:
        raise RuntimeError(error)
    return {"search": search_ids, "test": test_ids}


def workspaces(cfg: RunConfig, run_dir: Path, main_task_ids: list[str]) -> dict[str, Path]:
    """Build the images (once per meltzer tree) and write the task workspaces; return the compose paths."""
    meltzer, gateway = image.ensure_images()
    return task.write_workspaces(cfg, main_task_ids, run_dir, meltzer, gateway)


# ---------------------------------------------------------------- example prompts (render-prompts)

EXAMPLE_BOARD = search.render_board([(1, 1, "(lessons of experiment 1.1)"), (1, 2, search.NO_LESSONS)])
EXAMPLE_STRATEGY = "(the strategy written by the reflection)"


def example_prompts(cfg: RunConfig, main_task: str, side_task: str) -> dict[str, str]:
    """File name -> text of the prompts of one pair in every role and tier: the user message, the experiment of
    wave 1 (empty board) and of a later wave (`EXAMPLE_BOARD`), the reflection, the execution and the ceiling run."""
    prompts_dir = REPO_ROOT / cfg.prompts.dir
    base = task.base_prompt(cfg, side_task)
    files = {"user_message.md": prompts.render_user_message(basharena.load_task(main_task).description)}
    step_cap = cfg.search.step_cap
    for k in cfg.tiers:
        files[f"tier{k}_experiment_wave1.md"] = prompts.render_instructions(
            prompts_dir, k, "experiment", base, board="", step_cap=step_cap
        )
        files[f"tier{k}_experiment_wave2.md"] = prompts.render_instructions(
            prompts_dir, k, "experiment", base, board=EXAMPLE_BOARD, step_cap=step_cap
        )
        files[f"tier{k}_reflection.md"] = search.reflection_text(cfg, k, EXAMPLE_BOARD)
        files[f"tier{k}_execution.md"] = prompts.render_instructions(
            prompts_dir, k, "execution", base, strategy=EXAMPLE_STRATEGY
        )
        if cfg.execution.ceilings:
            files[f"ceiling{k}_execution.md"] = prompts.render_instructions(
                prompts_dir, k, "execution", base, strategy=prompts.ceiling_text(prompts_dir, k)
            )
    return files


def write_example_prompts(cfg: RunConfig, out_dir: Path, main_task: str, side_task: str) -> list[Path]:
    """Write `example_prompts` into `out_dir` (LF); return the paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in example_prompts(cfg, main_task, side_task).items():
        path = out_dir / name
        path.write_text(text + "\n", encoding="utf-8", newline="\n")
        written.append(path)
    return written
