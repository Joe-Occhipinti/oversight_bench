"""Strategy search (docs/spec-exp3.md sections 3 and 5): search pairs, waves, lessons, board, reflection.

Per tier, `search.waves` waves of `search.per_wave` experiments. Each experiment is one meltzer session on one search
pair; its developer instructions are base.md + tiers/tier<k>.md + experiment.md, with the board of the earlier waves.
The experiments of a wave run side by side and do not see each other. The same pairs are used in every tier.

Lessons are the experiment's final reply (`stdout.txt`), the reply of its last turn: when automatic review stopped a
turn and steps were left, the solver resumed the session with the lessons request (`bench.solver.resume_after_breaker`).
Its board entry is the lessons, `(no lessons)` when the session ended without a final reply (step cap, a review stop
with no steps left, time limit, meltzer error), or empty when the session failed (the
sample errored in the solver or its setup, or the model provider was unreachable; it is not run again). Scoring
problems do not change the entry: the board depends on the session only. The board lists the entries in order of wave, then
experiment number, each under the header `## Entry <wave>.<i>`: lessons only, no pair and no scores.

After the last wave, the reflection (one model call, no tools, made directly to OpenRouter with the agent model's
provider and reasoning effort) reads base.md without own pair + tiers/tier<k>.md + reflection.md with the board and
writes the strategy.

Files, per tier k: `tier<k>/search/wave<w>/exp<i>/` (the solver's files, `lessons.md`, `scores.json`),
`tier<k>/search/board.md`, `tier<k>/strategy.md` and `tier<k>/reflection.json` (request and responses). All are
rebuilt from the finished logs, so a resumed run writes the same files; the reflection is skipped when
`strategy.md` exists.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
from inspect_ai import Task
from inspect_ai.dataset import Sample

from bench import openrouter, prompts
from bench.config import REPO_ROOT, ModelSpec, RunConfig, to_plain, write_text_atomic
from bench.redo import is_provider_failure
from bench.solver import ModelFactory
from bench.task import base_prompt, build_task, exclusion_reason, make_sample, sample_metadata

NO_LESSONS = "(no lessons)"
OUTPUT_TRIES = 3  # reflection calls until one returns text


# ---------------------------------------------------------------- pairs and waves


def search_pairs(main_tasks: Sequence[str], side_tasks: Sequence[str], n: int, seed: int) -> list[tuple[str, str]]:
    """`n` distinct (main task, side task) pairs, drawn with `seed`.

    Side tasks are balanced as far as possible (6 pairs over 4 side tasks: 2+2+1+1): they come in turns, in an
    order shuffled once. Each pair's main task is chosen at random among the main tasks used least so far that are
    not yet paired with that side task, so main tasks are spread over the pairs.
    """
    if n > len(main_tasks) * len(side_tasks):
        raise ValueError(f"{n} distinct pairs need more than {len(main_tasks)} main tasks x {len(side_tasks)} side tasks")
    rng = random.Random(seed)
    sides = list(side_tasks)
    rng.shuffle(sides)
    used: Counter[str] = Counter()
    pairs: list[tuple[str, str]] = []
    for i in range(n):
        side = sides[i % len(sides)]
        free = [m for m in main_tasks if (m, side) not in pairs]
        least = min(used[m] for m in free)
        main = rng.choice([m for m in free if used[m] == least])
        used[main] += 1
        pairs.append((main, side))
    return pairs


def wave_pairs(cfg: RunConfig, pairs: Sequence[tuple[str, str]], wave: int) -> list[tuple[int, tuple[str, str]]]:
    """(experiment number from 1, pair) of the experiments of `wave` (from 1)."""
    per = cfg.search.per_wave
    return list(enumerate(pairs[(wave - 1) * per : wave * per], start=1))


def experiment_dir(tier: int, wave: int, experiment: int) -> str:
    return f"tier{tier}/search/wave{wave}/exp{experiment}"


def search_samples(
    cfg: RunConfig, tier: int, wave: int, pairs: Sequence[tuple[str, str]], board: str, compose_paths: Mapping[str, Any]
) -> list[Sample]:
    """The experiments of one wave of one tier, with `board` (the entries of the earlier waves) in their prompt."""
    samples = []
    for i, (main_task, side_task) in wave_pairs(cfg, pairs, wave):
        instructions = prompts.render_instructions(
            REPO_ROOT / cfg.prompts.dir, tier, "experiment", base_prompt(cfg, side_task),
            board=board, step_cap=cfg.search.step_cap,
        )  # fmt: skip
        metadata = sample_metadata(
            phase="search", tier=tier, group=f"tier{tier}", main_task=main_task, side_task=side_task,
            sample_dir=experiment_dir(tier, wave, i), developer_instructions=instructions, wave=wave, experiment=i,
        )  # fmt: skip
        samples.append(make_sample(f"exp{i}__{main_task}__{side_task}", compose_paths[main_task], metadata))
    return samples


def search_tasks(
    cfg: RunConfig,
    run_dir: Path,
    wave: int,
    pairs: Sequence[tuple[str, str]],
    boards: Mapping[int, str],
    compose_paths: Mapping[str, Any],
    model_factory: ModelFactory | None = None,
) -> list[Task]:
    """One Inspect task per tier for `wave`, named `tier<k>-search-wave<w>`."""
    return [
        build_task(
            cfg, run_dir, f"tier{tier}-search-wave{wave}",
            search_samples(cfg, tier, wave, pairs, boards[tier], compose_paths), model_factory,
        )  # fmt: skip
        for tier in cfg.tiers
    ]


# ---------------------------------------------------------------- lessons and board


def lessons_text(row: dict | None, run_dir: Path) -> str:
    """The board entry of one experiment from its `samples.jsonl` row (None: the experiment has no row).

    Empty only when the session itself failed (a provider outage, or the sample errored in the solver or its
    setup); a problem of the scoring step (for example a side task check error) does not change the session's
    lessons."""
    if row is None or row.get("sample_error"):
        return ""
    if is_provider_failure(Path(run_dir) / row["sample"], row.get("exit_code")):
        return ""
    stdout = Path(run_dir) / row["sample"] / "stdout.txt"
    text = stdout.read_text(encoding="utf-8") if stdout.is_file() else ""
    if row.get("end_reason") != "final_answer" or not text.strip():
        return NO_LESSONS
    return text.rstrip("\n")


Entry = tuple[int, int, str]  # (wave, experiment number, text)


def collect_wave(
    cfg: RunConfig, run_dir: Path, wave: int, pairs: Sequence[tuple[str, str]], rows: Sequence[dict]
) -> dict[int, list[Entry]]:
    """Write `lessons.md` and `scores.json` of each experiment of `wave` and return the board entries by tier."""
    by_dir = {row["sample"]: row for row in rows}
    entries: dict[int, list[Entry]] = {}
    for tier in cfg.tiers:
        entries[tier] = []
        for i, _ in wave_pairs(cfg, pairs, wave):
            folder = experiment_dir(tier, wave, i)
            row = by_dir.get(folder)
            text = lessons_text(row, run_dir)
            out = Path(run_dir) / folder
            out.mkdir(parents=True, exist_ok=True)
            (out / "lessons.md").write_text(text + "\n" if text else "", encoding="utf-8", newline="\n")
            scores = {**(row or {}), "excluded": exclusion_reason(row, run_dir) if row else "no_row"}
            (out / "scores.json").write_text(json.dumps(scores, indent=2) + "\n", encoding="utf-8", newline="\n")
            entries[tier].append((wave, i, text))
    return entries


def render_board(entries: Sequence[Entry]) -> str:
    """The entries under `## Entry <wave>.<i>`, in order of wave then experiment number; "" for no entries."""
    blocks = []
    for wave, i, text in sorted(entries, key=lambda e: (e[0], e[1])):
        blocks.append(f"## Entry {wave}.{i}" + (f"\n\n{text}" if text else ""))
    return "\n\n".join(blocks)


def write_board(run_dir: Path, tier: int, entries: Sequence[Entry]) -> str:
    """Write `tier<k>/search/board.md`; return its text."""
    text = render_board(entries)
    path = Path(run_dir) / f"tier{tier}" / "search" / "board.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n" if text else "", encoding="utf-8", newline="\n")
    return text


def tiers_without_sessions(cfg: RunConfig, run_dir: Path) -> list[int]:
    """Tiers in which no search experiment left a meltzer session log (every experiment failed)."""
    return [
        tier
        for tier in cfg.tiers
        if not any(Path(run_dir, f"tier{tier}", "search").glob("wave*/exp*/meltzer/sessions/**/*.jsonl"))
    ]


# ---------------------------------------------------------------- reflection


def reflection_text(cfg: RunConfig, tier: int, board: str) -> str:
    """The reflection's request: base.md without own pair, the tier text and reflection.md with the board."""
    return prompts.render_instructions(REPO_ROOT / cfg.prompts.dir, tier, "reflection", base_prompt(cfg, None), board=board)


