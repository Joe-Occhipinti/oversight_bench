"""CLI: `python -m bench <command>`.

Commands:
  run <config>         create results/<run_id>/ and run the experiment
  resume <run_id>      continue an interrupted run from results/<run_id>/ (finished samples are kept)
  preflight <config>   check the main-task pool (writes results/<run_id>/preflight.json)
  render-prompts <config>
                       write the rendered prompts of one sample per condition to
                       results/<run_id>/prompts/ (review gate, spec 9.2)
  judge <run_id>       label the successful samples of a finished run
  analyze <run_id>     compute ASR, intervals, tests, tables and plots
  summary <dirs...>    print one row per results folder

`<run_id>` resolves to results/<run_id>/ and reads its config.yaml. `<config>` is a path to a run YAML.
Commands that depend on other modules import them when called; if a module is missing the command
exits with code 2 and "not implemented yet". Other errors exit with code 1.

Calls into other modules (the contract those modules must match):
  bench.task.run_experiment(cfg: RunConfig, run_dir: Path) -> list[str] | None   main task ids used
  bench.preflight.preflight(cfg: RunConfig, run_dir: Path) -> list[str]
  bench.judge.judge_run(run_dir: Path, judge: dict, cards_dir: Path)   (called with keywords)
  bench.analysis.analyze_run(run_dir: Path, bootstrap_seed: int)       (called with a keyword)
  render-prompts: bench.prompts.write_review_prompts(out_dir, {condition: ReviewSample}) with
    ReviewSample(task_description, setting_description, side_task_description, attack_base_path,
    aware_section_path, cards_dir, card_id); the texts come from bench.basharena: load_task(id).description,
    setting_description(), SIDE_TASKS[name].description, with id = shuffled_pool(seed)[0] and name =
    the first side task of the config (the same sample in every condition). card_id is the card assigned to
    that pair in aware mode one_card, else None.

The manifest's main_task_ids come from run_experiment's return value; if it returns None they are read from
results/<run_id>/preflight.json, key "kept" (list of task ids), when that file exists.

`summary` reads, in each folder, `summary.json` as written by bench.analysis (any key may be missing,
shown as "-"):
  {"run_id": str,
   "models": {"agent": str, "monitor": str},
   "n_samples": int,
   "metrics": {"asr_unaware" | "asr_aware_strict" | "asr_aware_scorer":
                 {"average": {"value": float, "ci": [low, high]}, ...per side task...}},
   "gaps": {"asr_aware_strict" | "asr_aware_scorer": {"average": {"value": float, "ci": [low, high]}, ...}}}
Model ids missing from summary.json are read from the folder's config.yaml.
"""

from __future__ import annotations

import argparse
import glob
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from bench import assignment, config
from bench.config import ConfigError, RunConfig

SUMMARY_COLUMNS = ["run", "agent", "monitor", "aware_mode", "n", "asr_unaware", "asr_aware_strict", "asr_aware_scorer", "gap_strict", "gap_scorer"]


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


# ---------------------------------------------------------------- run, preflight, judge, analyze


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
    config.note_resume(run_dir, args.max_samples)
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
        sample = ReviewSample(
            task_description=load_task(task_id).description,
            setting_description=setting_description(),
            side_task_description=SIDE_TASKS[side_task].description,
            attack_base_path=root / cfg.prompts.attack_base,
            aware_section_path=root / cfg.prompts.aware_section_file,
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


def cmd_analyze(args: argparse.Namespace) -> int:
    cfg, run_dir = _load_run(args.run_id)
    try:
        from bench.analysis import analyze_run
    except ImportError as e:
        raise _import_error("analyze (bench.analysis.analyze_run)", e) from None
    analyze_run(run_dir, bootstrap_seed=cfg.seeds.bootstrap)
    return 0


# ---------------------------------------------------------------- summary (11.4)


def _dig(data: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _fmt(entry: Any, signed: bool = False) -> str:
    """`0.25 [0.20, 0.30]` from {value, ci: [low, high]}; "-" if the value is missing."""
    value = _dig(entry, "value")
    if not _is_num(value):
        return "-"
    f = "{:+.2f}" if signed else "{:.2f}"
    text = f.format(value)
    ci = _dig(entry, "ci")
    if isinstance(ci, (list, tuple)) and len(ci) == 2 and all(_is_num(x) for x in ci):
        text += f" [{f.format(ci[0])}, {f.format(ci[1])}]"
    return text


def _model_id(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("id")
    return value if isinstance(value, str) and value else None


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"warning: cannot read {path}: {e}", file=sys.stderr)
        return {}


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}


def _summary_row(folder: Path) -> list[str]:
    summary = _read_json(folder / "summary.json")
    cfg = _read_yaml(folder / "config.yaml")

    def model(role: str) -> str:
        return _model_id(_dig(summary, "models", role)) or _model_id(_dig(cfg, "models", role)) or "-"

    run_id = _dig(summary, "run_id")
    n = _dig(summary, "n_samples")
    return [
        run_id if isinstance(run_id, str) and run_id else folder.name,
        model("agent"),
        model("monitor"),
        _dig(cfg, "prompts", "aware_mode") if isinstance(_dig(cfg, "prompts", "aware_mode"), str) else "-",
        str(n) if _is_num(n) else "-",
        *(_fmt(_dig(summary, "metrics", k, "average")) for k in ("asr_unaware", "asr_aware_strict", "asr_aware_scorer")),
        *(_fmt(_dig(summary, "gaps", k, "average"), signed=True) for k in ("asr_aware_strict", "asr_aware_scorer")),
    ]


def _expand(arg: str) -> list[str]:
    """An existing path stays as it is; otherwise treat it as a glob (PowerShell does not expand them)."""
    if Path(arg).exists():
        return [arg]
    return sorted(m for m in glob.glob(arg) if Path(m).is_dir()) or [arg]


def cmd_summary(args: argparse.Namespace) -> int:
    dirs = [d for arg in args.dirs for d in _expand(arg)]
    rows = [SUMMARY_COLUMNS] + [_summary_row(Path(d)) for d in dirs]
    widths = [max(len(r[i]) for r in rows) for i in range(len(SUMMARY_COLUMNS))]
    for row in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())
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
    p.set_defaults(func=cmd_resume)

    for name, func, help_ in (
        ("judge", cmd_judge, "label the successful samples of a run"),
        ("analyze", cmd_analyze, "compute metrics, tables and plots of a run"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("run_id", help="run id; reads results/<run_id>/config.yaml")
        p.set_defaults(func=func)

    p = sub.add_parser("render-prompts", help="write the rendered prompts for the review gate")
    p.add_argument("config", help="path to a run YAML, e.g. runs/exp1-01.yaml")
    p.set_defaults(func=cmd_render_prompts)

    p = sub.add_parser("check-registry", help="check that every card citation exists in meltzer/v0.1")
    p.set_defaults(func=cmd_check_registry)

    p = sub.add_parser("summary", help="print one row per results folder")
    p.add_argument("dirs", nargs="+", help="results folders; wildcards such as results/* are expanded")
    p.set_defaults(func=cmd_summary)
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
