"""Pre-flight check of the main-task pool (spec 5.2).

`preflight(cfg, run_dir)` walks the pool in the order fixed by `main_tasks.seed`. For each task it
builds the task image `FROM` the meltzer image, starts the task's compose stack (with the gateway
service), runs steps 1-2 of the environment setup (5.4) and stops the stack. The first
`main_tasks.n` tasks that pass are kept. The tasks are checked in batches, several at a time, each in its
own worker process (`process_batch`; at most `PREFLIGHT_MAX_WORKERS`, and `parallel.max_samples`): a batch
is the next tasks still needed, so the checked tasks are the same as when checking them one by one. The
result is written to `<run_dir>/preflight.json`:

    {"kept": [<task ids in order>], "skipped": [{"task_id": ..., "error": ...}]}

Each stack runs as one Inspect sample with Inspect's `mockllm` model (no API calls). The task
workspaces (Dockerfile and compose file) are written to `<run_dir>/workspaces/` and removed at the
end: a run refuses to start in a results folder that holds anything but `preflight.json` and
`prompts/` (`bench.config.check_run_dir_free`), and the run writes its own workspaces.
"""

from __future__ import annotations

import functools
import json
import multiprocessing
import shutil
import sys
import tempfile
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import yaml
from inspect_ai import Task, eval
from inspect_ai.dataset import Sample
from inspect_ai.solver import Generate, TaskState, solver

from bench import basharena, image

ERROR_CHARS = 2000
# Covers the stack start and the two setup steps (each limited to 300 s by `run_environment_setup`).
SAMPLE_TIME_LIMIT_SECONDS = 1200

# At most this many tasks are checked at once: each check builds an image (CPU and network) and starts a stack.
PREFLIGHT_MAX_WORKERS = 8

# Checks one task; returns None when it passes, else the error text.
TaskCheck = Callable[[str], "str | None"]
# Checks several tasks; returns their results in the order of the task ids.
BatchRunner = Callable[[TaskCheck, "list[str]"], "list[str | None]"]


def sequential_batch(check_task: TaskCheck, task_ids: list[str]) -> list[str | None]:
    """Check the tasks one after the other."""
    return [check_task(task_id) for task_id in task_ids]


def process_batch(workers: int) -> BatchRunner:
    """A batch runner that checks the tasks in worker processes, `workers` at a time.

    Each check runs its own Inspect `eval`, which keeps global state, so the checks get a process each
    (`check_task` and its arguments must be picklable). A worker that fails gives the error text. Each finished
    check is reported on stderr as it ends; the results are returned in the order of the task ids.
    """

    def run(check_task: TaskCheck, task_ids: list[str]) -> list[str | None]:
        if not task_ids:
            return []
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=min(workers, len(task_ids)), mp_context=context) as pool:
            futures = {pool.submit(check_task, task_id): index for index, task_id in enumerate(task_ids)}
            results: list[str | None] = [None] * len(task_ids)
            for done, future in enumerate(as_completed(futures), start=1):
                index = futures[future]
                try:
                    results[index] = future.result()
                except Exception as exc:
                    results[index] = f"worker: {type(exc).__name__}: {exc}"
                status = "ok" if results[index] is None else "failed"
                print(f"preflight: checked {task_ids[index]}: {status} ({done}/{len(task_ids)} of this batch)", file=sys.stderr)
        return results

    return run


def preflight_workers(cfg: Any) -> int:
    """Tasks checked at once: the run's `parallel.max_samples`, at most `PREFLIGHT_MAX_WORKERS`."""
    return max(1, min(cfg.parallel.max_samples, PREFLIGHT_MAX_WORKERS))


def walk_pool(
    order: list[str], n: int, check_task: TaskCheck, run_batch: BatchRunner = sequential_batch
) -> tuple[list[str], list[dict]]:
    """Check tasks in `order` until `n` pass. Returns the kept ids and the skipped `{task_id, error}`.

    The tasks go to `run_batch` in batches. A batch is the next tasks still needed (n minus the kept so far),
    so the tasks checked, and the result, are the same as when checking them one by one."""
    kept: list[str] = []
    skipped: list[dict] = []
    position = 0
    while len(kept) < n and position < len(order):
        batch = order[position : position + n - len(kept)]
        position += len(batch)
        for task_id, error in zip(batch, run_batch(check_task, batch)):
            if error is None:
                kept.append(task_id)
                print(f"preflight: {task_id} ok ({len(kept)}/{n})", file=sys.stderr)
            else:
                error = error[:ERROR_CHARS]
                skipped.append({"task_id": task_id, "error": error})
                print(f"preflight: {task_id} skipped: {error.splitlines()[0] if error else ''}", file=sys.stderr)
    return kept, skipped


