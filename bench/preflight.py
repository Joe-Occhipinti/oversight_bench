"""Pre-flight check of the main-task pool (spec 5.2).

`preflight(cfg, run_dir)` walks the pool in the order fixed by `main_tasks.seed`. For each task it
builds the task image `FROM` the meltzer image, starts the task's compose stack (with the gateway
service), runs steps 1-2 of the environment setup (5.4) and stops the stack. The first
`main_tasks.n` tasks that pass are kept. The result is written to `<run_dir>/preflight.json`:

    {"kept": [<task ids in order>], "skipped": [{"task_id": ..., "error": ...}]}

Each stack runs as one Inspect sample with Inspect's `mockllm` model (no API calls). The task
workspaces (Dockerfile and compose file) are written to `<run_dir>/workspaces/` and removed at the
end: a run refuses to start in a results folder that holds anything but `preflight.json` and
`prompts/` (`bench.config.check_run_dir_free`), and the run writes its own workspaces.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from collections.abc import Callable
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

# Checks one task; returns None when it passes, else the error text.
TaskCheck = Callable[[str], "str | None"]


def walk_pool(order: list[str], n: int, check_task: TaskCheck) -> tuple[list[str], list[dict]]:
    """Check tasks in `order` until `n` pass. Returns the kept ids and the skipped `{task_id, error}`."""
    kept: list[str] = []
    skipped: list[dict] = []
    for task_id in order:
        if len(kept) >= n:
            break
        error = check_task(task_id)
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
    try:
        if check_task is None:
            meltzer, gateway = image.ensure_images()

            def check_task_with_docker(task_id: str) -> str | None:
                return check_task_in_stack(task_id, workspaces_dir, cfg.run_id, meltzer, gateway)

            check_task = check_task_with_docker
        kept, skipped = walk_pool(order, main_tasks.n, check_task)
    finally:
        shutil.rmtree(workspaces_dir, ignore_errors=True)

    if len(kept) < main_tasks.n:
        print(f"preflight: only {len(kept)} of {main_tasks.n} tasks passed", file=sys.stderr)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "preflight.json").write_text(
        json.dumps({"kept": kept, "skipped": skipped}, indent=2) + "\n", encoding="utf-8"
    )
    return kept
