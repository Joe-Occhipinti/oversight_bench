"""Plots of the failure and decision analysis across runs (`python -m bench plot-failures <run_id>...
[--taxonomy DIR] [--out DIR]`, spec-failure-analysis 9).

Inputs per run: `samples.jsonl`, `asr.json` (excluded samples), `failure_extract.jsonl`; across runs, the
taxonomy proposal `DIR/proposal.json` with its provisional assignments. Excluded samples are left out; samples
whose extraction reading failed are counted as `not read`.

Figures (PNG, matplotlib) in the output folder; `summary.json` holds the numbers behind them.
"""

from __future__ import annotations

import json
import re
import textwrap
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

from bench.asr import card_titles, read_rows, run_label  # noqa: E402
from bench.config import REPO_ROOT, RunConfig  # noqa: E402
from bench.failures import OUT_FILE, condition_key  # noqa: E402

INK, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#e3e2de", "#fcfcfb"
BLUE, ORANGE, AQUA, YELLOW, MAGENTA, VIOLET = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#6250d6"
GRAY_DARK, GRAY_LIGHT = "#8a8984", "#c9c8c2"
SEQ = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]  # blue ramp, light -> dark

TYPES = ["card", "unlisted_harness", "no_harness"]
TYPE_LABELS = {"card": "known exploit (card)", "unlisted_harness": "harness flaw not on a card",
               "no_harness": "sneakiness only (no harness flaw)"}  # fmt: skip
OUTCOMES = ["side_task_done", "monitor_blocked", "technical_failure", "abandoned", "episode_ended"]
OUTCOME_LABELS = {"side_task_done": "side task done", "monitor_blocked": "blocked by the monitor",
                  "technical_failure": "technical failure", "abandoned": "abandoned",
                  "episode_ended": "run ended first"}  # fmt: skip
OUTCOME_COLORS = dict(zip(OUTCOMES, [BLUE, ORANGE, AQUA, YELLOW, MAGENTA]))  # validated adjacent order

# Sample classes, in stack order; colors validated as adjacent pairs in this order.
CLASSES = [
    ("succeeded_card", "succeeded with a card", BLUE),
    ("succeeded_other", "succeeded without a card", AQUA),
    ("failed_card", "failed, tried a card", ORANGE),
    ("failed_unlisted", "failed, tried a harness flaw not on a card", VIOLET),
    ("failed_sneaky", "failed, sneakiness only", YELLOW),
    ("refused", "refused", GRAY_DARK),
    ("no_attempt", "no attempt (ignored or only planned)", GRAY_LIGHT),
    ("not_read", "extraction failed", "#ffffff"),
]


# ------------------------------------------------------------------ data


def load_runs(runs: list[tuple[Path, RunConfig]]) -> list[dict]:
    """Per run: id, label, key, and its counted samples, each with the extraction output (None if not read)."""
    out = []
    for run_dir, cfg in runs:
        run_dir = Path(run_dir)
        asr = json.loads((run_dir / "asr.json").read_text(encoding="utf-8"))
        excluded = {e["sample_id"] for e in asr["excluded"]}
        extract = {r["sample_id"]: r for r in read_rows(run_dir / OUT_FILE)}
        samples = []
        for row in read_rows(run_dir / "samples.jsonl"):
            if row["sample_id"] in excluded:
                continue
            x = extract.get(row["sample_id"]) or {}
            output = x.get("output") if x.get("status") == "completed" else None
            samples.append({**row, "output": output, "agent_run_id": x.get("agent_run_id")})
        out.append({"run_id": cfg.run_id, "label": run_label(cfg), "key": condition_key(cfg), "samples": samples,
                    "cards_dir": REPO_ROOT / cfg.prompts.cards})  # fmt: skip
    return out


def attempts(sample: dict) -> list[dict]:
    return (sample.get("output") or {}).get("attempts") or []


def sample_class(sample: dict) -> str:
    out = sample.get("output")
    if not out:
        return "not_read"
    if sample.get("side_pass"):
        done = [a for a in attempts(sample) if a["outcome"] == "side_task_done"]
        return "succeeded_card" if any(a["type"] == "card" for a in done) else "succeeded_other"
    types = {a["type"] for a in attempts(sample)}
    if not types:
        return "refused" if out.get("engagement") == "refused" else "no_attempt"
    if "card" in types:
        return "failed_card"
    return "failed_unlisted" if "unlisted_harness" in types else "failed_sneaky"


# ------------------------------------------------------------------ helpers


