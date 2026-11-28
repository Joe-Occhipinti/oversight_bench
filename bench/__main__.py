"""CLI: `python -m bench <command>`.

Commands:
  run <config>         create results/<run_id>/ and run the experiment (search, reflection, execution)
  resume <run_id>      continue an interrupted run from results/<run_id>/ (finished samples are kept;
                       --redo-provider-failures also runs again the execution samples lost to a provider outage)
  preflight <config>   check the main-task pool (writes results/<run_id>/preflight.json)
  render-prompts <config>
                       write the rendered prompts of one search pair in every role and tier to
                       results/<run_id>/prompts/
  metrics <run_id>     compute the outcome metrics of a run (writes results/<run_id>/summary.json)
  check-registry       check that every card citation exists in the meltzer tree

`<run_id>` resolves to results/<run_id>/ and reads its config.yaml. `<config>` is a path to a run YAML.
Errors exit with code 1.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from bench import config
from bench.config import ConfigError, RunConfig


class CliError(Exception):
    """A user-facing error: printed as `error: ...`, exit code 1."""


def _load_run(run_id: str) -> tuple[RunConfig, Path]:
    run_dir = config.resolve_run_dir(run_id)
    if not run_dir.is_dir():
        raise CliError(f"no results folder for run_id {run_id!r}: {run_dir}")
    return config.load_config(run_dir / "config.yaml"), run_dir


# ---------------------------------------------------------------- run, resume, preflight


def cmd_run(args: argparse.Namespace) -> int:
    cfg = config.load_config(args.config)
    try:
        run_dir = config.create_run_dir(cfg, args.config)
    except FileExistsError as e:
        raise FileExistsError(f"{e}; to continue an interrupted run use `python -m bench resume {cfg.run_id}`") from None
    return _run_and_finish(cfg, run_dir)


def _run_and_finish(cfg: RunConfig, run_dir: Path, max_samples: int | None = None) -> int:
    from bench.run import run_experiment

    ids = None
    try:
        ids = run_experiment(cfg, run_dir, max_samples=max_samples)
    finally:  # also when the run stops with an error (or Ctrl-C): then the status says `failed`
        config.finish_manifest(run_dir, ids, status="finished" if ids is not None else "failed")
    return 0


def _leftover_sandboxes() -> list[str]:
    """Names of Docker containers of earlier Inspect samples (a stopped run leaves its stacks running)."""
    try:
        out = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=60
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [name for name in out.split() if name.startswith("inspect-")]


def cmd_resume(args: argparse.Namespace) -> int:
    """Continue an interrupted run. The run is rebuilt from the config saved in its results folder, so nothing
    but the number of parallel samples can differ from the first start. A run that failed before its first eval
    set (no preflight.json, image build, a prompt file) is resumed the same way once the cause is fixed."""
    cfg, run_dir = _load_run(args.run_id)
    if not (run_dir / "manifest.json").is_file():
        raise CliError(f"{run_dir} holds no started run (no manifest.json); use `run` with a config")
    leftovers = _leftover_sandboxes()
    if leftovers:
        print(
            f"warning: {len(leftovers)} Docker containers of Inspect samples exist (a stopped run leaves its stacks "
            f"running); remove them first with `uv run inspect sandbox cleanup docker`, unless another run uses them",
            file=sys.stderr,
        )
    redo = None
    if args.redo_provider_failures:
        from bench.redo import mark_for_redo

        redo = mark_for_redo(run_dir)
        print(
            f"{len(redo)} execution samples ended because the model provider was unreachable and run again: "
            f"{', '.join(redo) or '-'}"
        )
    config.note_resume(run_dir, args.max_samples, redo)
    return _run_and_finish(cfg, run_dir, args.max_samples)


def _preflight_kept(run_dir: Path) -> list[str] | None:
    """The kept main task ids from `preflight.json` (key "kept"), or None if absent or unreadable."""
    try:
        kept = json.loads((run_dir / "preflight.json").read_text(encoding="utf-8")).get("kept")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(kept, list) or not all(isinstance(x, (str, int)) and not isinstance(x, bool) for x in kept):
        return None
    return [str(x) for x in kept]


def cmd_preflight(args: argparse.Namespace) -> int:
    from bench.preflight import preflight

    cfg = config.load_config(args.config)
    run_dir = config.resolve_run_dir(cfg.run_id)
    config.check_run_dir_free(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    preflight(cfg, run_dir)
    return 0


def cmd_render_prompts(args: argparse.Namespace) -> int:
    from bench.basharena import shuffled_pool
    from bench.run import write_example_prompts
    from bench.search import search_pairs

    cfg = config.load_config(args.config)
    run_dir = config.resolve_run_dir(cfg.run_id)
    config.check_run_dir_free(run_dir)
    main_tasks = cfg.setting.main_tasks
    try:
        # The search main tasks the run will use: preflight's kept list if it exists, else the shuffled pool.
        search_ids = (_preflight_kept(run_dir) or shuffled_pool(main_tasks.seed))[: main_tasks.search_n]
        n_pairs = cfg.search.waves * cfg.search.per_wave
        task_id, side_task = search_pairs(search_ids, cfg.setting.side_tasks, n_pairs, cfg.seeds.search_pairs)[0]
        written = write_example_prompts(cfg, run_dir / "prompts", task_id, side_task)
    except (FileNotFoundError, ValueError) as e:
        raise CliError(str(e)) from None
    print(f"pair: main task {task_id}, side task {side_task} (the first search pair)")
    for path in written:
        print(path)
    return 0


# ---------------------------------------------------------------- metrics, registry


def cmd_metrics(args: argparse.Namespace) -> int:
    from bench.metrics import SUMMARY, format_summary, measure_run

    cfg, run_dir = _load_run(args.run_id)
    if not (run_dir / "samples.jsonl").is_file():
        raise CliError(f"{run_dir} has no samples.jsonl; the run has not started")
    print(format_summary(measure_run(run_dir, cfg)))
    print(run_dir / SUMMARY)
    return 0


def cmd_check_registry(args: argparse.Namespace) -> int:
    from bench.registry_check import check_registry

    problems = check_registry(config.CARDS_DIR, config.MELTZER_DIR, "meltzer", config.MELTZER_VERSION)
    for p in problems:
        print(f"{p.card}: {p.message}")
    print(f"{len(problems)} problems")
    return 1 if problems else 0


# ---------------------------------------------------------------- entry point


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m bench", description="oversight_bench experiment CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, func, help_ in (
        ("run", cmd_run, "run the experiment described by a run config"),
        ("preflight", cmd_preflight, "check the main-task pool for a run config"),
        ("render-prompts", cmd_render_prompts, "write the rendered prompts of one search pair in every role and tier"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("config", help="path to a run YAML, e.g. runs/exp3-smoke.yaml")
        p.set_defaults(func=func)

    p = sub.add_parser("resume", help="continue an interrupted run (finished samples are kept)")
    p.add_argument("run_id", help="run id; reads results/<run_id>/config.yaml")
    p.add_argument(
        "--max-samples", type=int, default=None, help="parallel samples for this resume (default: the config's value)"
    )
    p.add_argument(
        "--redo-provider-failures",
        action="store_true",
        help="also run again the finished execution samples whose meltzer gave up because the model provider was "
        "unreachable",
    )
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("metrics", help="compute the outcome metrics of a run (writes summary.json)")
    p.add_argument("run_id", help="run id; reads results/<run_id>/")
    p.set_defaults(func=cmd_metrics)

    p = sub.add_parser("check-registry", help="check that every card citation exists in the meltzer tree")
    p.set_defaults(func=cmd_check_registry)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (CliError, ConfigError, FileExistsError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
