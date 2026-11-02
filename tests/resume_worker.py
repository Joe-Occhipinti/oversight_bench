"""Worker of tests/test_resume.py: an Inspect eval set of 8 slow samples, built in code like bench.task builds its task.

    python resume_worker.py <work dir> <max_samples> <seconds per sample> [<ids that record meltzer exit code 1>]

Each process appends `<pid> start|end <sample>` lines to <work dir>/events.txt and exits with the exit code of eval_set.
"""

import asyncio
import os
import sys
from pathlib import Path

from inspect_ai import Task, eval_set
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.scorer import Score, Target, accuracy, scorer
from inspect_ai.solver import Generate, TaskState, solver

work, max_samples, seconds = Path(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3])
exit_one = set(sys.argv[4].split(",")) - {"none"} if len(sys.argv) > 4 else set()


def note(text: str) -> None:
    with (work / "events.txt").open("a", encoding="utf-8") as f:
        f.write(f"{os.getpid()} {text}\n")


@solver
def slow(seconds: float):
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        note(f"start {state.sample_id}")
        await asyncio.sleep(seconds)
        # what bench.solver stores per sample; the run folder is <work dir>, the logs <work dir>/inspect_logs
        state.store.set("meltzer", {"exit_code": 1 if str(state.sample_id) in exit_one else 0})
        note(f"end {state.sample_id}")
        return state

    return solve


@scorer(metrics=[accuracy()])
def always_correct():
    async def score(state: TaskState, target: Target) -> Score:
        return Score(value="C")

    return score


task = Task(
    dataset=MemoryDataset([Sample(input="x", id=f"s{i}") for i in range(8)]),
    solver=slow(seconds),
    scorer=always_correct(),
    name="resume-probe",
)
success, _ = eval_set(
    task, log_dir=str(work / "inspect_logs"), model="mockllm/model", max_samples=max_samples, fail_on_error=False,
    retry_on_error=1, retry_attempts=1, retry_wait=1, display="none",
)
sys.exit(0 if success else 1)
