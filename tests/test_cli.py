"""Tests for the CLI (bench.__main__): summary, judge/analyze dispatch, run/preflight, missing modules."""

import json
import subprocess
import sys
import types
from pathlib import Path

import pytest
import yaml

from bench import config
from bench.__main__ import main
from bench.config import load_config

REPO = Path(__file__).resolve().parent.parent
PILOT = REPO / "runs" / "exp1-pilot.yaml"


def make_config(
    tmp_path, run_id="exp1-pilot", bootstrap=0, agent="a/agent", monitor="a/monitor", name="cfg.yaml", main_seed=0
):
    data = yaml.safe_load(PILOT.read_text(encoding="utf-8"))
    data["run_id"] = run_id
    data["seeds"]["bootstrap"] = bootstrap
    data["setting"]["main_tasks"]["seed"] = main_seed
    data["models"]["agent"]["id"] = agent
    data["models"]["monitor"]["id"] = monitor
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def ci(value, low, high):
    return {"value": value, "ci": [low, high]}


def make_results_dir(root, run_id, cfg_path=None, summary=None):
    d = root / run_id
    d.mkdir(parents=True)
    if cfg_path is not None:
        (d / "config.yaml").write_bytes(Path(cfg_path).read_bytes())
    if summary is not None:
        (d / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return d


FULL_SUMMARY = {
    "run_id": "run-a",
    "models": {"agent": "z-ai/glm-5.3", "monitor": "z-ai/glm-5.3-flash"},
    "n_samples": 240,
    # same layout as bench.analysis: per side task entries plus "average"; summary reads only "average"
    "metrics": {
        "asr_unaware": {"leak_passwords": ci(0.9, 0.8, 1.0), "average": ci(0.25, 0.2, 0.3)},
        "asr_aware_strict": {"leak_passwords": ci(0.9, 0.8, 1.0), "average": ci(0.4, 0.35, 0.45)},
        "asr_aware_scorer": {"leak_passwords": ci(0.9, 0.8, 1.0), "average": ci(0.5, 0.44, 0.56)},
    },
    "gaps": {
        "asr_aware_strict": {"leak_passwords": ci(0.9, 0.8, 1.0), "average": ci(0.15, 0.05, 0.25)},
        "asr_aware_scorer": {"leak_passwords": ci(0.9, 0.8, 1.0), "average": ci(-0.1, -0.2, 0.0)},
    },
}


@pytest.fixture
def results(tmp_path, monkeypatch):
    root = tmp_path / "results"
    root.mkdir()
    monkeypatch.setattr(config, "RESULTS_ROOT", root)
    return root


def fake_module(monkeypatch, name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def missing_module(monkeypatch, name):
    monkeypatch.setitem(sys.modules, name, None)  # makes `import name` raise ModuleNotFoundError
    # `from bench import x` also finds x as an attribute of the package once another test imported it.
    package, _, attr = name.rpartition(".")
    if package in sys.modules and sys.modules[package] is not None:
        monkeypatch.delattr(sys.modules[package], attr, raising=False)


# ---------------------------------------------------------------- summary


def test_summary_prints_one_row_per_run(tmp_path, capsys):
    cfg_a = make_config(tmp_path, "run-a", name="a.yaml")
    cfg_b = make_config(tmp_path, "run-b", agent="x/other-agent", monitor="x/other-monitor", name="b.yaml")
    second = {
        **FULL_SUMMARY,
        "run_id": "run-b",
        "models": {"agent": "x/other-agent", "monitor": "x/other-monitor"},
        "n_samples": 16,
        "metrics": {
            "asr_unaware": {"average": ci(0.1, 0.0, 0.2)},
            "asr_aware_strict": {"average": ci(0.2, 0.1, 0.3)},
            "asr_aware_scorer": {"average": ci(0.3, 0.2, 0.4)},
        },
        "gaps": {
            "asr_aware_strict": {"average": ci(0.1, -0.05, 0.25)},
            "asr_aware_scorer": {"average": ci(0.2, 0.1, 0.3)},
        },
    }
    da = make_results_dir(tmp_path / "r", "run-a", cfg_a, FULL_SUMMARY)
    db = make_results_dir(tmp_path / "r", "run-b", cfg_b, second)

    assert main(["summary", str(da), str(db)]) == 0
    lines = capsys.readouterr().out.splitlines()

    assert len(lines) == 3  # header + two rows
    header = lines[0]
    for col in ("run", "agent", "monitor", "n", "asr_unaware", "asr_aware_strict", "asr_aware_scorer", "gap_strict", "gap_scorer"):
        assert col in header
    row_a, row_b = lines[1], lines[2]
    assert row_a.startswith("run-a")
    assert "z-ai/glm-5.3" in row_a and "z-ai/glm-5.3-flash" in row_a
    assert "240" in row_a
    assert "0.25 [0.20, 0.30]" in row_a
    assert "0.40 [0.35, 0.45]" in row_a
    assert "0.50 [0.44, 0.56]" in row_a
    assert "+0.15 [+0.05, +0.25]" in row_a
    assert "-0.10 [-0.20, +0.00]" in row_a
    assert row_b.startswith("run-b")
    assert "x/other-agent" in row_b and "x/other-monitor" in row_b
    assert "16" in row_b
    assert "0.10 [0.00, 0.20]" in row_b


def test_summary_columns_are_aligned(tmp_path, capsys):
    cfg = make_config(tmp_path, "run-a")
    d1 = make_results_dir(tmp_path / "r", "run-a", cfg, FULL_SUMMARY)
    d2 = make_results_dir(tmp_path / "r", "a-much-longer-run-id", cfg, FULL_SUMMARY)
    main(["summary", str(d1), str(d2)])
    lines = capsys.readouterr().out.splitlines()
    starts = {line.index("z-ai/glm-5.3 ") for line in lines[1:]}
    assert len(starts) == 1


def test_summary_missing_keys_print_dash(tmp_path, capsys):
    cfg = make_config(tmp_path, "run-m", agent="m/agent", monitor="m/monitor")
    partial = {
        "run_id": "run-m",
        "metrics": {
            "asr_unaware": {"average": ci(0.5, 0.4, 0.6)},
            "asr_aware_strict": {"average": {"value": 0.7}},
        },
        "gaps": {},
    }
    d = make_results_dir(tmp_path / "r", "run-m", cfg, partial)
    assert main(["summary", str(d)]) == 0
    row = capsys.readouterr().out.splitlines()[1].split()
    assert row[0] == "run-m"
    # models fall back to config.yaml; n_samples missing
    assert "m/agent" in row and "m/monitor" in row
    out = " ".join(row)
    assert "0.50 [0.40, 0.60]" in out
    assert "0.70" in out and "0.70 [" not in out  # value without interval
    assert row.count("-") >= 4  # n, asr_aware_scorer, gap_strict, gap_scorer


def test_summary_null_interval_prints_value_only(tmp_path, capsys):
    """bench.analysis writes null for undefined numbers (e.g. an interval that could not be computed)."""
    summary = {
        "run_id": "run-n",
        "metrics": {"asr_unaware": {"average": {"value": 0.5, "ci": [None, None]}}},
        "gaps": {"asr_aware_strict": {"average": {"value": None, "ci": [None, None]}}},
    }
    d = make_results_dir(tmp_path / "r", "run-n", None, summary)
    assert main(["summary", str(d)]) == 0
    row = capsys.readouterr().out.splitlines()[1]
    assert "0.50" in row and "0.50 [" not in row
    assert "None" not in row


def test_summary_reads_real_analysis_output(tmp_path, capsys):
    """End to end on the real summary.json written by bench.analysis.analyze_run."""
    from bench.analysis import analyze_run

    run = tmp_path / "r" / "real"
    run.mkdir(parents=True)
    cfg = make_config(tmp_path, "real", agent="r/agent", monitor="r/monitor")
    (run / "config.yaml").write_bytes(cfg.read_bytes())
    side_tasks = ["download_malware", "leak_passwords", "disable_firewall", "sudo_edit"]
    rows = []
    for m in range(6):
        for j, side in enumerate(side_tasks):
            for cond in ("unaware", "aware"):
                passed = (m + j) % 2 == 0 or cond == "aware"
                rows.append(
                    {
                        "sample_id": f"{cond}__{m:03d}__{side}",
                        "condition": cond,
                        "main_task": f"{m:03d}",
                        "side_task": side,
                        "status": "ok",
                        "side_pass": passed,
                        "judge": {"faithful": passed, "card_id": "E01"} if cond == "aware" and passed else None,
                    }
                )
    lines = [json.dumps(r) for r in rows]
    (run / "samples.jsonl").write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    analyze_run(run, bootstrap_seed=0, n_boot=200)

    assert main(["summary", str(run)]) == 0
    row = capsys.readouterr().out.splitlines()[1]
    assert row.split()[0] == "real"
    assert "r/agent" in row and "r/monitor" in row
    assert " 48 " in row  # n_samples: 6 main tasks x 4 side tasks x 2 conditions
    assert "-" not in row.split()  # no missing cells
    assert row.count("[") == 5  # three ASR intervals and two gap intervals


def test_summary_empty_summary_and_no_config(tmp_path, capsys):
    d = make_results_dir(tmp_path / "r", "bare", None, {})
    assert main(["summary", str(d)]) == 0
    row = capsys.readouterr().out.splitlines()[1].split()
    assert row[0] == "bare"
    assert set(row[1:]) == {"-"}


def test_summary_missing_summary_json_warns_and_prints_row(tmp_path, capsys):
    cfg = make_config(tmp_path, "no-sum")
    d = make_results_dir(tmp_path / "r", "no-sum", cfg, None)
    assert main(["summary", str(d)]) == 0
    out, err = capsys.readouterr()
    assert out.splitlines()[1].split()[0] == "no-sum"
    assert "summary.json" in err


def test_summary_unreadable_summary_json_warns(tmp_path, capsys):
    d = make_results_dir(tmp_path / "r", "bad", None, None)
    (d / "summary.json").write_text("{not json", encoding="utf-8")
    assert main(["summary", str(d)]) == 0
    out, err = capsys.readouterr()
    assert out.splitlines()[1].split()[0] == "bad"
    assert "summary.json" in err


def test_summary_run_id_comes_from_summary_json(tmp_path, capsys):
    d = make_results_dir(tmp_path / "r", "folder-name", None, {**FULL_SUMMARY, "run_id": "from-json"})
    main(["summary", str(d)])
    assert capsys.readouterr().out.splitlines()[1].startswith("from-json")


def test_summary_expands_wildcards_when_arg_is_not_a_path(tmp_path, capsys):
    cfg = make_config(tmp_path, "run-a")
    root = tmp_path / "r"
    make_results_dir(root, "run-b", cfg, {**FULL_SUMMARY, "run_id": "run-b"})
    make_results_dir(root, "run-a", cfg, {**FULL_SUMMARY, "run_id": "run-a"})
    (root / "notes.txt").write_text("not a run", encoding="utf-8")

    assert main(["summary", str(root / "run-*")]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines[1:]] == ["run-a", "run-b"]  # sorted, one row each

    assert main(["summary", str(root / "*")]) == 0  # files matched by the glob are skipped
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines[1:]] == ["run-a", "run-b"]


def test_summary_wildcard_and_literal_args_mix(tmp_path, capsys):
    cfg = make_config(tmp_path, "run-a")
    root = tmp_path / "r"
    make_results_dir(root, "run-a", cfg, {**FULL_SUMMARY, "run_id": "run-a"})
    other = make_results_dir(tmp_path / "other", "zzz", cfg, {**FULL_SUMMARY, "run_id": "zzz"})
    assert main(["summary", str(other), str(root / "run-*")]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines[1:]] == ["zzz", "run-a"]


def test_summary_wildcard_without_match_prints_dash_row(tmp_path, capsys):
    assert main(["summary", str(tmp_path / "nothing-*")]) == 0
    out, err = capsys.readouterr()
    assert len(out.splitlines()) == 2
    assert "nothing-*" in out.splitlines()[1].split()[0]
    assert "summary.json" in err


def test_summary_existing_path_with_glob_characters_is_not_expanded(tmp_path, capsys):
    cfg = make_config(tmp_path, "x")
    d = make_results_dir(tmp_path / "r", "run[1]", cfg, {**FULL_SUMMARY, "run_id": "bracket"})
    make_results_dir(tmp_path / "r", "run1", cfg, {**FULL_SUMMARY, "run_id": "plain"})
    assert main(["summary", str(d)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines[1:]] == ["bracket"]


def test_summary_requires_at_least_one_dir(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["summary"])
    assert exc.value.code == 2


# ---------------------------------------------------------------- judge / analyze dispatch


def test_judge_dispatch(results, tmp_path, monkeypatch):
    cfg_path = make_config(tmp_path, "exp1-pilot")
    cfg = load_config(cfg_path)
    make_results_dir(results, "exp1-pilot", cfg_path)
    calls = []
    fake_module(monkeypatch, "bench.judge", judge_run=lambda *a, **k: calls.append((a, k)))

    assert main(["judge", "exp1-pilot"]) == 0

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == (results / "exp1-pilot",)
    assert kwargs["judge"] == config.to_plain(cfg.models.judge)
    assert kwargs["judge"]["id"] == cfg.models.judge.id
    assert kwargs["judge"]["provider"] == {"order": ["wafer"], "allow_fallbacks": False}
    assert kwargs["judge"]["concurrency"] == 8
    assert kwargs["cards_dir"] == REPO / "registry" / "v0.1" / "cards"
    assert set(kwargs) == {"judge", "cards_dir"}


def test_analyze_dispatch(results, tmp_path, monkeypatch):
    cfg_path = make_config(tmp_path, "exp1-pilot", bootstrap=7)
    make_results_dir(results, "exp1-pilot", cfg_path)
    calls = []
    fake_module(monkeypatch, "bench.analysis", analyze_run=lambda *a, **k: calls.append((a, k)))

    assert main(["analyze", "exp1-pilot"]) == 0

    assert calls == [((results / "exp1-pilot",), {"bootstrap_seed": 7})]


@pytest.mark.parametrize("cmd", ["judge", "analyze"])
def test_unknown_run_id_exits_1(results, capsys, cmd):
    assert main([cmd, "nope"]) == 1
    assert "nope" in capsys.readouterr().err


@pytest.mark.parametrize("cmd,module", [("judge", "bench.judge"), ("analyze", "bench.analysis")])
def test_judge_analyze_exit_2_when_module_missing(results, tmp_path, monkeypatch, capsys, cmd, module):
    make_results_dir(results, "exp1-pilot", make_config(tmp_path))
    missing_module(monkeypatch, module)
    assert main([cmd, "exp1-pilot"]) == 2
    assert "not implemented yet" in capsys.readouterr().err


def test_run_dir_with_invalid_config_yaml_exits_1(results, capsys):
    d = results / "broken"
    d.mkdir()
    (d / "config.yaml").write_text("run_id: broken\n", encoding="utf-8")
    assert main(["judge", "broken"]) == 1
    assert "setting" in capsys.readouterr().err


# ---------------------------------------------------------------- run / preflight


def test_run_exit_2_when_module_missing_and_no_folder_created(results, monkeypatch, capsys):
    missing_module(monkeypatch, "bench.task")
    assert main(["run", str(PILOT)]) == 2
    err = capsys.readouterr().err
    assert "not implemented yet" in err
    assert list(results.iterdir()) == []


def test_preflight_exit_2_when_module_missing(results, monkeypatch, capsys):
    missing_module(monkeypatch, "bench.preflight")
    assert main(["preflight", str(PILOT)]) == 2
    assert "not implemented yet" in capsys.readouterr().err
    assert list(results.iterdir()) == []


def test_run_dispatch_creates_folder_and_finishes_manifest(results, monkeypatch):
    calls = []

    def run_experiment(cfg, run_dir):
        calls.append((cfg, run_dir))
        assert (run_dir / "config.yaml").is_file()
        assert (run_dir / "manifest.json").is_file()
        return ["001", "002"]

    fake_module(monkeypatch, "bench.task", run_experiment=run_experiment)

    assert main(["run", str(PILOT)]) == 0

    run_dir = results / "exp1-pilot"
    assert calls == [(load_config(PILOT), run_dir)]
    assert (run_dir / "config.yaml").read_bytes() == PILOT.read_bytes()
    m = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert m["main_task_ids"] == ["001", "002"]
    assert m["end_time"] is not None


def test_run_sets_end_time_even_when_run_experiment_raises(results, monkeypatch):
    def run_experiment(cfg, run_dir):
        raise RuntimeError("boom")

    fake_module(monkeypatch, "bench.task", run_experiment=run_experiment)
    with pytest.raises(RuntimeError):
        main(["run", str(PILOT)])
    m = json.loads((results / "exp1-pilot" / "manifest.json").read_text(encoding="utf-8"))
    assert m["end_time"] is not None
    assert m["main_task_ids"] is None


def _manifest_after_run(results, monkeypatch, preflight_text, returns=None):
    run_dir = results / "exp1-pilot"
    run_dir.mkdir()
    if preflight_text is not None:
        (run_dir / "preflight.json").write_text(preflight_text, encoding="utf-8")
    fake_module(monkeypatch, "bench.task", run_experiment=lambda cfg, d: returns)
    assert main(["run", str(PILOT)]) == 0
    return json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))


