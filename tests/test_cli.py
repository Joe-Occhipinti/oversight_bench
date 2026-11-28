"""Tests for the CLI (bench.__main__): run, resume, preflight, render-prompts, metrics, check-registry."""

import json
import subprocess
import sys
import types
from pathlib import Path

import pytest
import yaml

from bench import __main__ as bench_main
from bench import config
from bench import run as bench_run
from bench.__main__ import main
from bench.config import load_config

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "tests" / "fixtures" / "exp3" / "config.yaml"
RUN_ID = "exp3-test"
IDS = {"search": ["001"], "test": ["002"]}


@pytest.fixture
def results(tmp_path, monkeypatch):
    root = tmp_path / "results"
    root.mkdir()
    monkeypatch.setattr(config, "RESULTS_ROOT", root)
    return root


def fake_run(monkeypatch, run_experiment):
    monkeypatch.setattr(bench_run, "run_experiment", run_experiment)


# ---------------------------------------------------------------- run / preflight


def test_run_dispatch_creates_folder_and_finishes_manifest(results, monkeypatch):
    calls = []

    def run_experiment(cfg, run_dir, max_samples=None):
        calls.append((cfg, run_dir))
        assert (run_dir / "config.yaml").is_file()
        assert (run_dir / "manifest.json").is_file()
        return IDS

    fake_run(monkeypatch, run_experiment)
    assert main(["run", str(CONFIG)]) == 0

    run_dir = results / RUN_ID
    assert calls == [(load_config(CONFIG), run_dir)]
    assert (run_dir / "config.yaml").read_bytes() == CONFIG.read_bytes()
    m = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert m["main_task_ids"] == IDS
    assert m["end_time"] is not None and m["status"] == "finished"


def test_run_sets_end_time_even_when_run_experiment_raises(results, monkeypatch):
    def run_experiment(cfg, run_dir, max_samples=None):
        raise RuntimeError("boom")

    fake_run(monkeypatch, run_experiment)
    with pytest.raises(RuntimeError):
        main(["run", str(CONFIG)])
    m = json.loads((results / RUN_ID / "manifest.json").read_text(encoding="utf-8"))
    assert m["end_time"] is not None and m["status"] == "failed"  # not read as finished
    assert m["main_task_ids"] is None


def test_a_run_that_failed_before_its_first_eval_set_can_be_resumed(results, monkeypatch):
    """For example preflight.json was missing: the folder has a manifest but no inspect_logs."""
    from bench.task import PreflightError

    def no_preflight(cfg, run_dir, max_samples=None):
        raise PreflightError("preflight.json not found")

    fake_run(monkeypatch, no_preflight)
    assert main(["run", str(CONFIG)]) == 1
    run_dir = results / RUN_ID
    assert not (run_dir / "inspect_logs").exists()
    monkeypatch.setattr(bench_main, "_leftover_sandboxes", lambda: [])
    fake_run(monkeypatch, lambda cfg, d, max_samples=None: IDS)
    assert main(["resume", RUN_ID]) == 0
    assert json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))["status"] == "finished"


def test_run_refuses_when_results_folder_exists(results, monkeypatch, capsys):
    calls = []
    fake_run(monkeypatch, lambda *a, **k: calls.append(a))
    (results / RUN_ID).mkdir()
    (results / RUN_ID / "samples.jsonl").write_text("x", encoding="utf-8")

    assert main(["run", str(CONFIG)]) == 1

    assert calls == []
    assert "already exists" in capsys.readouterr().err
    assert not (results / RUN_ID / "manifest.json").exists()


def test_run_after_preflight_is_allowed(results, monkeypatch):
    import bench.preflight

    calls = []
    monkeypatch.setattr(bench.preflight, "preflight", lambda cfg, run_dir: (run_dir / "preflight.json").write_text("{}"))
    fake_run(monkeypatch, lambda cfg, run_dir, max_samples=None: calls.append(run_dir) or IDS)

    assert main(["preflight", str(CONFIG)]) == 0
    assert main(["run", str(CONFIG)]) == 0

    assert calls == [results / RUN_ID]
    assert (results / RUN_ID / "preflight.json").is_file()


def test_preflight_dispatch_keeps_search_and_test_tasks(results, monkeypatch):
    import bench.preflight

    calls = []
    monkeypatch.setattr(bench.preflight, "preflight", lambda cfg, run_dir: calls.append((cfg, run_dir)))
    assert main(["preflight", str(CONFIG)]) == 0
    assert calls == [(load_config(CONFIG), results / RUN_ID)]
    assert calls[0][0].setting.main_tasks.n == 5  # search_n + test_n: what preflight keeps
    assert (results / RUN_ID).is_dir()


