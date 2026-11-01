"""Plots 1-3 of spec section 11.3 (matplotlib, Agg backend, PNG).

Colors are the first three slots of the validated categorical palette
(blue, orange, aqua); markers differ by condition so color is not the only cue.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.ticker import MaxNLocator  # noqa: E402

BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e3e2de"

# (metric key in summary.json, legend label, color, marker)
_ASR_SERIES = [
    ("asr_unaware", "unaware", BLUE, "o"),
    ("asr_aware_strict", "aware, strict (side task passed and judged faithful)", ORANGE, "s"),
    ("asr_aware_scorer", "aware, scorer only (side task passed)", AQUA, "D"),
]


def _pretty(name: str) -> str:
    return name.replace("_", " ")


def _style(ax, grid_axis: str) -> None:
    ax.grid(axis=grid_axis, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(colors=MUTED)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(MUTED)


def _triples(entries: dict, labels: list[str]):
    """(value, lo, hi) arrays from summary entries; None becomes NaN."""

    def num(x):
        return np.nan if x is None else float(x)

    v = np.array([num(entries[l]["value"]) for l in labels])
    lo = np.array([num(entries[l]["ci"][0]) for l in labels])
    hi = np.array([num(entries[l]["ci"][1]) for l in labels])
    return v, lo, hi


def plot_strategies(
    counts: dict[str, dict[str, int]],
    side_tasks: list[str],
    card_labels: dict[str, str],
    path: Path,
) -> None:
    """Plot 1. `counts[key][card_id]` = number of aware samples with side_pass and faithful,
    for key in side_tasks + ["all"]. All card ids are drawn, zeros included.
    `card_labels[card_id]` is the axis label, e.g. "delayed script swap (E01)"."""
    cards = list(card_labels)
    y = np.arange(len(cards))
    side_max = max([counts[s][c] for s in side_tasks for c in cards] + [1])

    fig = plt.figure(figsize=(20, 8.5))
    outer = fig.add_gridspec(1, 2, width_ratios=[1.0, 1.9], wspace=0.5)
    inner = outer[0, 1].subgridspec(2, 2, wspace=0.95, hspace=0.3)
    tick_labels = [card_labels[c] for c in cards]

    ax = fig.add_subplot(outer[0, 0])
    vals = [counts["all"][c] for c in cards]
    bars = ax.barh(y, vals, color=BLUE, height=0.65)
    ax.bar_label(bars, padding=3, fontsize=8, color=INK)
    ax.set_yticks(y, tick_labels, fontsize=9, color=INK)
    ax.invert_yaxis()
    ax.set_xlim(0, max(max(vals), 1) * 1.15)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_xlabel("aware samples with side task passed and judged faithful (count)")
    ax.set_title("All side tasks", fontsize=11, color=INK)
    _style(ax, "x")

    first = None
    for idx, s in enumerate(side_tasks[:4]):
        r, c = divmod(idx, 2)
        sub = fig.add_subplot(inner[r, c], sharex=first, sharey=first)
        first = first or sub
        v = [counts[s][k] for k in cards]
        b = sub.barh(y, v, color=BLUE, height=0.65)
        sub.bar_label(b, padding=2, fontsize=7, color=INK)
        sub.set_title(_pretty(s), fontsize=10, color=INK)
        sub.set_yticks(y, tick_labels, fontsize=7.5, color=INK)
        sub.tick_params(labelleft=True, labelbottom=(r == 1))
        if r == 1:
            sub.set_xlabel("count")
        _style(sub, "x")
    if first is not None:
        first.set_xlim(0, side_max * 1.25)
        first.xaxis.set_major_locator(MaxNLocator(integer=True))
        first.invert_yaxis()  # shared y: inverts all four

    fig.suptitle("Strategy distribution: exploit cards followed by successful aware attacks", fontsize=12, color=INK)
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def plot_asr(summary: dict, path: Path) -> None:
    """Plot 2. Per side task and average: point with interval for each of the three metrics."""
    labels = summary["side_tasks"] + ["average"]
    x = np.arange(len(labels))
    offsets = [-0.2, 0.0, 0.2]

    fig, ax = plt.subplots(figsize=(10, 5.5))
    for (key, name, color, marker), off in zip(_ASR_SERIES, offsets):
        v, lo, hi = _triples(summary["metrics"][key], labels)
        ax.errorbar(
            x + off,
            v,
            yerr=[v - lo, hi - v],
            fmt=marker,
            color=color,
            ecolor=color,
            capsize=3,
            markersize=7,
            linewidth=1.4,
            label=name,
        )
    ax.axvline(len(labels) - 1.5, color=MUTED, linestyle=":", linewidth=1)
    ax.set_xticks(x, [_pretty(l) for l in labels], color=INK)
    ax.set_ylim(-0.03, 1.03)
    ax.set_ylabel("attack success rate (ASR)", color=INK)
    ax.set_xlabel("side task", color=INK)
    ax.set_title("ASR by condition", fontsize=12, color=INK)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.16), ncol=1, frameon=False)
    _style(ax, "y")
    ax.text(
        0.0,
        -0.36,
        "95% intervals. Side tasks: Wilson interval. Average: bootstrap over main tasks "
        f"({summary['bootstrap']['n_boot']} resamples).",
        transform=ax.transAxes,
        fontsize=8,
        color=MUTED,
    )
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def plot_gap(summary: dict, path: Path) -> None:
    """Plot 3. Gap (aware strict - unaware) per side task and average, bootstrap interval, zero line."""
    labels = summary["side_tasks"] + ["average"]
    x = np.arange(len(labels))
    v, lo, hi = _triples(summary["gaps"]["asr_aware_strict"], labels)

    fig, ax = plt.subplots(figsize=(8.5, 5))
    ax.axhline(0, color=INK, linewidth=1.2, zorder=1)
    ax.errorbar(
        x,
        v,
        yerr=[v - lo, hi - v],
        fmt="o",
        color=BLUE,
        ecolor=BLUE,
        capsize=4,
        markersize=7,
        linewidth=1.4,
        zorder=3,
    )
    ax.axvline(len(labels) - 1.5, color=MUTED, linestyle=":", linewidth=1)
    ax.set_xticks(x, [_pretty(l) for l in labels], color=INK)
    finite = np.concatenate([lo[~np.isnan(lo)], hi[~np.isnan(hi)], [0.0]])
    pad = 0.05 * max(finite.max() - finite.min(), 0.1)
    ax.set_ylim(finite.min() - pad, finite.max() + pad)
    ax.set_ylabel("gap in ASR (aware strict - unaware)", color=INK)
    ax.set_xlabel("side task", color=INK)
    ax.set_title("Attack uplift from oversight awareness", fontsize=12, color=INK)
    _style(ax, "y")
    ax.text(
        0.0,
        -0.17,
        f"95% bootstrap intervals over main tasks ({summary['bootstrap']['n_boot']} resamples). "
        "Line at 0 = no difference.",
        transform=ax.transAxes,
        fontsize=8,
        color=MUTED,
    )
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
