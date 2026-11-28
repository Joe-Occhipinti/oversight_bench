"""Execution samples to run again after an outage of the model provider (exp1 spec 7.7).

When the internet or the provider is down, meltzer's model requests time out. meltzer retries twice, then exits
with code 1 and the line `meltzer: request to model provider failed: ...` on stderr. Inspect sees a sample that
finished, so `resume` keeps it. These samples say nothing about the attack: the agent could not think.

`mark_for_redo` finds them in the finished logs of a run and marks them as errored, and the log as `error`.
Inspect then runs again exactly the errored samples when the run is resumed (checked in tests/test_resume.py).
Only logs with status `success` are changed; an unfinished log is continued by `resume` as it is.

Only the execution and ceiling runs are redone: a search experiment that failed is not run again, its board entry is
empty (docs/spec-exp3.md 5.1). A sample's folder is its metadata `dir`, relative to the run folder.
"""

from __future__ import annotations

from pathlib import Path

from inspect_ai.log import EvalError, read_eval_log, write_eval_log

from bench.solver import SAMPLE_DIR_KEY, STORE_KEY

# What meltzer prints last on stderr when its model requests fail for the third time (`client.py`, `cli.py`).
PROVIDER_FAILURE = "request to model provider failed"


def is_provider_failure(sample_dir: Path, exit_code: object) -> bool:
    """True for a sample whose meltzer exited with code 1 and the provider-failure line on stderr."""
    if exit_code != 1:
        return False
    try:
        return PROVIDER_FAILURE in (sample_dir / "stderr.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


EXECUTION_LOGS = "inspect_logs/execution"


def mark_for_redo(run_dir: Path, log_dir: Path | None = None) -> list[str]:
    """Mark the provider failures in the finished logs of `log_dir` (default: the run's execution logs); return
    their sample folders (sorted)."""
    run_dir = Path(run_dir)
    log_dir = Path(log_dir) if log_dir is not None else run_dir / EXECUTION_LOGS
    marked: list[str] = []
    for path in sorted(log_dir.glob("*.eval")):
        log = read_eval_log(str(path))
        if log.status != "success":
            continue
        hits = [
            sample
            for sample in log.samples or []
            if sample.error is None
            and is_provider_failure(
                run_dir / sample.metadata[SAMPLE_DIR_KEY], ((sample.store or {}).get(STORE_KEY) or {}).get("exit_code")
            )
        ]
        if not hits:
            continue
        for sample in hits:
            sample.error = EvalError(
                message="the model provider was unreachable: meltzer's model requests failed; the sample is run again",
                traceback="",
                traceback_ansi="",
            )
        log.status = "error"
        write_eval_log(log, str(path))
        marked += [sample.metadata[SAMPLE_DIR_KEY] for sample in hits]
    return sorted(marked)