@solver
def _setup_only(task: Any, outcome: dict):
    """Steps 1-2 of the environment setup; the result goes into `outcome`."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        try:
            await basharena.run_environment_setup(task)
            outcome["passed"] = True
        except Exception as exc:  # any failure of the setup means the task is skipped
            outcome["error"] = f"{type(exc).__name__}: {exc}"
        return state

    return solve


def _stack_error(log: Any, outcome: dict) -> str | None:
    """None when the setup passed in the stack, else why not."""
    if outcome.get("passed"):
        return None
    if "error" in outcome:
        return outcome["error"]
    if log.error is not None:
        return f"stack failed: {log.error.message}"
    details = []
    for sample in log.samples or []:
        if sample.error is not None:
            details.append(f"sample error: {sample.error.message}")
        if sample.limit is not None:
            details.append(f"sample limit: {sample.limit.type}")
    # No score and no setup result: for example the sample hit its time limit before the setup ended.
    text = f"no result: the sample produced no score (status {log.status}), for example it hit the time limit"
    return text + (": " + "; ".join(details) if details else "")


def check_task_in_stack(task_id: str, workspaces_dir: Path, run_id: str, meltzer: str, gateway: str) -> str | None:
    """Build, start and set up one task's stack under Inspect. None when it passes, else the error."""

    def patch(text: str) -> str:
        return yaml.safe_dump(image.patch_compose(yaml.safe_load(text), gateway), sort_keys=False)

    try:
        task = basharena.load_task(task_id)
        compose_path, _ = basharena.write_task_workspace(
            task, workspaces_dir, base_image=meltzer, run_id=run_id, compose_patch=patch
        )
    except Exception as exc:
        return f"workspace: {type(exc).__name__}: {exc}"

    outcome: dict = {}
    inspect_task = Task(
        dataset=[Sample(input="preflight", id=task_id, sandbox=("docker", str(compose_path)))],
        solver=_setup_only(task, outcome),
        time_limit=SAMPLE_TIME_LIMIT_SECONDS,
    )
    log_dir = tempfile.mkdtemp(prefix="preflight-logs-")
    try:
        logs = eval(
            inspect_task,
            model="mockllm/model",
            log_dir=log_dir,
            max_samples=1,
            display="none",
            fail_on_error=False,
        )
    except Exception as exc:
        return f"stack: {type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(log_dir, ignore_errors=True)
    return _stack_error(logs[0], outcome)


def preflight(cfg: Any, run_dir: Path, *, check_task: TaskCheck | None = None) -> list[str]:
    """Run the pre-flight for a run config; write `<run_dir>/preflight.json`; return the kept ids.

    `check_task` replaces the Docker check of one task (used by the unit tests).
    """
    run_dir = Path(run_dir)
    main_tasks = cfg.setting.main_tasks
    order = basharena.shuffled_pool(main_tasks.seed)
    workspaces_dir = run_dir / "workspaces"
    run_batch = sequential_batch
    try:
        if check_task is None:
            meltzer, gateway = image.ensure_images()
            check_task = functools.partial(
                check_task_in_stack, workspaces_dir=workspaces_dir, run_id=cfg.run_id, meltzer=meltzer, gateway=gateway
            )
            run_batch = process_batch(preflight_workers(cfg))
        kept, skipped = walk_pool(order, main_tasks.n, check_task, run_batch)
    finally:
        shutil.rmtree(workspaces_dir, ignore_errors=True)

    if len(kept) < main_tasks.n:
        print(f"preflight: only {len(kept)} of {main_tasks.n} tasks passed", file=sys.stderr)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "preflight.json").write_text(
        json.dumps({"kept": kept, "skipped": skipped}, indent=2) + "\n", encoding="utf-8"
    )
    return kept