def test_preflight_refuses_when_run_already_started(results, monkeypatch, capsys):
    import bench.preflight

    calls = []
    monkeypatch.setattr(bench.preflight, "preflight", lambda *a: calls.append(a))
    (results / RUN_ID).mkdir()
    (results / RUN_ID / "manifest.json").write_text("{}", encoding="utf-8")
    assert main(["preflight", str(CONFIG)]) == 1
    assert calls == []
    assert "already exists" in capsys.readouterr().err


def test_run_with_invalid_config_exits_1_naming_the_field(results, tmp_path, capsys):
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    data["models"]["agent"]["reasoning_effort"] = "max"
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert main(["run", str(bad)]) == 1
    assert "models.agent.reasoning_effort" in capsys.readouterr().err
    assert list(results.iterdir()) == []


def test_run_with_missing_config_file_exits_1(results, tmp_path, capsys):
    assert main(["run", str(tmp_path / "nope.yaml")]) == 1
    assert "nope.yaml" in capsys.readouterr().err


def test_run_dir_with_invalid_config_yaml_exits_1(results, capsys):
    d = results / "broken"
    d.mkdir()
    (d / "config.yaml").write_text("run_id: broken\n", encoding="utf-8")
    assert main(["metrics", "broken"]) == 1
    assert "setting" in capsys.readouterr().err


# ---------------------------------------------------------------- resume


def started_run(results, monkeypatch):
    """Start a run with a fake run_experiment and return its folder (as left by an interrupted `run`)."""
    fake_run(monkeypatch, lambda cfg, run_dir, max_samples=None: IDS)
    assert main(["run", str(CONFIG)]) == 0
    (results / RUN_ID / "inspect_logs").mkdir(exist_ok=True)
    monkeypatch.setattr(bench_main, "_leftover_sandboxes", lambda: [])
    return results / RUN_ID


def test_resume_runs_the_saved_config_and_notes_the_resume(results, monkeypatch):
    run_dir = started_run(results, monkeypatch)
    saved = load_config(run_dir / "config.yaml")
    calls = []

    def run_experiment(cfg, d, max_samples=None):
        calls.append((cfg, d, max_samples))
        assert json.loads((d / "manifest.json").read_text(encoding="utf-8"))["end_time"] is None  # running again
        return IDS

    fake_run(monkeypatch, run_experiment)
    assert main(["resume", RUN_ID, "--max-samples", "4"]) == 0

    assert calls == [(saved, run_dir, 4)]
    m = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert [r["max_samples"] for r in m["resumes"]] == [4] and m["resumes"][0]["time"]
    assert m["end_time"] is not None and m["main_task_ids"] == IDS


def test_resume_uses_the_saved_config_not_the_one_in_runs(results, monkeypatch):
    run_dir = started_run(results, monkeypatch)
    data = yaml.safe_load((run_dir / "config.yaml").read_text(encoding="utf-8"))
    data["parallel"]["max_samples"] = 3
    (run_dir / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    seen = []
    fake_run(monkeypatch, lambda cfg, d, max_samples=None: seen.append(cfg.parallel.max_samples) or IDS)
    assert main(["resume", RUN_ID]) == 0
    assert seen == [3]


def test_resume_of_a_run_that_never_started_is_an_error(results, capsys):
    assert main(["resume", "nope"]) == 1
    assert "no results folder" in capsys.readouterr().err
    (results / "bare").mkdir()
    (results / "bare" / "config.yaml").write_bytes(CONFIG.read_bytes())
    assert main(["resume", "bare"]) == 1
    assert "holds no started run" in capsys.readouterr().err


def test_resume_warns_about_leftover_containers_but_goes_on(results, monkeypatch, capsys):
    started_run(results, monkeypatch)
    calls = []
    fake_run(monkeypatch, lambda cfg, d, max_samples=None: calls.append(d) or IDS)
    monkeypatch.setattr(bench_main, "_leftover_sandboxes", lambda: ["inspect-a-1", "inspect-a-2"])
    assert main(["resume", RUN_ID]) == 0
    err = capsys.readouterr().err
    assert "2 Docker containers" in err and "inspect sandbox cleanup docker" in err
    assert len(calls) == 1


def test_run_on_an_existing_folder_points_to_resume(results, monkeypatch, capsys):
    started_run(results, monkeypatch)
    assert main(["run", str(CONFIG)]) == 1
    err = capsys.readouterr().err
    assert "refusing to overwrite" in err and f"bench resume {RUN_ID}" in err


def test_resume_with_redo_marks_the_provider_failures_first_and_notes_them(results, monkeypatch, capsys):
    from bench import redo

    run_dir = started_run(results, monkeypatch)
    order = []
    failed = ["tier1/execution/a__x", "ceiling2/execution/b__y"]
    monkeypatch.setattr(redo, "mark_for_redo", lambda d: order.append(("mark", d)) or failed)
    fake_run(monkeypatch, lambda cfg, d, max_samples=None: order.append(("run", d)) or IDS)

    assert main(["resume", RUN_ID, "--redo-provider-failures"]) == 0

    assert order == [("mark", run_dir), ("run", run_dir)]  # marked before Inspect looks at the logs
    out = capsys.readouterr().out
    assert "2 execution samples" in out and ", ".join(failed) in out
    entry = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))["resumes"][-1]
    assert entry["redo"] == failed


