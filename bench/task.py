"""Inspect plumbing shared by the strategy search and the execution (docs/spec-exp3.md section 12.7).

- Main tasks: `main_task_split` reads `<run_dir>/preflight.json` (key `kept`, written by `python -m bench preflight`,
  in the order of the shuffled pool); the first `search_n` ids are the search main tasks, the next `test_n` the test
  main tasks.
- Workspaces: each main task's Dockerfile and patched compose file under `<run_dir>/workspaces/`.
- Samples: `make_sample` builds one sample. Its input is the user message (the main task); its metadata says what the
  solver runs (`sample_metadata`), including the developer instructions and the results folder.
- Tasks: `build_task` makes one Inspect task (one tier's search wave, one tier's execution, or one ceiling run);
  `run_tasks` runs several as one eval set in `<run_dir>/inspect_logs/<name>/` (one folder per eval set: Inspect
  refuses a log folder that holds logs of other tasks), so a stopped run continues where it stopped.
- Rows: `samples.jsonl` has one row per sample (`sample_row`), sorted by the sample's folder.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import yaml
from inspect_ai import Task, eval_set
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.log import EvalLog, EvalSample, read_eval_log_samples
from inspect_ai.model import Model
from inspect_ai.scorer import CORRECT

from bench import basharena, image, prompts
from bench.config import REPO_ROOT, SIDE_TASKS, ConfigError, RunConfig
from bench.redo import is_provider_failure
from bench.solver import INSTRUCTIONS_KEY, SAMPLE_DIR_KEY, STORE_KEY, ModelFactory, meltzer_solver, run_model_factory

SIDE_SCORER = "side_task_scorer"
MAIN_SCORER = "main_task_scorer"
# Fields of an eval log sample that `samples.jsonl` does not need (they are the large ones).
SKIPPED_SAMPLE_FIELDS = {"events", "events_data", "attachments", "messages", "timelines"}
LOGS_DIR = "inspect_logs"
# Inspect retries an unfinished task at most this often, after RETRY_WAIT_SECONDS (its defaults: 10 and minutes).
EVAL_RETRY_ATTEMPTS = 1
EVAL_RETRY_WAIT_SECONDS = 30


class PreflightError(ConfigError):
    """`preflight.json` is missing or lists fewer tasks than the run needs."""


# ---------------------------------------------------------------- main tasks


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


def main_task_split(cfg: RunConfig, run_dir: Path) -> tuple[list[str], list[str]]:
    """(search main tasks, test main tasks): disjoint by construction."""
    main_tasks = cfg.setting.main_tasks
    ids = kept_main_tasks(run_dir, main_tasks.n)
    return ids[: main_tasks.search_n], ids[main_tasks.search_n :]


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


# ---------------------------------------------------------------- prompts and samples


def base_prompt(cfg: RunConfig, own_side_task: str | None) -> str:
    """base.md with BashArena's setting description and all 4 side tasks (spec 2); `own_side_task` None for the
    reflection (no own pair)."""
    return prompts.render_base(
        REPO_ROOT / cfg.prompts.dir,
        setting_description=basharena.setting_description(),
        side_tasks={name: basharena.SIDE_TASKS[name].description for name in SIDE_TASKS},
        own_side_task=own_side_task,
    )


def sample_metadata(
    *,
    phase: str,
    tier: int,
    group: str,
    main_task: str,
    side_task: str,
    sample_dir: str,
    developer_instructions: str,
    wave: int | None = None,
    experiment: int | None = None,
) -> dict[str, Any]:
    """What the solver and `samples.jsonl` read. `group`: `tier<k>` or `ceiling<k>`; `sample_dir`: the results folder,
    relative to the run folder."""
    return {
        "phase": phase,
        "tier": tier,
        "group": group,
        "wave": wave,
        "experiment": experiment,
        "main_task": main_task,
        "side_task": side_task,
        SAMPLE_DIR_KEY: sample_dir,
        INSTRUCTIONS_KEY: developer_instructions,
    }


def make_sample(sample_id: str, compose_path: Path | None, metadata: dict[str, Any]) -> Sample:
    """One sample in the main task's stack (no stack when `compose_path` is None: tests without Docker)."""
    return Sample(
        input=prompts.render_user_message(basharena.load_task(metadata["main_task"]).description),
        id=sample_id,
        metadata=metadata,
        sandbox=("docker", str(compose_path)) if compose_path is not None else None,
    )


def build_task(cfg: RunConfig, run_dir: Path, name: str, samples: list[Sample], model_factory: ModelFactory | None = None) -> Task:
    return Task(
        dataset=MemoryDataset(samples, name=name),
        solver=meltzer_solver(cfg, run_dir, model_factory),
        scorer=[basharena.side_task_scorer(), basharena.main_task_scorer()],
        time_limit=cfg.limits.sample_time_limit_seconds,
        name=name,
    )


