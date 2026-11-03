"""Plots from `asr.json` files (matplotlib, Agg backend, PNG; spec 11.3).

- `plot_avg_asr`: the faithful average ASR of several runs, in the order given, as dots joined by a line, with
  their 95% intervals.
- `plot_strategies`: for one run, the number of faithful successful attacks per registry card.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.ticker import MaxNLocator  # noqa: E402

BLUE = "#2a78d6"
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e3e2de"
INTERVAL_NOTE = {
    "wilson": "Wilson interval (one side task)",
    "bootstrap_main_tasks": "bootstrap over main tasks",
}


def _style(ax, grid_axis: str) -> None:
    ax.grid(axis=grid_axis, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(colors=MUTED)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(MUTED)


def _num(x) -> float:
    return np.nan if x is None else float(x)


def plot_avg_asr(results: list[dict], path: Path) -> None:
    """Faithful average ASR per run (`asr.json` contents), dots joined by a line, with interval bars."""
    entries = [r["asr"]["faithful"]["average"] for r in results]
    v = np.array([_num(e["value"]) for e in entries])
    lo = np.array([_num(e["ci"][0]) for e in entries])
    hi = np.array([_num(e["ci"][1]) for e in entries])
    labels = [r["label"] for r in results]
    repeated = len(set(labels)) < len(labels)
    if repeated:  # two runs of the same kind: tell them apart by run id
        labels = [f"{r['label']}\n{r['run_id']}" for r in results]
    x = np.arange(len(results))

    fig, ax = plt.subplots(figsize=(max(5.0, 2.2 * len(results)), 5))
    ax.errorbar(x, v, yerr=[v - lo, hi - v], fmt="-o", color=BLUE, ecolor=BLUE, capsize=4, markersize=7, linewidth=1.4)
    ax.set_xticks(x, labels, color=INK)
    ax.set_xlim(-0.5, len(results) - 0.5)
    ax.set_ylim(-0.03, 1.03)
    ax.set_ylabel("average ASR, faithful to a registry card", color=INK)
    ax.set_title("Attack success through registry exploits", fontsize=12, color=INK)
    _style(ax, "y")
    methods = sorted({INTERVAL_NOTE.get(r["intervals"]["average"], r["intervals"]["average"]) for r in results})
    ax.text(
        0.0,
        -0.2 if repeated else -0.14,
        f"95% intervals: {'; '.join(methods)}. Provider failures and infrastructure errors are not counted.",
        transform=ax.transAxes,
        fontsize=8,
        color=MUTED,
    )
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def plot_strategies(result: dict, path: Path) -> None:
    """One horizontal bar per registry card: faithful successful attacks of the run, zeros included."""
    cards = result["strategies"]
    y = np.arange(len(cards))
    counts = [c["count"] for c in cards]

    fig, ax = plt.subplots(figsize=(9, 0.42 * len(cards) + 1.6))
    bars = ax.barh(y, counts, color=BLUE, height=0.65)
    ax.bar_label(bars, padding=3, fontsize=8, color=INK)
    ax.set_yticks(y, [f"{c['title']} ({c['card_id']})" for c in cards], fontsize=9, color=INK)
    ax.invert_yaxis()
    ax.set_xlim(0, max(counts + [1]) * 1.15)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_xlabel("samples with side task passed and judged faithful (count)", color=INK)
    ax.set_title(f"Exploit strategies: {result['label']} ({result['run_id']})", fontsize=12, color=INK)
    _style(ax, "x")
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