def test_resume_without_the_flag_does_not_touch_the_logs(results, monkeypatch):
    from bench import redo

    run_dir = started_run(results, monkeypatch)
    monkeypatch.setattr(redo, "mark_for_redo", lambda d: pytest.fail("mark_for_redo was called"))
    fake_run(monkeypatch, lambda cfg, d, max_samples=None: IDS)
    assert main(["resume", RUN_ID]) == 0
    entry = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))["resumes"][-1]
    assert "redo" not in entry


def test_leftover_sandboxes_lists_only_inspect_containers(monkeypatch):
    out = types.SimpleNamespace(stdout="inspect-run--abc-default-1\nportainer\ninspect-run--abc-gateway-1\n")
    monkeypatch.setattr(bench_main.subprocess, "run", lambda *a, **k: out)
    assert bench_main._leftover_sandboxes() == ["inspect-run--abc-default-1", "inspect-run--abc-gateway-1"]

    def no_docker(*a, **k):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(bench_main.subprocess, "run", no_docker)
    assert bench_main._leftover_sandboxes() == []


# ---------------------------------------------------------------- render-prompts


def test_render_prompts_writes_the_first_search_pair_in_every_role(results, capsys):
    from bench import basharena, search

    assert main(["render-prompts", str(CONFIG)]) == 0
    cfg = load_config(CONFIG)
    ids = basharena.shuffled_pool(0)[:3]  # no preflight.json: the shuffled pool
    main_task, side_task = search.search_pairs(ids, cfg.setting.side_tasks, 6, 0)[0]
    out = capsys.readouterr().out
    assert f"main task {main_task}, side task {side_task}" in out
    folder = results / RUN_ID / "prompts"
    names = sorted(p.name for p in folder.iterdir())
    assert len(names) == 1 + 4 * 4 + 4 and "user_message.md" in names and "ceiling4_execution.md" in names
    user = (folder / "user_message.md").read_text(encoding="utf-8")
    assert user == "Your task is as follows:\n" + basharena.load_task(main_task).description + "\n"
    assert f"### {side_task} (yours)" in (folder / "tier2_execution.md").read_text(encoding="utf-8")


def test_render_prompts_uses_preflight_json_and_allows_a_later_run(results, monkeypatch):
    from bench import basharena

    folder = results / RUN_ID
    folder.mkdir()
    kept = basharena.pool_ids()[5:10]
    (folder / "preflight.json").write_text(json.dumps({"kept": kept}), encoding="utf-8")
    assert main(["render-prompts", str(CONFIG)]) == 0
    assert main(["render-prompts", str(CONFIG)]) == 0  # can be repeated
    user = (folder / "prompts" / "user_message.md").read_text(encoding="utf-8")
    assert any(basharena.load_task(t).description in user for t in kept[:3])
    fake_run(monkeypatch, lambda cfg, run_dir, max_samples=None: IDS)
    assert main(["run", str(CONFIG)]) == 0


def test_render_prompts_refuses_when_run_folder_has_other_content(results, capsys):
    (results / RUN_ID).mkdir()
    (results / RUN_ID / "samples.jsonl").write_text("x", encoding="utf-8")
    assert main(["render-prompts", str(CONFIG)]) == 1
    assert "already exists" in capsys.readouterr().err


def test_render_prompts_missing_prompt_file_exits_1(results, tmp_path, capsys):
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    data["prompts"]["dir"] = str(tmp_path / "no-prompts")
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert main(["render-prompts", str(path)]) == 1
    assert "base.md" in capsys.readouterr().err


# ---------------------------------------------------------------- metrics, registry, entry point


def test_metrics_without_samples_jsonl_exits_1(results, capsys):
    (results / RUN_ID).mkdir()
    (results / RUN_ID / "config.yaml").write_bytes(CONFIG.read_bytes())
    assert main(["metrics", RUN_ID]) == 1
    assert "no samples.jsonl" in capsys.readouterr().err


def test_python_dash_m_bench_lists_the_commands():
    out = subprocess.run([sys.executable, "-m", "bench", "--help"], capture_output=True, text=True, cwd=REPO)
    assert out.returncode == 0
    for cmd in ("run", "resume", "preflight", "render-prompts", "metrics", "check-registry"):
        assert cmd in out.stdout
    for old in ("judge", "plot-asr", "docent-upload", "failure-extract"):
        assert old not in out.stdout


def test_no_subcommand_is_usage_error():
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_check_registry_reports_no_problems(capsys):
    assert main(["check-registry"]) == 0
    assert capsys.readouterr().out.strip().endswith("0 problems")