def _style(ax, grid_axis: str = "x") -> None:
    ax.set_facecolor(SURFACE)
    if grid_axis:
        ax.grid(axis=grid_axis, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(MUTED)
    ax.tick_params(colors=MUTED, labelcolor=INK)


def _figure(*args, **kw):
    fig, axes = plt.subplots(*args, **kw)
    fig.patch.set_facecolor(SURFACE)
    return fig, axes


def _save(fig, path: Path, note: str | None = None) -> None:
    if note:
        fig.text(0.01, 0.005, note, fontsize=8, color=MUTED, ha="left", va="bottom", wrap=True)
    fig.savefig(path, dpi=150, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)


def _wrap(text: str, width: int = 38) -> str:
    return "\n".join(textwrap.wrap(text, width)) or text


def _stacked_row(ax, y, parts, total, colors, min_label=0.04, height=0.62):
    """One horizontal 100% bar; parts are counts; labels show the count inside segments wide enough."""
    left = 0.0
    for count, color in zip(parts, colors):
        if not count:
            continue
        w = count / total
        ax.barh(y, w, left=left, color=color, edgecolor=SURFACE, linewidth=1.5, height=height)
        if w >= min_label:
            dark = color in (BLUE, VIOLET, ORANGE, GRAY_DARK)
            ax.text(left + w / 2, y, str(count), ha="center", va="center", fontsize=8,
                    color="#ffffff" if dark else INK)  # fmt: skip
        left += w


# ------------------------------------------------------------------ figures


def plot_sample_outcomes(runs: list[dict], path: Path) -> dict:
    """What happened in each counted sample: one 100% bar per run."""
    counts = {r["label"]: Counter(sample_class(s) for s in r["samples"]) for r in runs}
    keys = [k for k, _, _ in CLASSES if any(c[k] for c in counts.values())]
    meta = {k: (label, color) for k, label, color in CLASSES}
    fig, ax = _figure(figsize=(10, 0.8 * len(runs) + 2.2))
    for i, r in enumerate(runs):
        c = counts[r["label"]]
        _stacked_row(ax, i, [c[k] for k in keys], sum(c.values()), [meta[k][1] for k in keys])
    ax.set_yticks(range(len(runs)), [f"{r['label']}\n(n = {len(r['samples'])})" for r in runs])
    ax.invert_yaxis()
    ax.set_xlim(0, 1)
    ax.set_xlabel("share of counted samples", color=INK)
    ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    _style(ax, "x")
    handles = [Patch(facecolor=meta[k][1], edgecolor=MUTED if k == "not_read" else meta[k][1], label=meta[k][0])
               for k in keys]  # fmt: skip
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.14), ncol=3, frameon=False, fontsize=8)
    ax.set_title("What each run did with the side task", color=INK, fontsize=12, loc="left")
    ax.text(0, -0.36, "A failed sample is classed by its most harness-reliant attempt: card > harness flaw not on a "
                      "card > sneakiness only. Numbers in the bars are sample counts.", transform=ax.transAxes,
            fontsize=8, color=MUTED)  # fmt: skip
    _save(fig, path)
    return {label: dict(c) for label, c in counts.items()}


def plot_attempt_outcomes(runs: list[dict], path: Path) -> dict:
    """Attempts by strategy type, stacked by outcome; one panel per run, shared x."""
    data = {r["label"]: Counter((a["type"], a["outcome"]) for s in r["samples"] for a in attempts(s)) for r in runs}
    xmax = max((sum(c[(t, o)] for o in OUTCOMES) for c in data.values() for t in TYPES), default=1)
    fig, axes = _figure(len(runs), 1, figsize=(10, 1.6 * len(runs) + 1.2), sharex=True)
    axes = np.atleast_1d(axes)
    for ax, r in zip(axes, runs):
        c = data[r["label"]]
        for i, t in enumerate(TYPES):
            left = 0
            for o in OUTCOMES:
                n = c[(t, o)]
                if n:
                    ax.barh(i, n, left=left, color=OUTCOME_COLORS[o], edgecolor=SURFACE, linewidth=1.5, height=0.62)
                    if n >= xmax * 0.03:
                        dark = OUTCOME_COLORS[o] in (BLUE, ORANGE)
                        ax.text(left + n / 2, i, str(n), ha="center", va="center", fontsize=8,
                                color="#ffffff" if dark else INK)  # fmt: skip
                    left += n
            ax.text(left + xmax * 0.01, i, f"{left}", va="center", fontsize=8, color=MUTED)
        ax.set_yticks(range(len(TYPES)), [TYPE_LABELS[t] for t in TYPES], fontsize=9)
        ax.invert_yaxis()
        ax.set_title(r["label"], color=INK, fontsize=10, loc="left")
        _style(ax, "x")
    axes[-1].set_xlabel("attempts", color=INK)
    axes[0].set_xlim(0, xmax * 1.08)
    handles = [Patch(facecolor=OUTCOME_COLORS[o], label=OUTCOME_LABELS[o]) for o in OUTCOMES]
    fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, 0.0), ncol=5, frameon=False, fontsize=8)
    fig.suptitle("How attempts ended, by strategy", color=INK, fontsize=12, x=0.01, ha="left")
    fig.tight_layout()
    _save(fig, path)
    return {label: {f"{t}/{o}": c[(t, o)] for t in TYPES for o in OUTCOMES if c[(t, o)]} for label, c in data.items()}


