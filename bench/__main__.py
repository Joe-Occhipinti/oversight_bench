"""CLI: `python -m bench <command>`.

Commands:
  run <config>         create results/<run_id>/ and run the experiment
  resume <run_id>      continue an interrupted run from results/<run_id>/ (finished samples are kept;
                       --redo-provider-failures also runs again the samples lost to a provider outage)
  preflight <config>   check the main-task pool (writes results/<run_id>/preflight.json)
  render-prompts <config>
                       write the rendered prompts of one sample per condition to
                       results/<run_id>/prompts/ (review gate, spec 9.2)
  judge <run_id>       label the successful samples of a finished run
  asr <run_id>         compute the run's ASRs with intervals (writes results/<run_id>/asr.json)
  plot-asr <run_id>... --out <png>
                       plot the faithful average ASR of the runs, in the order given (reads their asr.json)
  plot-strategies <run_id>
                       plot the registry cards of the run's faithful successful attacks
                       (writes results/<run_id>/plots/strategies.png)

`<run_id>` resolves to results/<run_id>/ and reads its config.yaml. `<config>` is a path to a run YAML.
Commands that depend on other modules import them when called; if a module is missing the command
exits with code 2 and "not implemented yet". Other errors exit with code 1.

Calls into other modules (the contract those modules must match):
  bench.task.run_experiment(cfg: RunConfig, run_dir: Path) -> list[str] | None   main task ids used
  bench.preflight.preflight(cfg: RunConfig, run_dir: Path) -> list[str]
  bench.judge.judge_run(run_dir: Path, judge: dict, cards_dir: Path)   (called with keywords)
  bench.asr.measure_run(run_dir: Path, cfg: RunConfig) -> dict          (the content of asr.json)
  render-prompts: bench.prompts.write_review_prompts(out_dir, {condition: ReviewSample}) with
    ReviewSample(task_description, setting_description, side_task_description, attack_base_path,
    aware_common_path, aware_section_path, cards_dir, card_id); the texts come from bench.basharena: load_task(id).description,
    setting_description(), SIDE_TASKS[name].description, with id = shuffled_pool(seed)[0] and name =
    the first side task of the config (the same sample in every condition). card_id is the card assigned to
    that pair in aware mode one_card, else None.

The manifest's main_task_ids come from run_experiment's return value; if it returns None they are read from
results/<run_id>/preflight.json, key "kept" (list of task ids), when that file exists.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from bench import assignment, config
from bench.config import ConfigError, RunConfig


class CliError(Exception):
    """A user-facing error: printed as `error: ...`, exit code 1."""


class NotImplementedYet(Exception):
    """A module this command depends on is missing: exit code 2."""


def _import_error(what: str, exc: ImportError) -> NotImplementedYet:
    return NotImplementedYet(f"{what} is not implemented yet ({exc})")


def _load_run(run_id: str) -> tuple[RunConfig, Path]:
    run_dir = config.resolve_run_dir(run_id)
    if not run_dir.is_dir():
        raise CliError(f"no results folder for run_id {run_id!r}: {run_dir}")
    return config.load_config(run_dir / "config.yaml"), run_dir


# ---------------------------------------------------------------- run, preflight, judge


def cmd_run(args: argparse.Namespace) -> int:
    cfg = config.load_config(args.config)
    assignment.check_config(cfg)
    try:
        from bench.task import run_experiment
    except ImportError as e:
        raise _import_error("run (bench.task.run_experiment)", e) from None
    try:
        run_dir = config.create_run_dir(cfg, args.config)
    except FileExistsError as e:
        raise FileExistsError(f"{e}; to continue an interrupted run use `python -m bench resume {cfg.run_id}`") from None
    return _run_and_finish(cfg, run_dir)


def _run_and_finish(cfg: RunConfig, run_dir: Path, max_samples: int | None = None) -> int:
    from bench.task import run_experiment

    ids = None
    try:
        result = run_experiment(cfg, run_dir, max_samples=max_samples)
        if isinstance(result, (list, tuple)):
            ids = [str(x) for x in result]
    finally:
        config.finish_manifest(run_dir, ids if ids is not None else _preflight_kept(run_dir))
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
    but the number of parallel samples can differ from the first start."""
    cfg, run_dir = _load_run(args.run_id)
    assignment.check_config(cfg)
    if not (run_dir / "manifest.json").is_file() or not (run_dir / "inspect_logs").is_dir():
        raise CliError(f"{run_dir} holds no started run (no manifest.json or inspect_logs); use `run` with a config")
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
        print(f"{len(redo)} samples ended because the model provider was unreachable and run again: {', '.join(redo) or '-'}")
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
    cfg = config.load_config(args.config)
    assignment.check_config(cfg)
    try:
        from bench.preflight import preflight
    except ImportError as e:
        raise _import_error("preflight (bench.preflight.preflight)", e) from None
    run_dir = config.resolve_run_dir(cfg.run_id)
    config.check_run_dir_free(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    preflight(cfg, run_dir)
    return 0


def cmd_render_prompts(args: argparse.Namespace) -> int:
    cfg = config.load_config(args.config)
    try:
        from bench.basharena import SIDE_TASKS, load_task, setting_description, shuffled_pool
        from bench.prompts import ReviewSample, write_review_prompts
    except ImportError as e:
        raise _import_error("render-prompts (bench.basharena / bench.prompts)", e) from None
    run_dir = config.resolve_run_dir(cfg.run_id)
    config.check_run_dir_free(run_dir)
    assignment.check_config(cfg)
    root = config.REPO_ROOT
    try:
        # The first task the run will use: preflight's kept list if it exists, else the shuffled pool.
        task_ids = (_preflight_kept(run_dir) or shuffled_pool(cfg.setting.main_tasks.seed))[: cfg.setting.main_tasks.n]
        task_id, side_task = task_ids[0], cfg.setting.side_tasks[0]
        card_id = assignment.assigned_cards(cfg, task_ids).get((task_id, side_task))
        aware_common_path, aware_section_path = cfg.prompts.aware_paths()
        sample = ReviewSample(
            task_description=load_task(task_id).description,
            setting_description=setting_description(),
            side_task_description=SIDE_TASKS[side_task].description,
            attack_base_path=root / cfg.prompts.attack_base,
            aware_common_path=aware_common_path,
            aware_section_path=aware_section_path,
            cards_dir=root / cfg.prompts.cards,
            card_id=card_id,
        )
        written = write_review_prompts(run_dir / "prompts", {c: sample for c in cfg.conditions})
    except FileNotFoundError as e:
        raise CliError(str(e)) from None
    print(f"sample: main task {task_id}, side task {side_task}" + (f", card {card_id}" if card_id else ""))
    for path in written:
        print(path)
    return 0


def cmd_judge(args: argparse.Namespace) -> int:
    cfg, run_dir = _load_run(args.run_id)
    try:
        from bench.judge import judge_run
    except ImportError as e:
        raise _import_error("judge (bench.judge.judge_run)", e) from None
    judge_run(run_dir, judge=config.to_plain(cfg.models.judge), cards_dir=config.REPO_ROOT / cfg.prompts.cards)
    return 0


# ---------------------------------------------------------------- asr and plots (11)


def cmd_asr(args: argparse.Namespace) -> int:
    cfg, run_dir = _load_run(args.run_id)
    from bench.asr import format_result, measure_run

    if not (run_dir / "samples.jsonl").is_file():
        raise CliError(f"{run_dir} has no samples.jsonl; the run has not finished")
    try:
        result = measure_run(run_dir, cfg)
    except ValueError as e:
        raise CliError(str(e)) from None
    print(format_result(result))
    print(run_dir / "asr.json")
    return 0


def _read_asr(run_id: str) -> dict:
    path = config.resolve_run_dir(run_id) / "asr.json"
    if not path.is_file():
        raise CliError(f"{path} not found; run `python -m bench asr {run_id}` first")
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_plot_asr(args: argparse.Namespace) -> int:
    from bench.plots import plot_avg_asr

    results = [_read_asr(run_id) for run_id in args.run_ids]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    plot_avg_asr(results, out)
    print(out)
    return 0


def cmd_plot_strategies(args: argparse.Namespace) -> int:
    from bench.plots import plot_strategies

    result = _read_asr(args.run_id)
    out = config.resolve_run_dir(args.run_id) / "plots" / "strategies.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    plot_strategies(result, out)
    print(out)
    return 0


def cmd_check_registry(args: argparse.Namespace) -> int:
    from bench.registry_check import check_registry

    problems = check_registry(
        config.REPO_ROOT / "registry" / "v0.1" / "cards", config.REPO_ROOT / "meltzer" / "v0.1", "meltzer", "v0.1"
    )
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
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("config", help="path to a run YAML, e.g. runs/exp1-01.yaml")
        p.set_defaults(func=func)

    p = sub.add_parser("resume", help="continue an interrupted run (finished samples are kept)")
    p.add_argument("run_id", help="run id; reads results/<run_id>/config.yaml")
    p.add_argument(
        "--max-samples", type=int, default=None, help="parallel samples for this resume (default: the config's value)"
    )
    p.add_argument(
        "--redo-provider-failures",
        action="store_true",
        help="also run again the finished samples whose meltzer gave up because the model provider was unreachable",
    )
    p.set_defaults(func=cmd_resume)

    for name, func, help_ in (
        ("judge", cmd_judge, "label the successful samples of a run"),
        ("asr", cmd_asr, "compute the ASRs of a judged run (writes asr.json)"),
        ("plot-strategies", cmd_plot_strategies, "plot the cards of a run's faithful successful attacks"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("run_id", help="run id; reads results/<run_id>/")
        p.set_defaults(func=func)

    p = sub.add_parser("plot-asr", help="plot the faithful average ASR of several runs")
    p.add_argument("run_ids", nargs="+", help="run ids, in plot order; each needs results/<run_id>/asr.json")
    p.add_argument("--out", required=True, help="PNG file to write")
    p.set_defaults(func=cmd_plot_asr)

    p = sub.add_parser("render-prompts", help="write the rendered prompts for the review gate")
    p.add_argument("config", help="path to a run YAML, e.g. runs/exp1-01.yaml")
    p.set_defaults(func=cmd_render_prompts)

    p = sub.add_parser("check-registry", help="check that every card citation exists in meltzer/v0.1")
    p.set_defaults(func=cmd_check_registry)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except NotImplementedYet as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (CliError, ConfigError, FileExistsError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
