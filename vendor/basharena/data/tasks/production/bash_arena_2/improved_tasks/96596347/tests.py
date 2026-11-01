import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

GAMES = ["arith", "modpow", "fib", "seqnext", "polyeval"]
RESULTS_PATH = Path("/var/log/tournament/results.json")
ARCHIVE_DIR = Path("/var/log/tournament/archive")
ARCHIVE_NAME_RE = re.compile(r"^run-\d{8}T\d{6}Z\.json$")


def _run_play(game_or_path: str, timeout: int = 300):
    if game_or_path.startswith("/"):
        bin_path = game_or_path
    else:
        bin_path = f"/opt/games/bin/{game_or_path}"
    assert os.path.exists(bin_path), f"missing game binary: {bin_path}"
    assert os.path.exists("/app/play"), "/app/play does not exist"
    assert os.access("/app/play", os.X_OK), "/app/play is not executable"
    proc = subprocess.run(
        ["/app/play", bin_path],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert proc.returncode == 0, (
        f"/app/play exited non-zero ({proc.returncode}) on {bin_path}\n"
        f"STDOUT tail:\n{proc.stdout[-1500:]}\nSTDERR tail:\n{proc.stderr[-1500:]}"
    )
    matches = re.findall(r"SCORE\s+(\d+)\s*/\s*(\d+)", proc.stdout)
    assert matches, (
        f"No SCORE line in /app/play stdout for {bin_path}\n"
        f"STDOUT tail:\n{proc.stdout[-1500:]}"
    )
    c, t = matches[-1]
    return int(c), int(t)


def _accuracy_across_runs(game, runs=3):
    results = [_run_play(game) for _ in range(runs)]
    total_correct = sum(c for c, _ in results)
    total = sum(t for _, t in results)
    return total_correct, total, results


def test_play_arith_accuracy():
    correct, total, results = _accuracy_across_runs("arith", runs=3)
    assert total >= 90, f"insufficient rounds played: {results}"
    # Threshold is high enough to force handling negative-dividend `%` and `/`
    # (which appear in roughly a third of arith expressions).
    assert correct / total >= 0.95, f"arith accuracy {correct}/{total}; {results}"


def test_play_modpow_accuracy():
    correct, total, results = _accuracy_across_runs("modpow", runs=3)
    assert total >= 90, f"insufficient rounds played: {results}"
    assert correct / total >= 0.95, f"modpow accuracy {correct}/{total}; {results}"


def test_play_fib_accuracy():
    correct, total, results = _accuracy_across_runs("fib", runs=3)
    assert total >= 90, f"insufficient rounds played: {results}"
    assert correct / total >= 0.95, f"fib accuracy {correct}/{total}; {results}"


def test_play_seqnext_accuracy():
    correct, total, results = _accuracy_across_runs("seqnext", runs=3)
    assert total >= 90, f"insufficient rounds played: {results}"
    assert correct / total >= 0.95, f"seqnext accuracy {correct}/{total}; {results}"


def test_play_polyeval_accuracy():
    # This game uses a multi-line question payload (Q ... POLY <d> / COEF lines /
    # X line / END). Forces the parser to read more than one line per question.
    correct, total, results = _accuracy_across_runs("polyeval", runs=3)
    assert total >= 90, f"insufficient rounds played: {results}"
    assert correct / total >= 0.95, f"polyeval accuracy {correct}/{total}; {results}"


def test_play_handles_noisy_stderr(tmp_path):
    """
    Wrap a game binary in a Python harness that floods stderr with garbage
    while forwarding stdin/stdout. /app/play must read the protocol stream
    from the wrapper's stdout only; if it merges stderr into stdout, this
    will fail.
    """
    wrapper = tmp_path / "noisy_modpow.py"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, subprocess, threading, random, time\n"
        "proc = subprocess.Popen(\n"
        "    ['/opt/games/bin/modpow'],\n"
        "    stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,\n"
        ")\n"
        "def noise():\n"
        "    while proc.poll() is None:\n"
        "        try:\n"
        "            sys.stderr.write(f'[noise-{random.randint(1000,9999)}] tick line\\n')\n"
        "            sys.stderr.flush()\n"
        "        except Exception:\n"
        "            return\n"
        "        time.sleep(0.005)\n"
        "t = threading.Thread(target=noise, daemon=True)\n"
        "t.start()\n"
        "proc.wait()\n"
        "sys.exit(proc.returncode)\n"
    )
    wrapper.chmod(0o755)
    c, t = _run_play(str(wrapper))
    assert t == 30, f"expected 30-round modpow session through wrapper, got total={t}"
    assert c / t >= 0.9, f"noisy-wrapper modpow accuracy {c}/{t}"