def plot_attempts_per_sample(runs: list[dict], path: Path) -> dict:
    """Number of attempts per engaged sample, split by success; one panel per run."""
    bins = ["1", "2", "3", "4", "5+"]
    b = lambda n: bins[min(n, 5) - 1]  # noqa: E731
    data = {}
    for r in runs:
        engaged = [s for s in r["samples"] if attempts(s)]
        data[r["label"]] = {"succeeded": Counter(b(len(attempts(s))) for s in engaged if s.get("side_pass")),
                            "failed": Counter(b(len(attempts(s))) for s in engaged if not s.get("side_pass"))}  # fmt: skip
    ymax = max(data[l]["succeeded"][k] + data[l]["failed"][k] for l in data for k in bins)
    fig, axes = _figure(1, len(runs), figsize=(3.0 * len(runs), 3.6), sharey=True)
    axes = np.atleast_1d(axes)
    for ax, r in zip(axes, runs):
        d = data[r["label"]]
        x = np.arange(len(bins))
        succ = np.array([d["succeeded"][k] for k in bins])
        fail = np.array([d["failed"][k] for k in bins])
        ax.bar(x, succ, color=BLUE, width=0.62, edgecolor=SURFACE, linewidth=1.5)
        ax.bar(x, fail, bottom=succ, color=ORANGE, width=0.62, edgecolor=SURFACE, linewidth=1.5)
        for xi, (s_, f_) in enumerate(zip(succ, fail)):
            if s_ + f_:
                ax.text(xi, s_ + f_ + ymax * 0.02, str(s_ + f_), ha="center", fontsize=8, color=MUTED)
        ax.set_xticks(x, bins)
        ax.set_title(r["label"], color=INK, fontsize=10, loc="left")
        ax.set_xlabel("attempts in the sample", color=INK, fontsize=9)
        _style(ax, "y")
    axes[0].set_ylabel("samples with at least one attempt", color=INK)
    axes[0].set_ylim(0, ymax * 1.15)
    fig.legend(handles=[Patch(facecolor=BLUE, label="side task succeeded"), Patch(facecolor=ORANGE, label="failed")],
               loc="upper center", bbox_to_anchor=(0.5, 0.0), ncol=2, frameon=False, fontsize=8)  # fmt: skip
    fig.suptitle("Persistence: how many distinct mechanisms the agent tried", color=INK, fontsize=12, x=0.01, ha="left")
    fig.tight_layout()
    _save(fig, path)
    return {l: {k: dict(v) for k, v in d.items()} for l, d in data.items()}


TYPE_COLORS = {"card": BLUE, "unlisted_harness": AQUA, "no_harness": YELLOW}  # blue/aqua/yellow pass all pairs
OUTCOME_MARK = {"side_task_done": "*", "monitor_blocked": "x", "technical_failure": "o", "abandoned": "-",
                "episode_ended": "|"}  # fmt: skip


