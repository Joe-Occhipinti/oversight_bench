"""Inspect task for the experiment and `run_experiment` (spec 7.6, 10.2).

`run_experiment(cfg, run_dir)` is what `python -m bench run` calls (after it created `run_dir` with
`config.yaml` and `manifest.json`). It

1. reads the main tasks from `<run_dir>/preflight.json` (key `kept`, written by `python -m bench preflight`),
2. builds the images and writes each main task's workspace (Dockerfile and patched compose file) under
   `<run_dir>/workspaces/`,
3. runs one Inspect task whose samples are conditions x main tasks x side tasks, in an order shuffled with
   `seeds.inspect_shuffle`,
4. writes `<run_dir>/samples.jsonl`, one row per sample, from the Inspect log (10.2).

Sample id: `<condition>__<main_task>__<side_task>`. The sample input is the user message (9.1).
"""

from __future__ import annotations

import json
import random
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml
from inspect_ai import Task, eval_set
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.log import EvalLog, EvalSample, read_eval_log_samples
from inspect_ai.model import Model
from inspect_ai.scorer import CORRECT

from bench import assignment, basharena, image, prompts
from bench.config import ConfigError, RunConfig
from bench.solver import STORE_KEY, ModelFactory, meltzer_solver, run_model_factory

SIDE_SCORER = "side_task_scorer"
MAIN_SCORER = "main_task_scorer"
# Fields of an eval log sample that `samples.jsonl` does not need (they are the large ones).
SKIPPED_SAMPLE_FIELDS = {"events", "events_data", "attachments", "messages", "timelines"}


class PreflightError(ConfigError):
    """`preflight.json` is missing or lists fewer tasks than the run needs."""


# ---------------------------------------------------------------- main tasks and samples


def kept_main_tasks(run_dir: Path, n: int) -> list[str]:
    """The first `n` ids of `kept` in `<run_dir>/preflight.json`."""
    path = Path(run_dir) / "preflight.json"
    hint = "run `python -m bench preflight <run config>` first"
    if not path.is_file():
        raise PreflightError(f"{path} not found; {hint}")
    kept = json.loads(path.read_text(encoding="utf-8")).get("kept")
    if not isinstance(kept, list) or len(kept) < n:
        raise PreflightError(f"{path} lists {len(kept) if isinstance(kept, list) else 0} tasks, need {n}; {hint}")
    return [str(task_id) for task_id in kept[:n]]


def build_samples(cfg: RunConfig, main_task_ids: list[str], compose_paths: dict[str, Path]) -> list[Sample]:
    """One sample per condition, main task and side task, shuffled with `seeds.inspect_shuffle`.

    In aware mode `one_card`, the aware samples carry their assigned card in the metadata (`assigned_card`)."""
    cards = assignment.assigned_cards(cfg, main_task_ids)
    samples = []
    for condition in cfg.conditions:
        for main_task in main_task_ids:
            user_message = prompts.render_user_message(basharena.load_task(main_task).description)
            for side_task in cfg.setting.side_tasks:
                metadata = {"condition": condition, "main_task": main_task, "side_task": side_task}
                if condition == "aware" and cards:
                    metadata["assigned_card"] = cards[(main_task, side_task)]
                samples.append(
                    Sample(
                        input=user_message,
                        id=f"{condition}__{main_task}__{side_task}",
                        metadata=metadata,
                        sandbox=("docker", str(compose_paths[main_task])),
                    )
                )
    random.Random(cfg.seeds.inspect_shuffle).shuffle(samples)
    return samples


def write_workspaces(cfg: RunConfig, main_task_ids: list[str], run_dir: Path, meltzer: str, gateway: str) -> dict[str, Path]:
    """Write each task's Dockerfile and patched compose file; return the compose paths by task id."""

    def patch(text: str) -> str:
        return yaml.safe_dump(image.patch_compose(yaml.safe_load(text), gateway), sort_keys=False)

    paths = {}
    for task_id in main_task_ids:
        compose_path, _ = basharena.write_task_workspace(
            basharena.load_task(task_id),
            Path(run_dir) / "workspaces",
            base_image=meltzer,
            run_id=cfg.run_id,
            compose_patch=patch,
        )
        paths[task_id] = Path(compose_path).resolve()
    return paths


def build_task(cfg: RunConfig, run_dir: Path, samples: list[Sample], model_factory: ModelFactory | None = None) -> Task:
    return Task(
        dataset=MemoryDataset(samples, name=cfg.run_id),
        solver=meltzer_solver(cfg, run_dir, model_factory),
        scorer=[basharena.side_task_scorer(), basharena.main_task_scorer()],
        time_limit=cfg.limits.sample_time_limit_seconds,
        name=cfg.run_id,
    )


# ---------------------------------------------------------------- samples.jsonl (10.2)


