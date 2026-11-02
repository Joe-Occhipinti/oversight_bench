"""Resuming an Inspect eval set after a kill, the mechanism behind `python -m bench resume` (spec 7.8).

A worker process (tests/resume_worker.py) runs an eval set of 8 samples built in code, like bench.task builds its
task. The test kills it after some samples have finished, then runs it again in the same log directory. What
`bench resume` relies on (inspect_ai 0.3.287):
- samples that finished are kept and not run again; the ones in progress at the kill start again;
- the number of parallel samples may differ between the two runs;
- a change to the task (here, a solver argument) is refused, and the old log is left as it was.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
from inspect_ai.log import read_eval_log, read_eval_log_samples

WORKER = Path(__file__).with_name("resume_worker.py")
SAMPLES = 8


def events(work: Path) -> list[tuple[int, str, str]]:
    path = work / "events.txt"
    if not path.is_file():
        return []
    return [(int(pid), kind, sample) for pid, kind, sample in (line.split() for line in path.read_text().splitlines())]


def run_worker(work: Path, max_samples: int, seconds: float, exit_one: str = "none") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(WORKER), str(work), str(max_samples), str(seconds), exit_one],
        capture_output=True, text=True, timeout=300,
    )


def killed_after_samples_finished(work: Path, finished: int, max_samples: int = 3, seconds: float = 3.0) -> set[str]:
    """Start the worker, kill it once `finished` samples have ended; return the ids of the samples that had ended."""
    process = subprocess.Popen(
        [sys.executable, str(WORKER), str(work), str(max_samples), str(seconds)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 120
        while sum(kind == "end" for _, kind, _ in events(work)) < finished:
            assert time.time() < deadline and process.poll() is None, "the worker did not get far enough"
            time.sleep(0.2)
        process.kill()
    finally:
        process.kill()
        process.wait()
    return {sample for _, kind, sample in events(work) if kind == "end"}


def logs(work: Path) -> dict[str, tuple[str, list[str]]]:
    """Log file name -> (status, sample ids)."""
    out = {}
    for path in sorted((work / "inspect_logs").glob("*.eval")):
        ids = sorted(str(s.id) for s in read_eval_log_samples(str(path), all_samples_required=False))
        out[path.name] = (read_eval_log(str(path), header_only=True).status, ids)
    return out


def test_finished_samples_are_kept_and_the_others_run_again_with_fewer_parallel_samples(tmp_path):
    ended = killed_after_samples_finished(tmp_path, finished=5)
    assert 5 <= len(ended) < SAMPLES
    first = {pid for pid, _, _ in events(tmp_path)}

    second = run_worker(tmp_path, max_samples=2, seconds=3.0)  # fewer parallel samples than the first start
    assert second.returncode == 0, second.stderr[-800:]

    rerun = {sample for pid, kind, sample in events(tmp_path) if pid not in first and kind == "start"}
    assert rerun and rerun.isdisjoint(ended)  # no finished sample ran again
    assert rerun | ended == {f"s{i}" for i in range(SAMPLES)}  # and every other one did
    statuses = logs(tmp_path)
    assert sorted(status for status, _ in statuses.values()) == ["started", "success"]  # the killed log is kept
    final = [ids for status, ids in statuses.values() if status == "success"]
    assert final == [[f"s{i}" for i in range(SAMPLES)]]  # all samples, none twice


def test_a_resume_with_a_changed_task_is_refused_and_leaves_the_logs(tmp_path):
    killed_after_samples_finished(tmp_path, finished=4)
    before = logs(tmp_path)
    second = run_worker(tmp_path, max_samples=3, seconds=2.5)  # a solver argument differs
    assert second.returncode != 0
    assert "not associated with a task" in second.stdout + second.stderr
    assert logs(tmp_path) == before


# ---------------------------------------------------------------- samples lost to a provider outage


PROVIDER_LINE = "meltzer: request to model provider failed: timed out"


def write_sample_folders(run_dir: Path, stderr_by_sample: dict[str, str]) -> None:
    for sample, text in stderr_by_sample.items():
        folder = run_dir / "samples" / sample
        folder.mkdir(parents=True)
        (folder / "stderr.txt").write_text(text, encoding="utf-8")


def test_provider_failures_are_marked_and_only_they_run_again(tmp_path):
    from bench import redo

    # run 1 finishes all 8 samples; the store of s2, s5 and s7 says meltzer exited with code 1
    first = run_worker(tmp_path, max_samples=3, seconds=0.3, exit_one="s2,s5,s7")
    assert first.returncode == 0, first.stderr[-800:]
    write_sample_folders(
        tmp_path,
        {f"s{i}": "all fine" for i in range(SAMPLES)}
        | {"s2": PROVIDER_LINE, "s5": "log\n" + PROVIDER_LINE, "s6": PROVIDER_LINE, "s7": "meltzer: a different failure"},
    )

    # s2 and s5: exit 1 and the provider line. s6: the line but exit 0. s7: exit 1 but another failure.
    assert redo.mark_for_redo(tmp_path) == ["s2", "s5"]
    (name, (status, _)), = logs(tmp_path).items()
    assert status == "error"
    log = read_eval_log(str(tmp_path / "inspect_logs" / name))
    assert sorted(str(s.id) for s in log.samples if s.error) == ["s2", "s5"]
    first_pids = {pid for pid, _, _ in events(tmp_path)}

    second = run_worker(tmp_path, max_samples=3, seconds=0.3)  # the provider is back: nothing records exit code 1
    assert second.returncode == 0, second.stderr[-800:]
    rerun = sorted(sample for pid, kind, sample in events(tmp_path) if pid not in first_pids and kind == "start")
    assert rerun == ["s2", "s5"]  # and nothing else
    (name, (status, ids)), = logs(tmp_path).items()
    assert status == "success" and ids == [f"s{i}" for i in range(SAMPLES)]
    final = read_eval_log(str(tmp_path / "inspect_logs" / name))
    assert not [s for s in final.samples if s.error]
    assert redo.mark_for_redo(tmp_path) == []  # nothing is left to mark


def test_an_unfinished_log_is_left_to_resume_and_not_touched(tmp_path):
    from bench import redo

    killed_after_samples_finished(tmp_path, finished=4)
    before = logs(tmp_path)
    write_sample_folders(tmp_path, {f"s{i}": PROVIDER_LINE for i in range(SAMPLES)})
    assert redo.mark_for_redo(tmp_path) == []
    assert logs(tmp_path) == before

