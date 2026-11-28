"""Side task success rates with 95% intervals (used by `bench.metrics`).

Per side task: k out of n, with the Wilson interval. The average is the mean of the per-side-task values; with one
side task it is that side task's value and interval, with several its interval is a percentile bootstrap that
resamples main tasks with replacement and keeps all side tasks of each drawn main task (side tasks share main tasks,
so their rates are not independent).

A difference between two groups of samples on the same main tasks (two tiers, a ceiling and its tier) uses the same
resamples for both groups (same seed, same main tasks), so its bootstrap is paired.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

import numpy as np
from scipy.stats import norm

ALPHA = 0.05
N_BOOT = 10_000


def wilson_interval(k: int, n: int, alpha: float = ALPHA) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion, no continuity correction. (nan, nan) when n == 0."""
    if n == 0:
        return (math.nan, math.nan)
    z = float(norm.ppf(1 - alpha / 2))
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    lo = 0.0 if k == 0 else max(0.0, center - half)
    hi = 1.0 if k == n else min(1.0, center + half)
    return (lo, hi)


def bootstrap_average(success: np.ndarray, valid: np.ndarray, n_boot: int, seed: int) -> np.ndarray:
    """Averages over side tasks of `n_boot` resamples of the main tasks.

    `success[m, s]` is 0/1 and `valid[m, s]` says whether main task m has a counted sample in side task s. Each
    resample draws as many main tasks as there are, with replacement, and keeps all side tasks of each drawn task.
    The resamples depend only on `seed` and the number of main tasks.
    """
    n_main = valid.shape[0]
    rng = np.random.default_rng(seed)
    # counts[r, m] = number of times main task m is drawn in resample r
    counts = rng.multinomial(n_main, np.full(n_main, 1.0 / n_main), size=n_boot).astype(float)
    v = valid.astype(float)
    num, den = counts @ (success * v), counts @ v
    per = np.full(num.shape, np.nan)
    np.divide(num, den, out=per, where=den > 0)
    with np.errstate(invalid="ignore"):
        return np.nanmean(per, axis=1)


def _num(x: float) -> float | None:
    return None if math.isnan(x) else float(x)


def _entry(k: int, n: int) -> dict:
    lo, hi = wilson_interval(k, n)
    return {"k": k, "n": n, "value": k / n if n else None, "ci": [_num(lo), _num(hi)]}


def _percentiles(values: np.ndarray) -> list[float | None]:
    if np.all(np.isnan(values)):
        return [None, None]
    lo, hi = np.nanpercentile(values, [100 * ALPHA / 2, 100 * (1 - ALPHA / 2)])
    return [float(lo), float(hi)]


Test = Callable[[dict], bool]


def matrix(
    rows: Sequence[dict], main_tasks: Sequence[str], side_tasks: Sequence[str], test: Test
) -> tuple[np.ndarray, np.ndarray]:
    """(success, valid) over `main_tasks` x `side_tasks` for the counted `rows`."""
    m_idx, s_idx = {m: i for i, m in enumerate(main_tasks)}, {s: j for j, s in enumerate(side_tasks)}
    success = np.zeros((len(main_tasks), len(side_tasks)))
    valid = np.zeros((len(main_tasks), len(side_tasks)), dtype=bool)
    for r in rows:
        if r["side_task"] in s_idx:
            i, j = m_idx[str(r["main_task"])], s_idx[r["side_task"]]
            success[i, j], valid[i, j] = float(bool(test(r))), True
    return success, valid


def rates(
    rows: Sequence[dict], side_tasks: Sequence[str], main_tasks: Sequence[str], test: Test, n_boot: int, seed: int
) -> tuple[dict, str]:
    """Per side task and average entries of the rate of `test` over the counted `rows`, and the method of the
    average's interval (`none`, `wilson` or `bootstrap_main_tasks`)."""
    out = {}
    for s in side_tasks:
        mine = [r for r in rows if r["side_task"] == s]
        out[s] = _entry(sum(bool(test(r)) for r in mine), len(mine))
    measured = [s for s in side_tasks if out[s]["n"]]
    if not measured:
        out["average"] = {"value": None, "ci": [None, None]}
        return out, "none"
    if len(measured) == 1:
        out["average"] = {"value": out[measured[0]]["value"], "ci": out[measured[0]]["ci"]}
        return out, "wilson"
    averages = bootstrap_average(*matrix(rows, main_tasks, measured, test), n_boot, seed)
    out["average"] = {"value": float(np.mean([out[s]["value"] for s in measured])), "ci": _percentiles(averages)}
    return out, "bootstrap_main_tasks"


def average_difference(
    rows_a: Sequence[dict],
    rows_b: Sequence[dict],
    side_tasks: Sequence[str],
    main_tasks: Sequence[str],
    test: Test,
    n_boot: int,
    seed: int,
) -> dict:
    """Average rate of group a minus that of group b, with a paired bootstrap interval over `main_tasks`. Both
    averages and the bootstrap use only the side tasks with counted samples in both groups (listed in the result),
    so the two averages cover the same side tasks."""
    common = [s for s in side_tasks if any(r["side_task"] == s for r in rows_a) and any(r["side_task"] == s for r in rows_b)]
    if not common:
        return {"value": None, "ci": [None, None], "side_tasks": []}

    def average(rows: Sequence[dict]) -> float:
        per_side = [[r for r in rows if r["side_task"] == s] for s in common]
        return float(np.mean([sum(bool(test(r)) for r in mine) / len(mine) for mine in per_side]))

    boot_a = bootstrap_average(*matrix(rows_a, main_tasks, common, test), n_boot, seed)
    boot_b = bootstrap_average(*matrix(rows_b, main_tasks, common, test), n_boot, seed)
    return {"value": average(rows_a) - average(rows_b), "ci": _percentiles(boot_a - boot_b), "side_tasks": common}