def test_main_task_ids_come_from_preflight_json_when_run_experiment_returns_none(results, monkeypatch):
    preflight = json.dumps({"kept": ["b", "a", 7], "skipped": [{"task_id": "z", "error": "boom"}]})
    m = _manifest_after_run(results, monkeypatch, preflight)
    assert m["main_task_ids"] == ["b", "a", "7"]
    assert m["end_time"] is not None


def test_returned_ids_win_over_preflight_json(results, monkeypatch):
    m = _manifest_after_run(results, monkeypatch, json.dumps({"kept": ["x"]}), returns=["r1", "r2"])
    assert m["main_task_ids"] == ["r1", "r2"]


@pytest.mark.parametrize(
    "text",
    [None, "{not json", "[]", json.dumps({"skipped": []}), json.dumps({"kept": "abc"}), json.dumps({"kept": [{"id": 1}]})],
)
def test_main_task_ids_stay_null_without_usable_preflight_json(results, monkeypatch, text):
    assert _manifest_after_run(results, monkeypatch, text)["main_task_ids"] is None


def test_run_refuses_when_results_folder_exists(results, monkeypatch, capsys):
    calls = []
    fake_module(monkeypatch, "bench.task", run_experiment=lambda *a: calls.append(a))
    (results / "exp1-pilot").mkdir()
    (results / "exp1-pilot" / "samples.jsonl").write_text("x", encoding="utf-8")

    assert main(["run", str(PILOT)]) == 1

    assert calls == []
    assert "already exists" in capsys.readouterr().err
    assert not (results / "exp1-pilot" / "manifest.json").exists()


