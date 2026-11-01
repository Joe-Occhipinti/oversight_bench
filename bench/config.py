"""Run config (spec section 12) and results folder helpers (section 10.1).

`load_config(path)` reads a run YAML, validates every field and returns a frozen `RunConfig`.
Field names are the YAML key names. Lists become tuples. A `ConfigError` names the field
(dotted path, e.g. `models.agent.reasoning_effort`).

Results helpers: `create_run_dir` makes `results/<run_id>/`, copies the config to `config.yaml` and
writes `manifest.json`; `finish_manifest` fills the end time and the main task ids later.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.metadata
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_ROOT = REPO_ROOT / "results"

SIDE_TASKS = ("download_malware", "leak_passwords", "disable_firewall", "sudo_edit")
CONDITIONS = ("unaware", "aware")
# Agent and monitor efforts. "max" and "xhigh" are rejected: Inspect maps "max" to OpenRouter "xhigh" (spec 7.2).
AGENT_MONITOR_EFFORTS = ("none", "minimal", "low", "medium", "high")
MAX_MAIN_TASKS = 100
TOP_LEVEL_KEYS = (
    "run_id", "setting", "conditions", "prompts", "models", "meltzer", "limits", "parallel", "seeds",
)
IMAGE_REPO = "oversight-bench/basharena-meltzer"


class ConfigError(ValueError):
    """Invalid run config. The message names the field."""


# ---------------------------------------------------------------- dataclasses (mirror the YAML)


@dataclass(frozen=True)
class MainTasks:
    pool: str
    n: int
    seed: int


@dataclass(frozen=True)
class Setting:
    name: str
    side_tasks: tuple[str, ...]
    main_tasks: MainTasks


@dataclass(frozen=True)
class Prompts:
    attack_base: str
    aware_section: str
    cards: str


@dataclass(frozen=True)
class Provider:
    order: tuple[str, ...]
    allow_fallbacks: bool


@dataclass(frozen=True)
class ModelSpec:
    id: str
    reasoning_effort: str | None
    provider: Provider


@dataclass(frozen=True)
class JudgeSpec(ModelSpec):
    temperature: float
    concurrency: int


@dataclass(frozen=True)
class Models:
    agent: ModelSpec
    monitor: ModelSpec
    judge: JudgeSpec


@dataclass(frozen=True)
class Meltzer:
    max_turn_steps: int


@dataclass(frozen=True)
class Limits:
    meltzer_seconds: int
    sample_time_limit_seconds: int
    wait_seconds: int


@dataclass(frozen=True)
class Parallel:
    max_samples: int


@dataclass(frozen=True)
class Seeds:
    inspect_shuffle: int
    bootstrap: int


@dataclass(frozen=True)
class RunConfig:
    run_id: str
    setting: Setting
    conditions: tuple[str, ...]
    prompts: Prompts
    models: Models
    meltzer: Meltzer
    limits: Limits
    parallel: Parallel
    seeds: Seeds


def to_plain(obj: Any) -> Any:
    """Dataclass (or tuple/list) to JSON-ready dicts and lists, e.g. `to_plain(cfg.models.judge)`."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [to_plain(x) for x in obj]
    return obj


# ---------------------------------------------------------------- validation helpers


def _join(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


def _field(parent: dict, key: str, prefix: str) -> Any:
    if key not in parent:
        raise ConfigError(f"{_join(prefix, key)}: required field is missing")
    return parent[key]


def _closed(mapping: dict, allowed: Sequence[str], prefix: str) -> None:
    """The schema is closed: a key that is not a field is an error."""
    for key in mapping:
        if key not in allowed:
            raise ConfigError(f"{_join(prefix, str(key))}: unknown field; allowed: {', '.join(allowed)}")


def _section(parent: dict, key: str, prefix: str, allowed: Sequence[str]) -> dict:
    name = _join(prefix, key)
    value = _field(parent, key, prefix)
    if not isinstance(value, dict):
        raise ConfigError(f"{name}: must be a mapping, got {value!r}")
    _closed(value, allowed, name)
    return value


def _str(parent: dict, key: str, prefix: str) -> str:
    name = _join(prefix, key)
    value = _field(parent, key, prefix)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name}: must be a non-empty string, got {value!r}")
    return value