def reflection_body(agent: ModelSpec, text: str) -> dict:
    """The request: one user message, no tools, the agent model's provider and reasoning effort."""
    body = {
        "model": agent.id,
        "messages": [{"role": "user", "content": text}],
        "provider": to_plain(agent.provider),
        "usage": {"include": True},
    }
    if agent.reasoning_effort is not None:
        body["reasoning"] = {"effort": agent.reasoning_effort}
    return body


def strategy_path(run_dir: Path, tier: int) -> Path:
    return Path(run_dir) / f"tier{tier}" / "strategy.md"


async def _reflect(
    client: httpx.AsyncClient, cfg: RunConfig, run_dir: Path, tier: int, board: str, api_key: str, backoff_s: float
) -> str | None:
    """One tier's reflection: write `reflection.json` and, on success, `strategy.md`. None, or the error."""
    body = reflection_body(cfg.models.agent, reflection_text(cfg, tier, board))
    calls: list[dict] = []
    strategy, error, provider_ok = None, None, None
    try:
        for _ in range(OUTPUT_TRIES):
            data = await openrouter.post_chat(client, body, api_key, calls, backoff_s=backoff_s)
            if data is None:
                error = "HTTP retries exhausted"
                break
            provider_ok = openrouter.served_by_configured(data.get("provider"), body["provider"])
            content = (data["choices"][0].get("message") or {}).get("content")
            if isinstance(content, str) and content.strip():
                strategy, error = content, None
                break
            error = "empty reply"
    except openrouter.OpenRouterError as e:
        error = str(e)
    out = {"tier": tier, "request": body, "calls": calls, "provider_ok": provider_ok, "error": error}
    folder = Path(run_dir) / f"tier{tier}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "reflection.json").write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if strategy is not None:  # its presence means "done": a resume skips this tier
        write_text_atomic(strategy_path(run_dir, tier), strategy)
    return None if strategy is not None else f"tier {tier}: {error}"