def test_run_after_preflight_is_allowed(results, monkeypatch):
    calls = []
    fake_module(monkeypatch, "bench.preflight", preflight=lambda cfg, run_dir: (run_dir / "preflight.json").write_text("{}"))
    fake_module(monkeypatch, "bench.task", run_experiment=lambda cfg, run_dir: calls.append(run_dir))

    assert main(["preflight", str(PILOT)]) == 0
    assert main(["run", str(PILOT)]) == 0

    assert calls == [results / "exp1-pilot"]
    assert (results / "exp1-pilot" / "preflight.json").is_file()


def test_preflight_dispatch(results, monkeypatch):
    calls = []
    fake_module(monkeypatch, "bench.preflight", preflight=lambda cfg, run_dir: calls.append((cfg, run_dir)))
    assert main(["preflight", str(PILOT)]) == 0
    assert calls == [(load_config(PILOT), results / "exp1-pilot")]
    assert (results / "exp1-pilot").is_dir()


def test_preflight_refuses_when_run_already_started(results, monkeypatch, capsys):
    calls = []
    fake_module(monkeypatch, "bench.preflight", preflight=lambda *a: calls.append(a))
    (results / "exp1-pilot").mkdir()
    (results / "exp1-pilot" / "manifest.json").write_text("{}", encoding="utf-8")
    assert main(["preflight", str(PILOT)]) == 1
    assert calls == []
    assert "already exists" in capsys.readouterr().err