# ---------------------------------------------------------------- samples.jsonl


def sample_row(sample: EvalSample) -> dict[str, Any]:
    """One `samples.jsonl` row from an eval log sample."""
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
        status = "infra_error"  # Inspect's own time limit is the backstop for a hung run
    elif exit_code is not None and not expected_exit:
        status = "infra_error"
    elif exit_code in (1, 2):
        status = "meltzer_error"
    else:
        status = "ok"

    row = {
        "sample": metadata.get(SAMPLE_DIR_KEY),
        "sample_id": str(sample.id),
        "group": metadata.get("group"),
        "phase": metadata.get("phase"),
        "tier": metadata.get("tier"),
        "wave": metadata.get("wave"),
        "experiment": metadata.get("experiment"),
        "main_task": metadata.get("main_task"),
        "side_task": metadata.get("side_task"),
        "status": status,
        "exit_code": exit_code,
        "side_pass": None if side is None else side.value == CORRECT,
        "side_explanation": None if side is None else side.explanation,
        "main_score": None if main is None else main.value,
        "time_limit_hit": bool(result.get("time_limit_hit")) or inspect_time_limit,
        "end_reason": result.get("end_reason"),
        "breaker_stops": result.get("breaker_stops"),  # turns stopped by automatic review
        "steps": result.get("steps"),  # model requests of the main session (search only)
        "side_task_seconds": result.get("side_task_seconds"),
        "wait_seconds": result.get("wait_seconds"),
        "sample_error": sample.error.message if sample.error is not None else None,  # the solver or its setup failed
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


def write_samples_jsonl(run_dir: Path, rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Write `<run_dir>/samples.jsonl` (rows sorted by sample folder) and return the rows."""
    rows = sorted(rows, key=lambda row: row["sample"])
    text = "".join(json.dumps(row) + "\n" for row in rows)
    (Path(run_dir) / "samples.jsonl").write_text(text, encoding="utf-8", newline="\n")
    return rows


MELTZER_CONFIG_ERROR = 2  # meltzer's exit code for a config error: meltzer did not run the session


def exclusion_reason(row: dict, run_dir: Path) -> str | None:
    """`provider_failure`, `infra_error`, `config_error` (meltzer exit 2), or None for a sample that counts.
    A model error (exit 1 without the provider-failure line) counts: the agent ran and did not succeed."""
    if is_provider_failure(Path(run_dir) / row["sample"], row.get("exit_code")):
        return "provider_failure"
    if row.get("status") == "infra_error":
        return "infra_error"
    if row.get("exit_code") == MELTZER_CONFIG_ERROR:
        return "config_error"
    return None


# ---------------------------------------------------------------- eval sets


def run_tasks(
    cfg: RunConfig,
    run_dir: Path,
    name: str,
    tasks: Sequence[Task],
    *,
    model_factory: ModelFactory | None = None,
    eval_model: str | Model | None = None,
    max_samples: int | None = None,
    retry_on_error: int = 1,
) -> tuple[list[dict[str, Any]], str | None]:
    """Run `tasks` side by side as one eval set in `<run_dir>/inspect_logs/<name>/`. Returns the rows of their
    samples, and None or, if a task did not finish, why.

    Called again with the same tasks, the eval set continues where it stopped: finished samples are kept, the ones
    in progress start again. At most `max_samples` (default `parallel.max_samples`) samples run at once over all
    tasks (one sandbox each). `max_samples` is not part of the tasks' identity, so a resume can run fewer at once.

    The eval's own model is the agent model (the same object as the agent alias: `get_model` returns the same
    instance for the same arguments): the bridge serves requests with a model name outside the aliases (for
    example a child session started with another `-m`) with the eval's model, so they keep the provider pin
    and the reasoning effort. `model_factory` and `eval_model` replace OpenRouter (tests use `mockllm`).
    """
    agent = cfg.models.agent
    samples = max_samples or cfg.parallel.max_samples
    _, logs = eval_set(
        list(tasks),
        model=eval_model or (model_factory or run_model_factory(cfg))(agent.id, agent),
        max_tasks=len(tasks),
        max_samples=samples,
        max_sandboxes=samples,
        log_dir=str(Path(run_dir) / LOGS_DIR / name),
        fail_on_error=False,
        retry_on_error=retry_on_error,
        retry_attempts=EVAL_RETRY_ATTEMPTS,
        retry_wait=EVAL_RETRY_WAIT_SECONDS,
    )
    rows = [sample_row(s) for s in read_samples(logs)]
    failed = [log for log in logs if log.status != "success"]
    if not failed:
        return rows, None
    error = failed[0].error.message if failed[0].error else failed[0].status
    return rows, f"the Inspect run {name} did not finish ({failed[0].status}): {error}"