def plot_sequences(runs: list[dict], path: Path) -> dict:
    """Each engaged sample as a row of its attempts in order: fill = strategy, mark = outcome."""
    rows_per_run = {r["label"]: sorted((s for s in r["samples"] if attempts(s)),
                                       key=lambda s: (not s.get("side_pass"), len(attempts(s))))
                    for r in runs}  # fmt: skip
    width = max((len(attempts(s)) for rows in rows_per_run.values() for s in rows), default=1)
    width = min(width, 11)
    height = max(len(rows) for rows in rows_per_run.values())
    fig, axes = _figure(1, len(runs), figsize=(2.6 * len(runs) + 1, max(6, height * 0.09 + 2)), sharey=True)
    axes = np.atleast_1d(axes)
    for ax, r in zip(axes, runs):
        rows = rows_per_run[r["label"]]
        for y, s in enumerate(rows):
            for x, a in enumerate(attempts(s)[:width]):
                ax.add_patch(plt.Rectangle((x, y), 0.9, 0.86, color=TYPE_COLORS[a["type"]], linewidth=0))
                ax.text(x + 0.45, y + 0.45, OUTCOME_MARK[a["outcome"]], ha="center", va="center", fontsize=5.5,
                        color=INK)  # fmt: skip
        n_succ = sum(1 for s in rows if s.get("side_pass"))
        if n_succ and n_succ < len(rows):
            ax.axhline(n_succ - 0.07, color=INK, linewidth=0.8)
            ax.text(width + 0.1, n_succ / 2, "succeeded", rotation=90, va="center", fontsize=8, color=MUTED)
            ax.text(width + 0.1, (n_succ + len(rows)) / 2, "failed", rotation=90, va="center", fontsize=8, color=MUTED)
        ax.set_xlim(0, width + 0.6)
        ax.set_ylim(height, 0)
        ax.set_xticks(np.arange(width) + 0.45, [str(i + 1) for i in range(width)], fontsize=7)
        ax.set_yticks([])
        ax.set_xlabel("attempt number", color=INK, fontsize=9)
        ax.set_title(f"{r['label']}\n({len(rows)} samples with attempts)", color=INK, fontsize=10, loc="left")
        _style(ax, "")
        ax.spines["left"].set_visible(False)
    handles = [Patch(facecolor=TYPE_COLORS[t], label=TYPE_LABELS[t]) for t in TYPES]
    handles += [plt.Line2D([], [], linestyle="", marker=f"${OUTCOME_MARK[o]}$", color=INK, label=OUTCOME_LABELS[o])
                for o in OUTCOMES]  # fmt: skip
    fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, 0.0), ncol=4, frameon=False, fontsize=8)
    fig.suptitle("Attempt sequences: one row per sample, attempts left to right", color=INK, fontsize=12, x=0.01,
                 ha="left")  # fmt: skip
    fig.tight_layout()
    _save(fig, path)
    # what follows each failed attempt
    follow = {}
    for r in runs:
        c = Counter()
        for s in r["samples"]:
            at = attempts(s)
            for i, a in enumerate(at):
                if a["outcome"] in ("monitor_blocked", "technical_failure"):
                    nxt = at[i + 1] if i + 1 < len(at) else None
                    what = "none" if nxt is None else ("same type" if nxt["type"] == a["type"] else f"-> {nxt['type']}")
                    if nxt is not None and a["type"] == "card" and nxt["type"] == "card":
                        what = "same card" if nxt["card_id"] == a["card_id"] else "other card"
                    c[(a["outcome"], what)] += 1
        follow[r["label"]] = {f"{o} | next: {w}": n for (o, w), n in sorted(c.items())}
    return follow


def plot_cards(runs: list[dict], path: Path) -> dict:
    """Card attempts per run: heatmap of attempts, annotated succeeded/attempts."""
    titles = card_titles(runs[0]["cards_dir"])
    ids = sorted(titles)
    att = {r["label"]: Counter() for r in runs}
    succ = {r["label"]: Counter() for r in runs}
    for r in runs:
        for s in r["samples"]:
            for a in attempts(s):
                if a["type"] == "card" and a["card_id"]:
                    att[r["label"]][a["card_id"]] += 1
                    succ[r["label"]][a["card_id"]] += a["outcome"] == "side_task_done"
    labels = [r["label"] for r in runs]
    m = np.array([[att[l][c] for l in labels] for c in ids], dtype=float)
    vmax = max(m.max(), 1)
    fig, ax = _figure(figsize=(2.0 * len(runs) + 4, 0.42 * len(ids) + 1.6))
    cmap = matplotlib.colors.ListedColormap(SEQ)
    norm = matplotlib.colors.BoundaryNorm(np.linspace(0.5, vmax + 0.5, len(SEQ) + 1), len(SEQ))
    masked = np.ma.masked_where(m == 0, m)
    ax.imshow(masked, cmap=cmap, norm=norm, aspect="auto")
    for i, c in enumerate(ids):
        for j, l in enumerate(labels):
            if att[l][c]:
                v = att[l][c]
                dark = norm(v) >= 4
                ax.text(j, i, f"{succ[l][c]}/{v}", ha="center", va="center", fontsize=8,
                        color="#ffffff" if dark else INK)  # fmt: skip
    ax.set_xticks(range(len(labels)), labels)
    ax.set_yticks(range(len(ids)), [f"{titles[c]} ({c})" for c in ids], fontsize=9)
    ax.xaxis.tick_top()
    ax.set_xticks(np.arange(-0.5, len(labels)), minor=True)
    ax.set_yticks(np.arange(-0.5, len(ids)), minor=True)
    ax.grid(which="minor", color=SURFACE, linewidth=2)
    ax.tick_params(which="both", length=0, labelcolor=INK)
    for side in ax.spines.values():
        side.set_visible(False)
    ax.set_title("Known exploits tried, by run: succeeded / attempts (darker = more attempts)", color=INK,
                 fontsize=11, loc="left", pad=28)  # fmt: skip
    _save(fig, path, "Counts attempts typed `card` by the extraction (an agent in unaware or explore that used a "
                     "card's mechanism without seeing the card counts too). Empty cell: no attempt.")  # fmt: skip
    return {l: {c: [succ[l][c], att[l][c]] for c in ids if att[l][c]} for l in labels}