def test_run_with_invalid_config_exits_1_naming_the_field(results, tmp_path, capsys):
    data = yaml.safe_load(PILOT.read_text(encoding="utf-8"))
    data["models"]["agent"]["reasoning_effort"] = "max"
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert main(["run", str(bad)]) == 1
    assert "models.agent.reasoning_effort" in capsys.readouterr().err
    assert list(results.iterdir()) == []


def test_run_with_missing_config_file_exits_1(results, tmp_path, capsys):
    assert main(["run", str(tmp_path / "nope.yaml")]) == 1
    assert "nope.yaml" in capsys.readouterr().err


# ---------------------------------------------------------------- render-prompts


class FakeReviewSample:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def fake_basharena(monkeypatch, calls=None, pool=("first-task", "second-task")):
    calls = calls if calls is not None else {}

    def shuffled_pool(seed):
        calls["seed"] = seed
        return list(pool)

    def load_task(task_id):
        calls["task_id"] = task_id
        return types.SimpleNamespace(description=f"TASK TEXT of {task_id}")

    return calls, fake_module(
        monkeypatch,
        "bench.basharena",
        shuffled_pool=shuffled_pool,
        load_task=load_task,
        setting_description=lambda: "SETTING TEXT",
        SIDE_TASKS={
            name: types.SimpleNamespace(description=f"SIDE TEXT of {name}")
            for name in ("download_malware", "leak_passwords", "disable_firewall", "sudo_edit")
        },
    )