def sample_row(sample: EvalSample) -> dict[str, Any]:
    """One `samples.jsonl` row from an eval log sample. `judge` is null until the judge runs."""
    metadata = sample.metadata
    result = sample.store.get(STORE_KEY) or {}
    scores = sample.scores or {}
    side, main = scores.get(SIDE_SCORER), scores.get(MAIN_SCORER)
    side_metadata = (side.metadata or {}) if side is not None else {}
    exit_code = result.get("exit_code")

    inspect_time_limit = sample.limit is not None and sample.limit.type == "time"
    meltzer_stopped = bool(result.get("time_limit_hit")) or result.get("side_task_seconds") is not None
    # meltzer exits 0, 1, 2 or 130; 137 follows our SIGKILL after the time limit or the stop at the side task.
    # Anything else is abnormal (killed by the system, failed to start) and the sample is dropped like an
    # infrastructure error.
    expected_exit = exit_code in (0, 1, 2) or (exit_code in (130, 137) and meltzer_stopped)
    if sample.error is not None or inspect_time_limit or side_metadata.get("check_error"):
        status = "infra_error"  # Inspect's own time limit is the backstop for a hung run (7.3, 10.2)
    elif exit_code is not None and not expected_exit:
        status = "infra_error"
    elif exit_code in (1, 2):
        status = "meltzer_error"
    else:
        status = "ok"

    row = {
        "sample_id": str(sample.id),
        "condition": metadata.get("condition"),
        "main_task": metadata.get("main_task"),
        "side_task": metadata.get("side_task"),
        "assigned_card": metadata.get("assigned_card"),
        "status": status,
        "exit_code": exit_code,
        "side_pass": None if side is None else side.value == CORRECT,
        "side_explanation": None if side is None else side.explanation,
        "main_score": None if main is None else main.value,
        "judge": None,
        "time_limit_hit": bool(result.get("time_limit_hit")) or inspect_time_limit,
        "end_reason": result.get("end_reason"),
        "side_task_seconds": result.get("side_task_seconds"),
        "wait_seconds": result.get("wait_seconds"),
    }
    if side_metadata.get("check_error_reason"):
        row["check_error_reason"] = side_metadata["check_error_reason"]
    return row


def read_samples(logs: Iterable[EvalLog]) -> Iterable[EvalSample]:
    """The samples of the logs, one at a time and without their transcripts."""
    for log in logs:
        yield from read_eval_log_samples(
            log.location, all_samples_required=False, exclude_fields=SKIPPED_SAMPLE_FIELDS
        )


def write_samples_jsonl(run_dir: Path, samples: Iterable[EvalSample]) -> list[dict[str, Any]]:
    """Write `<run_dir>/samples.jsonl` (rows sorted by sample id) and return the rows."""
    rows = sorted((sample_row(s) for s in samples), key=lambda row: row["sample_id"])
    text = "".join(json.dumps(row) + "\n" for row in rows)
    (Path(run_dir) / "samples.jsonl").write_text(text, encoding="utf-8", newline="\n")
    return rows


# ---------------------------------------------------------------- the run


# Inspect retries an unfinished task at most this often, after RETRY_WAIT_SECONDS (its defaults: 10 and minutes).
EVAL_RETRY_ATTEMPTS = 1
EVAL_RETRY_WAIT_SECONDS = 30


def run_experiment(
    cfg: RunConfig,
    run_dir: Path,
    *,
    model_factory: ModelFactory | None = None,
    eval_model: str | Model | None = None,
    max_samples: int | None = None,
) -> list[str]:
    """Run all samples and write `samples.jsonl`; return the main task ids.

    The run is an Inspect eval set in `<run_dir>/inspect_logs`: if the process stops, calling this again with the
    same `cfg` continues where it stopped. Samples that finished are kept; the ones in progress start again from
    their beginning. `max_samples` replaces `cfg.parallel.max_samples` for this call only (it is not part of the
    task's identity, so a resume can run fewer samples at once). Any other change to `cfg` makes Inspect refuse
    the old logs.

    The eval's own model is the agent model (same object as the agent alias, `get_model` returns the same
    instance for the same arguments): the bridge serves requests with a model name outside the aliases (for
    example a child session started with another `-m`) with the eval's model, so they keep the provider pin
    and the reasoning effort. `model_factory` and `eval_model` replace OpenRouter (tests use `mockllm`).
    """
    run_dir = Path(run_dir)
    main_task_ids = kept_main_tasks(run_dir, cfg.setting.main_tasks.n)
    meltzer, gateway = image.ensure_images()
    compose_paths = write_workspaces(cfg, main_task_ids, run_dir, meltzer, gateway)
    task = build_task(cfg, run_dir, build_samples(cfg, main_task_ids, compose_paths), model_factory)

    agent = cfg.models.agent
    _, logs = eval_set(
        task,
        model=eval_model or (model_factory or run_model_factory(cfg))(agent.id, agent),
        max_samples=max_samples or cfg.parallel.max_samples,
        log_dir=str(run_dir / "inspect_logs"),
        fail_on_error=False,
        retry_on_error=1,
        retry_attempts=EVAL_RETRY_ATTEMPTS,
        retry_wait=EVAL_RETRY_WAIT_SECONDS,
    )
    write_samples_jsonl(run_dir, read_samples(logs))
    failed = [log for log in logs if log.status != "success"]
    if failed:
        error = failed[0].error.message if failed[0].error else failed[0].status
        raise RuntimeError(f"the Inspect run did not finish ({failed[0].status}): {error}")
    return main_task_ids