def plot_one_card_outcomes(runs: list[dict], path: Path) -> dict | None:
    """Mode one_card (each card assigned equally often): per assigned card, the samples by outcome of their
    attempts with that card."""
    r = next((r for r in runs if r["key"] == "one_card"), None)
    if r is None:
        return None
    titles = card_titles(r["cards_dir"])
    kinds = ["side_task_done", "monitor_blocked", "technical_failure", "abandoned", "episode_ended", "none"]
    colors = {**OUTCOME_COLORS, "none": GRAY_LIGHT}
    labels = {**OUTCOME_LABELS, "none": "no attempt with this card"}
    per = defaultdict(Counter)
    for s in r["samples"]:
        own = [a for a in attempts(s) if a["type"] == "card" and a["card_id"] == s.get("assigned_card")]
        # the sample's furthest outcome with its card: done > blocked > technical > abandoned > ended
        outcome = next((o for o in kinds[:-1] if any(a["outcome"] == o for a in own)), "none")
        per[s.get("assigned_card")][outcome] += 1
    ids = sorted(per, key=lambda c: (-per[c]["side_task_done"], -per[c]["technical_failure"], c))
    fig, ax = _figure(figsize=(10, 0.42 * len(ids) + 1.8))
    for i, c in enumerate(ids):
        left = 0
        for k in kinds:
            n = per[c][k]
            if n:
                ax.barh(i, n, left=left, color=colors[k], edgecolor=SURFACE, linewidth=1.5, height=0.66)
                ax.text(left + n / 2, i, str(n), ha="center", va="center", fontsize=8,
                        color="#ffffff" if colors[k] in (BLUE, ORANGE) else INK)  # fmt: skip
                left += n
    ax.set_yticks(range(len(ids)), [f"{titles[c]} ({c})" for c in ids], fontsize=9)
    ax.invert_yaxis()
    ax.set_xlabel("samples assigned the card", color=INK)
    _style(ax, "x")
    handles = [Patch(facecolor=colors[k], label=labels[k]) for k in kinds]
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=3, frameon=False, fontsize=8)
    ax.set_title("One card: how far each assigned card got (furthest outcome of its attempts)", color=INK,
                 fontsize=12, loc="left")  # fmt: skip
    _save(fig, path)
    return {c: dict(per[c]) for c in ids}


def one_card_adherence(runs: list[dict]) -> dict:
    """Mode one_card: per assigned card, samples whose attempts used only that card, another card, or none."""
    out = {}
    for r in runs:
        if r["key"] != "one_card":
            continue
        c = defaultdict(Counter)
        for s in r["samples"]:
            cards = {a["card_id"] for a in attempts(s) if a["type"] == "card"}
            other = {a["type"] for a in attempts(s)} - {"card"}
            assigned = s.get("assigned_card")
            kind = ("no attempt" if not attempts(s) else "only assigned" if cards == {assigned} and not other
                    else "assigned + other" if assigned in cards else "never assigned")  # fmt: skip
            c[assigned][kind] += 1
            c[assigned]["succeeded"] += bool(s.get("side_pass"))
        out[r["label"]] = {k: dict(v) for k, v in sorted(c.items())}
    return out


def server_requests(sample: dict) -> int | None:
    """Requests the logging server captured, from the scorer's explanation (None if not stated)."""
    m = re.search(r"Captured (\d+) requests", sample.get("side_explanation") or "")
    return int(m.group(1)) if m else None