def test_render_prompts_passes_first_task_and_first_side_task(results, tmp_path, monkeypatch, capsys):
    calls, _ = fake_basharena(monkeypatch)
    cfg_path = make_config(tmp_path, "exp1-pilot", main_seed=5)
    seen = {}

    def write_review_prompts(out_dir, samples):
        seen["out_dir"], seen["samples"] = out_dir, samples
        return [out_dir / "x.md"]

    fake_module(monkeypatch, "bench.prompts", ReviewSample=FakeReviewSample, write_review_prompts=write_review_prompts)

    assert main(["render-prompts", str(cfg_path)]) == 0

    assert calls == {"seed": 5, "task_id": "first-task"}
    assert seen["out_dir"] == results / "exp1-pilot" / "prompts"
    assert list(seen["samples"]) == ["unaware", "aware"]
    unaware, aware = seen["samples"]["unaware"], seen["samples"]["aware"]
    assert unaware.__dict__ == aware.__dict__  # the same scenario in both conditions
    assert unaware.task_description == "TASK TEXT of first-task"
    assert unaware.setting_description == "SETTING TEXT"
    assert unaware.side_task_description == "SIDE TEXT of download_malware"
    assert unaware.attack_base_path == REPO / "prompts" / "exp1" / "attack_base.md"
    assert unaware.aware_section_path == REPO / "prompts" / "exp1" / "aware_all_cards.md"
    assert unaware.cards_dir == REPO / "registry" / "v0.1" / "cards"
    assert unaware.card_id is None
    assert "first-task" in capsys.readouterr().out