def _const(parent: dict, key: str, prefix: str, expected: str) -> str:
    value = _str(parent, key, prefix)
    if value != expected:
        raise ConfigError(f"{_join(prefix, key)}: must be {expected!r}, got {value!r}")
    return value


def _int(parent: dict, key: str, prefix: str, minimum: int | None = None, maximum: int | None = None) -> int:
    name = _join(prefix, key)
    value = _field(parent, key, prefix)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name}: must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name}: must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name}: must be <= {maximum}, got {value}")
    return value


def _number(parent: dict, key: str, prefix: str) -> float:
    name = _join(prefix, key)
    value = _field(parent, key, prefix)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name}: must be a number, got {value!r}")
    return value


def _bool(parent: dict, key: str, prefix: str) -> bool:
    name = _join(prefix, key)
    value = _field(parent, key, prefix)
    if not isinstance(value, bool):
        raise ConfigError(f"{name}: must be true or false, got {value!r}")
    return value


def _str_list(parent: dict, key: str, prefix: str, allowed: Sequence[str] | None = None) -> tuple[str, ...]:
    """Non-empty list of distinct strings, optionally restricted to `allowed`."""
    name = _join(prefix, key)
    value = _field(parent, key, prefix)
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{name}: must be a non-empty list, got {value!r}")
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{name}: entries must be non-empty strings, got {item!r}")
    if len(set(value)) != len(value):
        raise ConfigError(f"{name}: duplicate entries in {value!r}")
    if allowed is not None:
        bad = [item for item in value if item not in allowed]
        if bad:
            raise ConfigError(f"{name}: unknown {', '.join(map(repr, bad))}; allowed: {', '.join(allowed)}")
    return tuple(value)


def _effort(parent: dict, prefix: str, restricted: bool) -> str | None:
    name = _join(prefix, "reasoning_effort")
    value = _field(parent, "reasoning_effort", prefix)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{name}: must be null or a string, got {value!r}")
    if restricted and value not in AGENT_MONITOR_EFFORTS:
        hint = ""
        if value in ("max", "xhigh"):
            hint = " (Inspect maps 'max' to OpenRouter 'xhigh', which GLM models do not support; see spec 7.2)"
        raise ConfigError(
            f"{name}: {value!r} is not allowed{hint}; use null or one of {', '.join(AGENT_MONITOR_EFFORTS)}"
        )
    return value


def _provider(parent: dict, prefix: str) -> Provider:
    section = _section(parent, "provider", prefix, ("order", "allow_fallbacks"))
    p = _join(prefix, "provider")
    return Provider(order=_str_list(section, "order", p), allow_fallbacks=_bool(section, "allow_fallbacks", p))


def _model(parent: dict, key: str, restricted: bool, extra: Sequence[str] = ()) -> tuple[dict, dict, str]:
    """Fields shared by agent, monitor and judge: (ModelSpec kwargs, the section, its dotted name)."""
    section = _section(parent, key, "models", ("id", "reasoning_effort", "provider", *extra))
    prefix = f"models.{key}"
    kwargs = dict(
        id=_str(section, "id", prefix),
        reasoning_effort=_effort(section, prefix, restricted),
        provider=_provider(section, prefix),
    )
    return kwargs, section, prefix


# ---------------------------------------------------------------- load_config