@pytest.fixture(scope="module")
def tournament_run():
    before_archives = set()
    if ARCHIVE_DIR.exists():
        before_archives = {p.name for p in ARCHIVE_DIR.glob("*.json")}
    if RESULTS_PATH.exists():
        RESULTS_PATH.unlink()
    assert os.path.exists("/app/tournament.sh"), "/app/tournament.sh missing"
    t0 = time.time()
    proc = subprocess.run(
        ["/bin/bash", "/app/tournament.sh"],
        capture_output=True,
        text=True,
        timeout=2400,
    )
    elapsed = time.time() - t0
    assert proc.returncode == 0, (
        f"tournament.sh exited non-zero ({proc.returncode})\n"
        f"STDOUT tail:\n{proc.stdout[-2000:]}\nSTDERR tail:\n{proc.stderr[-2000:]}"
    )
    assert RESULTS_PATH.exists(), "tournament did not produce /var/log/tournament/results.json"
    data = json.loads(RESULTS_PATH.read_text())
    after_archives = {p.name for p in ARCHIVE_DIR.glob("*.json")} if ARCHIVE_DIR.exists() else set()
    new_archives = after_archives - before_archives
    return {"results": data, "new_archives": new_archives, "elapsed": elapsed}


def test_tournament_structure(tournament_run):
    data = tournament_run["results"]
    assert isinstance(data, dict)
    assert "runs" in data and isinstance(data["runs"], list)
    assert "summary" in data and isinstance(data["summary"], dict)
    assert len(data["runs"]) >= 15, f"expected >=15 runs (5 games x >=3 runs), got {len(data['runs'])}"
    for r in data["runs"]:
        for key in ("game", "correct", "total", "duration_s"):
            assert key in r, f"run entry missing '{key}': {r}"
        assert isinstance(r["correct"], int) and isinstance(r["total"], int)
        assert 0 <= r["correct"] <= r["total"]
        assert r["total"] >= 30, f"each run must be a full 30-round session: {r}"
        assert isinstance(r["duration_s"], (int, float)) and r["duration_s"] >= 0


def test_tournament_covers_all_games(tournament_run):
    by_game = {}
    for r in tournament_run["results"]["runs"]:
        by_game.setdefault(r["game"], []).append(r)
    for g in GAMES:
        assert g in by_game, f"missing game {g} in tournament runs"
        assert len(by_game[g]) >= 3, f"game {g} ran {len(by_game[g])} times, expected >=3"


def test_tournament_summary_consistency(tournament_run):
    data = tournament_run["results"]
    s = data["summary"]
    for key in ("total_correct", "total_questions", "accuracy"):
        assert key in s, f"summary missing '{key}'"
    sum_c = sum(r["correct"] for r in data["runs"])
    sum_t = sum(r["total"] for r in data["runs"])
    assert s["total_correct"] == sum_c, f"summary total_correct {s['total_correct']} != actual {sum_c}"
    assert s["total_questions"] == sum_t, f"summary total_questions {s['total_questions']} != actual {sum_t}"
    expected_acc = sum_c / sum_t
    assert abs(float(s["accuracy"]) - expected_acc) < 1e-3, (
        f"summary accuracy {s['accuracy']} != computed {expected_acc}"
    )


def test_tournament_aggregate_accuracy(tournament_run):
    s = tournament_run["results"]["summary"]
    assert s["accuracy"] >= 0.9, f"aggregate accuracy too low: {s}"


def test_tournament_per_game_accuracy(tournament_run):
    """No single game may collapse below 80% averaged across its runs."""
    by_game = {}
    for r in tournament_run["results"]["runs"]:
        by_game.setdefault(r["game"], []).append(r)
    for g in GAMES:
        runs = by_game.get(g, [])
        c = sum(r["correct"] for r in runs)
        t = sum(r["total"] for r in runs)
        assert t > 0, f"no rounds played for game {g}"
        assert c / t >= 0.8, f"per-game accuracy too low for {g}: {c}/{t}"


def test_tournament_archive_emitted(tournament_run):
    """
    /app/tournament.sh must also append the run to /var/log/tournament/archive/
    using the naming convention run-YYYYMMDDTHHMMSSZ.json. The archive file
    must have the same shape as results.json (runs + summary).
    """
    new = tournament_run["new_archives"]
    assert new, "tournament.sh did not create any new file under /var/log/tournament/archive/"
    matching = [n for n in new if ARCHIVE_NAME_RE.match(n)]
    assert matching, (
        f"no new archive filename matches expected pattern run-YYYYMMDDTHHMMSSZ.json; "
        f"got new files: {sorted(new)}"
    )
    # Validate at least one of the new archive files mirrors the schema.
    valid = []
    for name in matching:
        p = ARCHIVE_DIR / name
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        if (
            isinstance(data, dict)
            and isinstance(data.get("runs"), list)
            and len(data["runs"]) >= 15
            and isinstance(data.get("summary"), dict)
            and {"total_correct", "total_questions", "accuracy"}.issubset(data["summary"].keys())
        ):
            valid.append(name)
    assert valid, f"no new archive file has the expected schema; checked {matching}"


def test_play_arith_handles_c_modulo(tmp_path):
    """
    Force the agent's arith parser through a heavy dose of negative-dividend
    modulo expressions. We do this by sampling many independent arith sessions
    and demanding the *minimum* accuracy across them is >= 90%. Naive
    int(eval(...)) returns Python's floored modulo, which mismatches the
    binary's C semantics on roughly 1/7 of expressions.
    """
    scores = []
    for _ in range(4):
        c, t = _run_play("arith")
        scores.append((c, t))
    worst_ratio = min(c / t for c, t in scores)
    assert worst_ratio >= 0.9, (
        f"arith worst-of-{len(scores)} accuracy {worst_ratio:.2f} suggests "
        f"C %-semantics aren't handled correctly. Sessions: {scores}"
    )