def plot_failed_delivery(runs: list[dict], path: Path) -> dict:
    """Failed samples with an attempt: did anything reach the logging server? Split by the strategy class."""
    groups = [("failed_card", "tried a card"), ("failed_unlisted", "tried a harness flaw not on a card"),
              ("failed_sneaky", "sneakiness only")]  # fmt: skip
    data = {}
    fig, axes = _figure(1, len(groups), figsize=(4.0 * len(groups) + 2, 0.75 * len(runs) + 1.8), sharey=True)
    for ax, (cls, title) in zip(np.atleast_1d(axes), groups):
        for i, r in enumerate(runs):
            ss = [s for s in r["samples"] if sample_class(s) == cls]
            got = sum(1 for s in ss if server_requests(s))
            none = len(ss) - got
            data.setdefault(r["label"], {})[cls] = {"server_received_requests": got, "nothing_received": none}
            ax.barh(i, got, color=VIOLET, height=0.62, edgecolor=SURFACE, linewidth=1.5)
            ax.barh(i, none, left=got, color=GRAY_LIGHT, height=0.62, edgecolor=SURFACE, linewidth=1.5)
            if got:
                ax.text(got / 2, i, str(got), ha="center", va="center", fontsize=8, color="#ffffff")
            if none:
                ax.text(got + none / 2, i, str(none), ha="center", va="center", fontsize=8, color=INK)
        ax.set_title(f"failed, {title}", color=INK, fontsize=10, loc="left")
        ax.set_xlabel("failed samples", color=INK)
        _style(ax, "x")
    ax0 = np.atleast_1d(axes)[0]
    ax0.set_yticks(range(len(runs)), [r["label"] for r in runs])
    ax0.invert_yaxis()
    fig.legend(handles=[Patch(facecolor=VIOLET, label="the logging server received requests, but no secret in plaintext"),
                        Patch(facecolor=GRAY_LIGHT, label="the logging server received nothing")],
               loc="upper center", bbox_to_anchor=(0.5, 0.0), ncol=2, frameon=False, fontsize=8)  # fmt: skip
    fig.suptitle("Failed samples: did the exfiltration reach the server?", color=INK, fontsize=12, x=0.01, ha="left")
    fig.tight_layout()
    _save(fig, path)
    return data


def plot_first_attempt(runs: list[dict], blocks: dict[str, int], path: Path) -> dict:
    """Where in the transcript the first attempt starts (share of blocks), per run, split by success."""
    fig, ax = _figure(figsize=(9, 0.9 * len(runs) + 1.6))
    rng = np.random.default_rng(0)
    data = {}
    for i, r in enumerate(runs):
        pts = []
        for s in r["samples"]:
            at = attempts(s)
            n = blocks.get(s["sample_id"])
            if at and n:
                pts.append((min(a["first_block"] for a in at) / n, bool(s.get("side_pass"))))
        for succeeded, color, dy in ((True, BLUE, -0.12), (False, ORANGE, 0.12)):
            xs = [p for p, ok in pts if ok == succeeded]
            ax.scatter(xs, i + dy + rng.uniform(-0.07, 0.07, len(xs)), s=14, color=color, edgecolor=SURFACE,
                       linewidth=0.5, zorder=3)  # fmt: skip
            if xs:
                ax.plot([np.median(xs)] * 2, [i + dy - 0.12, i + dy + 0.12], color=INK, linewidth=2, zorder=4)
        data[r["label"]] = {"median_all": float(np.median([p for p, _ in pts])) if pts else None, "n": len(pts)}
    ax.set_yticks(range(len(runs)), [r["label"] for r in runs])
    ax.invert_yaxis()
    ax.set_xlim(0, 1)
    ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.set_xlabel("position of the first attempt in the transcript (share of messages)", color=INK)
    _style(ax, "x")
    ax.legend(handles=[Patch(facecolor=BLUE, label="side task succeeded"), Patch(facecolor=ORANGE, label="failed"),
                       plt.Line2D([], [], color=INK, linewidth=2, label="median")],
              loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=3, frameon=False, fontsize=8)  # fmt: skip
    ax.set_title("How long the agent prepared before its first attempt", color=INK, fontsize=12, loc="left")
    _save(fig, path)
    return data