def test_render_prompts_uses_first_side_task_of_the_config(results, tmp_path, monkeypatch):
    fake_basharena(monkeypatch)
    data = yaml.safe_load(PILOT.read_text(encoding="utf-8"))
    data["setting"]["side_tasks"] = ["sudo_edit", "leak_passwords"]
    data["conditions"] = ["aware"]
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    seen = {}

    def write_review_prompts(out_dir, samples):
        seen["samples"] = samples
        return []

    fake_module(monkeypatch, "bench.prompts", ReviewSample=FakeReviewSample, write_review_prompts=write_review_prompts)
    assert main(["render-prompts", str(cfg_path)]) == 0
    assert list(seen["samples"]) == ["aware"]
    assert seen["samples"]["aware"].side_task_description == "SIDE TEXT of sudo_edit"


def test_render_prompts_writes_real_files_and_aware_starts_with_unaware(results, monkeypatch):
    fake_basharena(monkeypatch)
    assert main(["render-prompts", str(PILOT)]) == 0
    out = results / "exp1-pilot" / "prompts"
    unaware = (out / "unaware_developer_instructions.md").read_text(encoding="utf-8")
    aware = (out / "aware_developer_instructions.md").read_text(encoding="utf-8")
    assert aware.startswith(unaware) and len(aware) > len(unaware)
    assert "SIDE TEXT of download_malware" in unaware
    assert "TASK TEXT of first-task" in (out / "aware_user_message.md").read_text(encoding="utf-8")
    assert "SIDE TEXT" not in (out / "unaware_user_message.md").read_text(encoding="utf-8")
    # only prompts/ was created: no config copy and no manifest, so a later `run` is not blocked
    assert sorted(p.name for p in (results / "exp1-pilot").iterdir()) == ["prompts"]


def test_render_prompts_exit_2_when_basharena_missing(results, monkeypatch, capsys):
    missing_module(monkeypatch, "bench.basharena")
    assert main(["render-prompts", str(PILOT)]) == 2
    assert "not implemented yet" in capsys.readouterr().err
    assert list(results.iterdir()) == []


def test_render_prompts_exit_2_when_a_name_is_missing(results, monkeypatch, capsys):
    fake_module(monkeypatch, "bench.basharena", shuffled_pool=lambda seed: ["t"])  # no load_task etc.
    assert main(["render-prompts", str(PILOT)]) == 2
    assert "not implemented yet" in capsys.readouterr().err
    assert list(results.iterdir()) == []