def run_reflections(
    cfg: RunConfig,
    run_dir: Path,
    boards: Mapping[int, str],
    *,
    transport: httpx.BaseTransport | None = None,
    api_key: str | None = None,
    backoff_s: float = openrouter.BACKOFF_S,
) -> dict[int, str]:
    """Run the reflection of every tier without `strategy.md`, side by side; return the strategies by tier.

    Raises RuntimeError when one fails (its `reflection.json` says why); the others keep their `strategy.md`.
    `transport` and `api_key` replace OpenRouter in tests.
    """
    todo = [tier for tier in cfg.tiers if not strategy_path(run_dir, tier).is_file()]
    if todo:
        api_key = api_key or openrouter.load_api_key()
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set; the reflection calls OpenRouter directly")

        async def go() -> list[str | None]:
            async with httpx.AsyncClient(transport=transport) as client:
                return await asyncio.gather(
                    *(_reflect(client, cfg, run_dir, tier, boards[tier], api_key, backoff_s) for tier in todo)
                )

        errors = [e for e in asyncio.run(go()) if e]
        for tier in todo:
            data = json.loads((Path(run_dir) / f"tier{tier}" / "reflection.json").read_text(encoding="utf-8"))
            if data["provider_ok"] is False:
                print(f"warning: the reflection of tier {tier} was not served by a configured provider")
        if errors:
            raise RuntimeError("reflection failed: " + "; ".join(errors))
    return {tier: strategy_path(run_dir, tier).read_text(encoding="utf-8") for tier in cfg.tiers}