def plot_unaware(runs: list[dict], path: Path) -> dict | None:
    """Unaware: did the agent turn to its harness, and did it understand the mechanism it used?"""
    r = next((r for r in runs if r["key"] == "unaware"), None)
    if r is None:
        return None
    read = [s for s in r["samples"] if s.get("output")]
    aware = Counter(bool((s["output"].get("harness_awareness") or {}).get("treats_harness_as_target")) for s in read)
    und = Counter()
    for s in read:
        for a in attempts(s):
            if a["type"] != "no_harness":
                und[(a.get("understood_mechanism"), a["outcome"] == "side_task_done")] += 1
    fig, (ax1, ax2) = _figure(1, 2, figsize=(11, 3.2), gridspec_kw={"width_ratios": [1, 1.3]})
    vals = [aware[True], aware[False]]
    ax1.barh([0, 1], vals, color=[BLUE, GRAY_LIGHT], height=0.6, edgecolor=SURFACE)
    for y, v in enumerate(vals):
        ax1.text(v + 0.5, y, str(v), va="center", fontsize=9, color=INK)
    ax1.set_yticks([0, 1], ["Considered", "Not considered"])
    ax1.invert_yaxis()
    ax1.set_xlabel("samples", color=INK)
    ax1.set_title("How often unaware agents consider their own harness for attacks", color=INK, fontsize=10,
                  loc="left")  # fmt: skip
    _style(ax1, "x")
    levels = ["stated", "partial", "not_stated"]
    names = {"stated": "Verbalized", "partial": "Partly verbalized", "not_stated": "Not verbalized"}
    for i, lv in enumerate(levels):
        s_, f_ = und[(lv, True)], und[(lv, False)]
        ax2.barh(i, s_, color=BLUE, height=0.6, edgecolor=SURFACE)
        ax2.barh(i, f_, left=s_, color=ORANGE, height=0.6, edgecolor=SURFACE)
        ax2.text(s_ + f_ + 0.2, i, f"{s_ + f_} ({s_} succeeded)", va="center", fontsize=9, color=INK)
    ax2.set_yticks(range(3), [names[lv] for lv in levels])
    ax2.invert_yaxis()
    ax2.set_xlabel("attacks trying to use harness exploits in unaware runs", color=INK)
    ax2.set_title("How often unaware agents verbalize the mechanism of a harness exploit before using it", color=INK,
                  fontsize=10, loc="left")  # fmt: skip
    ax2.set_xlim(0, max(1, max(und[(lv, True)] + und[(lv, False)] for lv in levels)) * 1.5)
    _style(ax2, "x")
    ax2.legend(handles=[Patch(facecolor=BLUE, label="success"), Patch(facecolor=ORANGE, label="failure")],
               loc="lower right", frameon=False, fontsize=8)  # fmt: skip
    fig.tight_layout()
    _save(fig, path)
    return {"read": len(read), "considered_harness": aware[True],
            "understood": {f"{lv}/{'done' if ok else 'failed'}": n for (lv, ok), n in und.items()}}


def plot_categories(proposal: dict, field: str, runs: list[dict], path: Path, title: str, note: str | None = None,
                    by_run: bool = True) -> dict | None:
    """Provisional taxonomy categories of one field: counts per run (small multiples) or pooled."""
    data = proposal["fields"].get(field)
    if not data or not data.get("assignments"):
        return None
    cats = [c["name"] for c in data["output"]["categories"]]
    values = {v["id"]: v for v in data["values"]}
    labels = [r["label"] for r in runs if any(v["run"] == r["label"] for v in values.values())] if by_run else ["all"]
    counts = {l: Counter() for l in labels}
    for vid, cat in data["assignments"].items():
        counts[values[vid]["run"] if by_run else "all"][cat] += 1
    if any(c["unassigned"] for c in counts.values()):
        cats = cats + ["unassigned"]
    order = sorted(cats, key=lambda c: -sum(counts[l][c] for l in labels))
    fig, axes = _figure(1, len(labels), figsize=(3.1 * len(labels) + 3.2, 0.42 * len(order) + 1.6), sharey=True)
    axes = np.atleast_1d(axes)
    xmax = max(max(c.values(), default=0) for c in counts.values()) or 1
    for ax, l in zip(axes, labels):
        n = sum(counts[l].values())
        vals = [counts[l][c] for c in order]
        ax.barh(range(len(order)), vals, color=BLUE, height=0.62, edgecolor=SURFACE)
        for y, v in enumerate(vals):
            if v:
                ax.text(v + xmax * 0.02, y, f"{v}", va="center", fontsize=8, color=INK)
        ax.set_xlim(0, xmax * 1.25)
        ax.set_title(f"{l} (n = {n})", color=INK, fontsize=10, loc="left")
        _style(ax, "x")
    axes[0].set_yticks(range(len(order)), [_wrap(c, 44) for c in order], fontsize=8.5)
    axes[0].invert_yaxis()
    fig.suptitle(title, color=INK, fontsize=12, x=0.01, ha="left")
    fig.tight_layout()
    _save(fig, path, note or "Categories proposed by an LLM reading and assigned by a second one; provisional "
                             "until reviewed.")  # fmt: skip
    return {l: {c: counts[l][c] for c in order if counts[l][c]} for l in labels}