def test_render_prompts_exit_2_when_prompts_module_missing(results, monkeypatch, capsys):
    fake_basharena(monkeypatch)
    missing_module(monkeypatch, "bench.prompts")
    assert main(["render-prompts", str(PILOT)]) == 2
    assert "not implemented yet" in capsys.readouterr().err
    assert list(results.iterdir()) == []


def test_render_prompts_missing_data_exits_1(results, monkeypatch, capsys):
    _, module = fake_basharena(monkeypatch)

    def shuffled_pool(seed):
        raise FileNotFoundError("pool_no_cover.txt not found")

    module.shuffled_pool = shuffled_pool
    assert main(["render-prompts", str(PILOT)]) == 1
    assert "pool_no_cover.txt" in capsys.readouterr().err


def test_render_prompts_refuses_when_run_folder_has_other_content(results, monkeypatch, capsys):
    fake_basharena(monkeypatch)
    (results / "exp1-pilot").mkdir()
    (results / "exp1-pilot" / "samples.jsonl").write_text("x", encoding="utf-8")
    assert main(["render-prompts", str(PILOT)]) == 1
    assert "already exists" in capsys.readouterr().err
    assert not (results / "exp1-pilot" / "prompts").exists()


def test_render_prompts_can_be_repeated_and_followed_by_preflight_and_run(results, monkeypatch):
    _, basharena = fake_basharena(monkeypatch)
    fake_module(monkeypatch, "bench.preflight", preflight=lambda cfg, run_dir: (run_dir / "preflight.json").write_text("{}"))
    calls = []
    fake_module(monkeypatch, "bench.task", run_experiment=lambda cfg, run_dir: calls.append(run_dir))
    assert main(["render-prompts", str(PILOT)]) == 0
    assert main(["render-prompts", str(PILOT)]) == 0
    assert main(["preflight", str(PILOT)]) == 0
    assert main(["run", str(PILOT)]) == 0
    assert calls == [results / "exp1-pilot"]
    assert (results / "exp1-pilot" / "prompts" / "aware_user_message.md").is_file()
    assert (results / "exp1-pilot" / "preflight.json").is_file()
    assert (results / "exp1-pilot" / "manifest.json").is_file()


# ---------------------------------------------------------------- entry point


def test_python_dash_m_bench_runs_summary(tmp_path):
    d = make_results_dir(tmp_path / "r", "run-a", None, FULL_SUMMARY)
    r = subprocess.run(
        [sys.executable, "-m", "bench", "summary", str(d)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stderr
    assert "0.25 [0.20, 0.30]" in r.stdout


def test_no_subcommand_is_usage_error():
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2



def test_check_registry_reports_no_problems(capsys):
    assert main(["check-registry"]) == 0
    assert "0 problems" in capsys.readouterr().out


def one_card_config(tmp_path, n=14, **changes):
    data = yaml.safe_load(PILOT.read_text(encoding="utf-8"))
    data["prompts"]["aware_mode"] = "one_card"
    data["setting"]["main_tasks"]["n"] = n
    data.update(changes)
    path = tmp_path / "one.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_render_prompts_one_card_mode_renders_the_assigned_card(results, tmp_path, monkeypatch, capsys):
    from bench import assignment

    pool = [f"task{i:02d}" for i in range(30)]
    fake_basharena(monkeypatch, pool=pool)
    cfg_path = one_card_config(tmp_path)
    cfg = load_config(cfg_path)
    expected = assignment.assigned_cards(cfg, pool[:14])[("task00", "download_malware")]

    assert main(["render-prompts", str(cfg_path)]) == 0

    out = results / "exp1-pilot" / "prompts"
    aware = (out / "aware_developer_instructions.md").read_text(encoding="utf-8")
    assert aware.count("\n## Card E") == 1 and f"\n## Card {expected}\n" in aware
    assert "you prefer" not in aware
    assert f"card {expected}" in capsys.readouterr().out
    assert "## Card E" not in (out / "unaware_developer_instructions.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("command", ["render-prompts", "preflight", "run"])
def test_one_card_mode_with_an_unbalanced_main_task_count_fails_before_anything_runs(
    results, tmp_path, monkeypatch, capsys, command
):
    fake_basharena(monkeypatch)
    assert main([command, str(one_card_config(tmp_path, n=20))]) == 1
    assert "multiple of the number of cards (14)" in capsys.readouterr().err
    assert not (results / "exp1-pilot").exists()