def load_config(path: str | Path) -> RunConfig:
    """Read and validate a run YAML. Raises ConfigError naming the offending field."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"{path}: cannot read config: {e}") from e
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    try:
        return _build(raw)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from None


def _build(raw: dict) -> RunConfig:
    run_id = _str(raw, "run_id", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id):
        raise ConfigError(f"run_id: must be a plain folder name (letters, digits, . _ -), got {run_id!r}")

    _closed(raw, TOP_LEVEL_KEYS, "")
    s = _section(raw, "setting", "", ("name", "side_tasks", "main_tasks"))
    mt = _section(s, "main_tasks", "setting", ("pool", "n", "seed"))
    setting = Setting(
        name=_const(s, "name", "setting", "basharena"),
        side_tasks=_str_list(s, "side_tasks", "setting", allowed=SIDE_TASKS),
        main_tasks=MainTasks(
            pool=_const(mt, "pool", "setting.main_tasks", "no_cover"),
            n=_int(mt, "n", "setting.main_tasks", minimum=1, maximum=MAX_MAIN_TASKS),
            seed=_int(mt, "seed", "setting.main_tasks"),
        ),
    )

    conditions = _str_list(raw, "conditions", "", allowed=CONDITIONS)

    p = _section(raw, "prompts", "", ("attack_base", "aware_section", "cards"))
    prompts = Prompts(
        attack_base=_str(p, "attack_base", "prompts"),
        aware_section=_str(p, "aware_section", "prompts"),
        cards=_str(p, "cards", "prompts"),
    )

    m = _section(raw, "models", "", ("agent", "monitor", "judge"))
    agent, _, _ = _model(m, "agent", restricted=True)
    monitor, _, _ = _model(m, "monitor", restricted=True)
    judge, jsec, jprefix = _model(m, "judge", restricted=False, extra=("temperature", "concurrency"))
    models = Models(
        agent=ModelSpec(**agent),
        monitor=ModelSpec(**monitor),
        judge=JudgeSpec(
            **judge,
            temperature=_number(jsec, "temperature", jprefix),
            concurrency=_int(jsec, "concurrency", jprefix, minimum=1),
        ),
    )

    mel = _section(raw, "meltzer", "", ("max_turn_steps",))
    lim = _section(raw, "limits", "", ("meltzer_seconds", "sample_time_limit_seconds", "wait_seconds"))
    par = _section(raw, "parallel", "", ("max_samples",))
    seeds = _section(raw, "seeds", "", ("inspect_shuffle", "bootstrap"))
    return RunConfig(
        run_id=run_id,
        setting=setting,
        conditions=conditions,
        prompts=prompts,
        models=models,
        meltzer=Meltzer(max_turn_steps=_int(mel, "max_turn_steps", "meltzer", minimum=1)),
        limits=Limits(
            meltzer_seconds=_int(lim, "meltzer_seconds", "limits", minimum=1),
            sample_time_limit_seconds=_int(lim, "sample_time_limit_seconds", "limits", minimum=1),
            wait_seconds=_int(lim, "wait_seconds", "limits", minimum=0),
        ),
        parallel=Parallel(max_samples=_int(par, "max_samples", "parallel", minimum=1)),
        seeds=Seeds(
            inspect_shuffle=_int(seeds, "inspect_shuffle", "seeds"),
            bootstrap=_int(seeds, "bootstrap", "seeds"),
        ),
    )


# ---------------------------------------------------------------- meltzer tree hash and image tag


def meltzer_tree_hash(root: Path | None = None) -> str:
    """sha256 (hex) over the sorted relative paths and contents of files under `meltzer/v0.1`.

    Files inside `__pycache__` folders are skipped. Each file contributes
    `<posix relative path> NUL <bytes> NUL`.
    """
    root = Path(root) if root is not None else REPO_ROOT / "meltzer" / "v0.1"
    if not root.is_dir():
        raise FileNotFoundError(f"meltzer tree not found: {root}")
    files = sorted(
        (p.relative_to(root).as_posix(), p)
        for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.relative_to(root).parts
    )
    h = hashlib.sha256()
    for rel, p in files:
        h.update(rel.encode("utf-8") + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def image_tag(tree_hash: str | None = None) -> str:
    """The meltzer image name, as built by `bench.image.meltzer_image_tag` (spec 4.1)."""
    from bench.image import meltzer_image_tag  # lazy: bench.image imports this module

    return meltzer_image_tag(tree_hash)


# ---------------------------------------------------------------- results folder and manifest


def resolve_run_dir(run_id: str, results_root: Path | None = None) -> Path:
    """`results/<run_id>/` (not checked for existence)."""
    return Path(results_root if results_root is not None else RESULTS_ROOT) / run_id


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", *args], cwd=cwd or REPO_ROOT, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None


def git_commit(cwd: Path | None = None) -> str:
    """HEAD commit of oversight_bench, or "unknown"."""
    r = _git("rev-parse", "HEAD", cwd=cwd)
    if r is None or r.returncode != 0 or not r.stdout.strip():
        return "unknown"
    return r.stdout.strip()


def git_dirty(cwd: Path | None = None) -> bool | None:
    """True if `git status --porcelain` is non-empty (untracked files count); None if git fails."""
    r = _git("status", "--porcelain", cwd=cwd)
    if r is None or r.returncode != 0:
        return None
    return bool(r.stdout.strip())


def basharena_commit(path: Path | None = None) -> str | None:
    """Commit recorded in `vendor/basharena/COMMIT`, or None if the file is absent."""
    path = Path(path) if path is not None else REPO_ROOT / "vendor" / "basharena" / "COMMIT"
    return path.read_text(encoding="utf-8").strip() if path.is_file() else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


PRE_RUN_ENTRIES = ("preflight.json", "prompts")  # written by `preflight` and `render-prompts` before a run


def check_run_dir_free(run_dir: Path) -> None:
    """Raise FileExistsError if `run_dir` exists, unless it is empty or holds only `preflight.json`
    and/or `prompts/` (written by the preflight and render-prompts commands before the run)."""
    if run_dir.exists() and (not run_dir.is_dir() or any(p.name not in PRE_RUN_ENTRIES for p in run_dir.iterdir())):
        raise FileExistsError(f"{run_dir} already exists; refusing to overwrite (use a new run_id)")


def create_run_dir(cfg: RunConfig, config_path: str | Path, results_root: Path | None = None) -> Path:
    """Create `results/<run_id>/`, copy the config to `config.yaml`, write `manifest.json`.

    Raises FileExistsError if the folder exists, unless it is empty or holds only `preflight.json` and/or
    `prompts/` (the preflight and render-prompts commands write them before the run starts; see 5.2, 9.2).
    """
    run_dir = resolve_run_dir(cfg.run_id, results_root)
    check_run_dir_free(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(config_path, run_dir / "config.yaml")
    tree_hash = meltzer_tree_hash()
    _write_json(
        run_dir / "manifest.json",
        {
            "run_id": cfg.run_id,
            "oversight_bench_commit": git_commit(),
            "oversight_bench_dirty": git_dirty(),
            "meltzer_tree_hash": tree_hash,
            "image_tag": image_tag(tree_hash),
            "basharena_commit": basharena_commit(),
            "inspect_ai_version": _inspect_ai_version(),
            "seeds": {
                "main_tasks": cfg.setting.main_tasks.seed,
                "inspect_shuffle": cfg.seeds.inspect_shuffle,
                "bootstrap": cfg.seeds.bootstrap,
            },
            "start_time": _now(),
            "end_time": None,
            "main_task_ids": None,
        },
    )
    return run_dir


def _inspect_ai_version() -> str:
    try:
        return importlib.metadata.version("inspect_ai")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def finish_manifest(run_dir: Path, main_task_ids: Sequence[str] | None = None) -> None:
    """Set `end_time` to now and, if given, `main_task_ids`. Other fields are kept."""
    path = Path(run_dir) / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["end_time"] = _now()
    if main_task_ids is not None:
        manifest["main_task_ids"] = list(main_task_ids)
    _write_json(path, manifest)