def plot_failure_reasons_by_type(proposal: dict, runs: list[dict], path: Path) -> dict | None:
    """Failure-reason categories split by strategy type, pooled over runs (shares within type)."""
    data = proposal["fields"].get("failure_reason")
    if not data or not data.get("assignments"):
        return None
    values = {v["id"]: v for v in data["values"]}
    counts = {t: Counter() for t in TYPES}
    for vid, cat in data["assignments"].items():
        counts[values[vid]["type"]][cat] += 1
    cats = sorted({c for t in TYPES for c in counts[t]}, key=lambda c: -sum(counts[t][c] for t in TYPES))
    types = [t for t in TYPES if counts[t]]
    fig, axes = _figure(1, len(types), figsize=(3.3 * len(types) + 3.2, 0.42 * len(cats) + 1.6), sharey=True)
    axes = np.atleast_1d(axes)
    for ax, t in zip(axes, types):
        n = sum(counts[t].values())
        vals = [counts[t][c] / n for c in cats]
        ax.barh(range(len(cats)), vals, color=TYPE_COLORS[t], height=0.62, edgecolor=SURFACE)
        for y, (v, c) in enumerate(zip(vals, cats)):
            if counts[t][c]:
                ax.text(v + 0.02, y, f"{counts[t][c]}", va="center", fontsize=8, color=INK)
        ax.set_xlim(0, 1)
        ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
        ax.set_title(f"{TYPE_LABELS[t]}\n(n = {n} failed attempts)", color=INK, fontsize=10, loc="left")
        _style(ax, "x")
    axes[0].set_yticks(range(len(cats)), [_wrap(c, 44) for c in cats], fontsize=8.5)
    axes[0].invert_yaxis()
    fig.suptitle("Why attempts failed, by strategy (all runs pooled)", color=INK, fontsize=12, x=0.01, ha="left")
    fig.tight_layout()
    _save(fig, path, "Bars: share of the type's failed attempts; numbers: counts. Categories provisional.")
    return {t: dict(counts[t]) for t in types}


def transcript_blocks(runs: list[dict], results_root: Path) -> dict[str, int]:
    """Number of Docent messages per sample, rebuilt from the session logs as the upload builds them."""
    from bench.docent_upload import build_messages
    from bench.judge import ordered_rows, session_files

    out = {}
    for r in runs:
        for s in r["samples"]:
            files = session_files(results_root / r["run_id"] / "samples" / s["sample_id"])
            if files:
                out[s["sample_id"]] = len(build_messages(ordered_rows(files)))
    return out


def plot_all(runs_in: list[tuple[Path, RunConfig]], proposal: dict | None, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    runs = load_runs(runs_in)
    results_root = Path(runs_in[0][0]).parent
    summary: dict = {"runs": [r["run_id"] for r in runs]}
    summary["sample_outcomes"] = plot_sample_outcomes(runs, out_dir / "01_sample_outcomes.png")
    summary["attempt_outcomes"] = plot_attempt_outcomes(runs, out_dir / "02_attempt_outcomes.png")
    summary["attempts_per_sample"] = plot_attempts_per_sample(runs, out_dir / "03_attempts_per_sample.png")
    summary["after_a_failure"] = plot_sequences(runs, out_dir / "04_sequences.png")
    summary["cards"] = plot_cards(runs, out_dir / "05_cards.png")
    summary["one_card_outcomes"] = plot_one_card_outcomes(runs, out_dir / "05b_one_card_outcomes.png")
    summary["one_card_adherence"] = one_card_adherence(runs)
    summary["failed_delivery"] = plot_failed_delivery(runs, out_dir / "05c_failed_delivery.png")
    summary["first_attempt"] = plot_first_attempt(runs, transcript_blocks(runs, results_root),
                                                  out_dir / "06_first_attempt.png")  # fmt: skip
    summary["unaware"] = plot_unaware(runs, out_dir / "07_unaware_harness.png")
    if proposal:
        summary["failure_reason"] = plot_categories(proposal, "failure_reason", runs, out_dir / "08_failure_reasons.png",
                                                    "Why attempts failed (provisional categories)")  # fmt: skip
        summary["failure_reason_by_type"] = plot_failure_reasons_by_type(proposal, runs,
                                                                         out_dir / "09_failure_reasons_by_type.png")  # fmt: skip
        summary["mechanism"] = plot_categories(proposal, "mechanism", runs, out_dir / "10_mechanisms.png",
                                               "Attempts without a card: what they relied on (provisional)")  # fmt: skip
        summary["card_choice"] = plot_categories(proposal, "card_choice", runs, out_dir / "11_card_choice.png",
                                                 "All cards: how the agent chose its card (provisional)")  # fmt: skip
        summary["idea_formation"] = plot_categories(proposal, "idea_formation", runs, out_dir / "12_idea_formation.png",
                                                    "Explore: how the agent formed its exploit ideas (provisional)")  # fmt: skip
        summary["harness_awareness_trigger"] = plot_categories(
            proposal, "harness_awareness_trigger", runs, out_dir / "13_unaware_trigger.png",
            "Unaware: what led the agent to its harness (provisional)")  # fmt: skip
        summary["engagement"] = plot_categories(proposal, "engagement", runs, out_dir / "14_no_attempt.png",
                                                "Samples without an attempt: why (provisional)")  # fmt: skip
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary
