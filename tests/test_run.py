"""bench.run.run_experiment end to end through real Inspect eval sets, without Docker: the solver and the scorers
are replaced by fakes (the solver writes the files meltzer would leave), and the reflection's HTTP calls are mocked.

Checks the order of the phases, wave isolation, the boards, the reflection input, the execution and ceiling
prompts, the results layout, `samples.jsonl`, that a failed search experiment is not run again while a failed
execution sample is, and that calling the run again (a resume) runs nothing new.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import httpx
import pytest
from inspect_ai.scorer import CORRECT, INCORRECT, Score, accuracy, mean, scorer
from inspect_ai.solver import Generate, TaskState, solver

from bench import basharena, run, search
from bench import task as bench_task
from bench.config import REPO_ROOT, load_config

CONFIG = REPO_ROOT / "tests" / "fixtures" / "exp3" / "config.yaml"
IDS = basharena.pool_ids()[:3]  # two search main tasks, one test main task
SIDES = ("leak_passwords", "sudo_edit")
SEEN: list[dict] = []  # metadata of every sample the fake solver ran, in order
FAILED_ONCE: set[str] = set()
BROKEN_EXPERIMENT = "tier3/search/wave1/exp2"  # raises: an infrastructure error
STEP_CAPPED = "tier1/search/wave1/exp2"  # ends at the step cap
FLAKY_EXECUTION = "tier1/execution/"  # the first tier-1 execution sample raises once
SESSION = "meltzer/sessions/2026/11/28/session.jsonl"


@solver
def fake_meltzer(cfg, run_dir, model_factory=None):
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        meta = dict(state.metadata)
        SEEN.append(meta)
        folder = run_dir / meta["dir"]
        folder.mkdir(parents=True, exist_ok=True)
        if meta["dir"] == BROKEN_EXPERIMENT:
            raise RuntimeError("the stack did not start")
        if meta["dir"].startswith(FLAKY_EXECUTION) and not FAILED_ONCE:
            FAILED_ONCE.add(meta["dir"])
            raise RuntimeError("a flaky stack")
        capped = meta["dir"] == STEP_CAPPED
        text = "Stopped: the turn reached max_turn_steps (40 model requests).\n" if capped else f"LESSONS of {meta['dir']}\n"
        (folder / "stdout.txt").write_text(text, encoding="utf-8")
        (folder / "stderr.txt").write_text("", encoding="utf-8")
        (folder / SESSION).parent.mkdir(parents=True, exist_ok=True)
        (folder / SESSION).write_text(json.dumps({"type": "session_meta"}) + "\n", encoding="utf-8")
        end = "max_turn_steps" if capped else "final_answer"
        state.store.set("meltzer", {"exit_code": 0, "time_limit_hit": False, "end_reason": end, "wait_seconds": 0.1})
        return state

    return solve


@scorer(metrics=[accuracy()], name="side_task_scorer")
def fake_side_scorer():
    async def score(state, target):
        return Score(value=CORRECT if state.metadata["side_task"] == "sudo_edit" else INCORRECT, explanation="checked")

    return score


@scorer(metrics=[mean()], name="main_task_scorer")
def fake_main_scorer():
    async def score(state, target):
        return Score(value=0.5, explanation="1/2")

    return score


class Reflection:
    def __init__(self):
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        text = json.loads(request.content)["messages"][0]["content"]
        self.requests.append(text)
        tier = re.search(r"MARK-TIERS-TIER(\d)", text).group(1)
        content = f"STRATEGY-OF-TIER-{tier}"
        return httpx.Response(200, json={"id": "r", "provider": "Wafer", "choices": [{"message": {"content": content}}]})


@pytest.fixture(scope="module")
def outcome(tmp_path_factory):
    run_dir = tmp_path_factory.mktemp("run")
    (run_dir / "preflight.json").write_text(json.dumps({"kept": IDS, "skipped": []}), encoding="utf-8")
    base = load_config(CONFIG)
    cfg = dataclasses.replace(
        base,
        setting=dataclasses.replace(
            base.setting, side_tasks=SIDES,
            main_tasks=dataclasses.replace(base.setting.main_tasks, search_n=2, test_n=1),
        ),
        tiers=(1, 3),
        search=dataclasses.replace(base.search, waves=2, per_wave=2),
        parallel=dataclasses.replace(base.parallel, max_samples=4),
    )  # fmt: skip
    reflection = Reflection()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("INSPECT_DISPLAY", "none")
        mp.setattr(bench_task, "meltzer_solver", fake_meltzer)
        mp.setattr(bench_task.basharena, "side_task_scorer", fake_side_scorer)
        mp.setattr(bench_task.basharena, "main_task_scorer", fake_main_scorer)
        mp.setattr(run, "workspaces", lambda cfg, run_dir, ids: {task_id: None for task_id in ids})
        options = dict(eval_model="mockllm/model", reflection_transport=httpx.MockTransport(reflection), api_key="k")
        ids = run.run_experiment(cfg, run_dir, **options)
        first = list(SEEN)
        rows = (run_dir / "samples.jsonl").read_text(encoding="utf-8")
        again = run.run_experiment(cfg, run_dir, **options)  # what `resume` does on a finished run
    return dict(cfg=cfg, run_dir=run_dir, ids=ids, again=again, first=first, seen=list(SEEN), rows=rows,
                reflection=reflection)  # fmt: skip


def rows(outcome) -> dict[str, dict]:
    return {r["sample"]: r for r in map(json.loads, outcome["rows"].splitlines())}


def test_main_tasks_split_into_search_and_test(outcome):
    assert outcome["ids"] == {"search": IDS[:2], "test": IDS[2:]} == outcome["again"]


def test_phases_run_in_order_waves_one_after_the_other_tiers_side_by_side(outcome):
    order = [(m["phase"], m["wave"]) for m in outcome["first"]]
    stages = [("search", 1), ("search", 2), ("execution", None)]
    assert [s for i, s in enumerate(order) if i == 0 or order[i - 1] != s] == stages
    assert {m["tier"] for m in outcome["first"] if m["wave"] == 1} == {1, 3}


def test_search_pairs_are_the_same_in_every_tier(outcome):
    by_tier = {k: [(m["wave"], m["experiment"], m["main_task"], m["side_task"]) for m in outcome["first"] if m["phase"] == "search" and m["tier"] == k] for k in (1, 3)}
    assert sorted(by_tier[1]) == sorted(by_tier[3]) and len(by_tier[1]) == 4
    assert {(main, side) for _, _, main, side in by_tier[1]} <= {(m, s) for m in IDS[:2] for s in SIDES}


def test_wave_two_sees_the_board_of_wave_one_of_its_own_tier_only(outcome):
    for m in outcome["first"]:
        text = m["developer_instructions"]
        if m["phase"] != "search":
            continue
        if m["wave"] == 1:
            assert "## Entry" not in text and "LESSONS" not in text
        else:
            k = m["tier"]
            assert f"## Entry 1.1\n\nLESSONS of tier{k}/search/wave1/exp1" in text
            assert "wave2" not in text and f"tier{4 - k}/search" not in text  # not tier 1 <-> 3


def test_boards_lessons_and_scores(outcome):
    run_dir = outcome["run_dir"]
    board1 = (run_dir / "tier1" / "search" / "board.md").read_text(encoding="utf-8")
    assert board1 == (
        "## Entry 1.1\n\nLESSONS of tier1/search/wave1/exp1\n\n## Entry 1.2\n\n(no lessons)\n\n"
        "## Entry 2.1\n\nLESSONS of tier1/search/wave2/exp1\n\n## Entry 2.2\n\nLESSONS of tier1/search/wave2/exp2\n"
    )
    board3 = (run_dir / "tier3" / "search" / "board.md").read_text(encoding="utf-8")
    assert "## Entry 1.2\n\n## Entry 2.1" in board3  # the broken experiment's entry is empty
    exp = run_dir / "tier1" / "search" / "wave2" / "exp1"
    assert (exp / "lessons.md").read_text(encoding="utf-8") == "LESSONS of tier1/search/wave2/exp1\n"
    assert json.loads((exp / "scores.json").read_text(encoding="utf-8"))["main_score"] == 0.5


def test_a_failed_search_experiment_is_not_run_again_but_a_failed_execution_sample_is(outcome):
    runs = [m["dir"] for m in outcome["first"]]
    assert runs.count(BROKEN_EXPERIMENT) == 1
    assert rows(outcome)[BROKEN_EXPERIMENT]["status"] == "infra_error"
    (flaky,) = FAILED_ONCE
    assert runs.count(flaky) == 2 and rows(outcome)[flaky]["status"] == "ok"


def test_reflection_reads_the_full_board_of_its_tier(outcome):
    run_dir, cfg = outcome["run_dir"], outcome["cfg"]
    requests = outcome["reflection"].requests
    assert len(requests) == 2
    for k in (1, 3):
        board = (run_dir / f"tier{k}" / "search" / "board.md").read_text(encoding="utf-8").rstrip("\n")
        assert search.reflection_text(cfg, k, board) in requests
        assert (run_dir / f"tier{k}" / "strategy.md").read_text(encoding="utf-8") == f"STRATEGY-OF-TIER-{k}"
        assert (run_dir / f"tier{k}" / "reflection.json").is_file()


def test_execution_gets_the_strategy_and_ceilings_the_ceiling_text(outcome):
    execution = [m for m in outcome["first"] if m["phase"] == "execution"]
    groups = {m["group"] for m in execution}
    assert groups == {"tier1", "tier3", "ceiling1", "ceiling3"}
    for m in execution:
        text = m["developer_instructions"]
        k = m["tier"]
        if m["group"].startswith("ceiling"):
            assert f"MARK-CEILINGS-TIER{k}" in text and "STRATEGY-OF-TIER" not in text
        else:
            assert f"STRATEGY-OF-TIER-{k}" in text and "MARK-CEILINGS" not in text
        assert "LESSONS" not in text and "## Entry" not in text  # execution does not see the board
        assert m["dir"] == f"{m['group']}/execution/{m['main_task']}__{m['side_task']}"


def test_samples_jsonl_has_every_sample_once(outcome):
    table = rows(outcome)
    assert len(table) == 2 * 4 + 4 * 2  # tiers x experiments, (tiers + ceilings) x test pairs
    assert list(table) == sorted(table)
    row = table["ceiling3/execution/" + f"{IDS[2]}__sudo_edit"]
    assert (row["phase"], row["group"], row["tier"], row["side_pass"], row["main_score"]) == ("execution", "ceiling3", 3, True, 0.5)


def test_one_log_folder_per_eval_set(outcome):
    logs = outcome["run_dir"] / "inspect_logs"
    assert sorted(p.name for p in logs.iterdir()) == ["execution", "search-wave1", "search-wave2"]
    assert len(list((logs / "execution").glob("*.eval"))) == 4
    assert len(list((logs / "search-wave1").glob("*.eval"))) == 2


def test_calling_the_run_again_runs_nothing_new(outcome):
    assert outcome["seen"] == outcome["first"]  # no sample ran again
    assert len(outcome["reflection"].requests) == 2  # the strategies were kept
    assert (outcome["run_dir"] / "samples.jsonl").read_text(encoding="utf-8") == outcome["rows"]


# ---------------------------------------------------------------- checks before anything runs


def small_cfg(**changes):
    base = load_config(CONFIG)
    return dataclasses.replace(base, tiers=(2,), **changes)


@pytest.mark.parametrize(
    "missing", ["base.md", "experiment.md", "reflection.md", "execution.md", "tiers/tier2.md", "ceilings/tier2.md"]
)
def test_a_missing_prompt_file_stops_the_run_before_anything_runs(tmp_path, monkeypatch, missing):
    import shutil

    prompts_dir = tmp_path / "prompts"
    shutil.copytree(REPO_ROOT / "tests" / "fixtures" / "exp3" / "prompts", prompts_dir)
    (prompts_dir / missing).unlink()
    cfg = small_cfg(prompts=dataclasses.replace(load_config(CONFIG).prompts, dir=str(prompts_dir)))
    monkeypatch.setattr(run, "workspaces", lambda *a: pytest.fail("the run went on"))
    with pytest.raises(run.InputError, match=re.escape(Path(missing).name)):
        run.run_experiment(cfg, tmp_path / "run")
    assert not (tmp_path / "run").exists()


def test_the_ceiling_texts_are_needed_only_with_ceilings(tmp_path):
    import shutil

    prompts_dir = tmp_path / "prompts"
    shutil.copytree(REPO_ROOT / "tests" / "fixtures" / "exp3" / "prompts", prompts_dir)
    shutil.rmtree(prompts_dir / "ceilings")
    base = load_config(CONFIG)
    cfg = small_cfg(
        prompts=dataclasses.replace(base.prompts, dir=str(prompts_dir)),
        execution=dataclasses.replace(base.execution, ceilings=False),
    )
    run.check_inputs(cfg)


@pytest.mark.parametrize("docs", ["no-such-folder", "empty"])
def test_missing_docs_stop_the_run(tmp_path, docs):
    (tmp_path / "empty").mkdir()
    cfg = small_cfg(materials=dataclasses.replace(load_config(CONFIG).materials, docs=str(tmp_path / docs)))
    with pytest.raises(run.InputError, match="no docs"):
        run.check_inputs(cfg)


def test_a_tier_whose_experiments_all_failed_stops_before_the_reflection(tmp_path, monkeypatch):
    """No session in a tier: the run stops before paying for the reflections and the execution."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "preflight.json").write_text(json.dumps({"kept": IDS}), encoding="utf-8")
    base = load_config(CONFIG)
    cfg = dataclasses.replace(
        base, tiers=(2,), setting=dataclasses.replace(
            base.setting, side_tasks=("sudo_edit",),
            main_tasks=dataclasses.replace(base.setting.main_tasks, search_n=1, test_n=1),
        ),
        search=dataclasses.replace(base.search, waves=1, per_wave=1),
    )  # fmt: skip
    monkeypatch.setattr(run, "workspaces", lambda cfg, run_dir, ids: {task_id: None for task_id in ids})
    monkeypatch.setattr(bench_task, "run_tasks", lambda cfg, run_dir, name, tasks, **k: ([], None))
    monkeypatch.setattr(search, "run_reflections", lambda *a, **k: pytest.fail("the reflection was called"))
    with pytest.raises(RuntimeError, match="no search experiment of tier 2 produced a meltzer session"):
        run.run_experiment(cfg, run_dir)


def test_example_prompts_cover_every_role_and_tier(outcome, tmp_path):
    written = run.write_example_prompts(outcome["cfg"], tmp_path, IDS[0], "sudo_edit")
    names = sorted(p.name for p in written)
    assert names == sorted(
        ["user_message.md"]
        + [f"tier{k}_{r}.md" for k in (1, 3) for r in ("experiment_wave1", "experiment_wave2", "reflection", "execution")]
        + [f"ceiling{k}_execution.md" for k in (1, 3)]
    )
    wave2 = (tmp_path / "tier3_experiment_wave2.md").read_text(encoding="utf-8")
    assert "## Entry 1.1" in wave2 and "MARK-TIERS-TIER3" in wave2 and "(yours)" in wave2
    assert "(yours)" not in (tmp_path / "tier1_reflection.md").read_text(encoding="utf-8")
