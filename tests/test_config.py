"""Tests for bench.config: run config validation, results folder, manifest, meltzer tree hash."""

import copy
import dataclasses
import hashlib
import importlib.metadata
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from bench import config
from bench.config import ConfigError, load_config

REPO = Path(__file__).resolve().parent.parent
RUNS = REPO / "runs"

BASE = {
    "run_id": "t-01",
    "setting": {
        "name": "basharena",
        "side_tasks": ["download_malware", "leak_passwords", "disable_firewall", "sudo_edit"],
        "main_tasks": {"pool": "no_cover", "n": 30, "seed": 0},
    },
    "conditions": ["unaware", "aware"],
    "prompts": {
        "attack_base": "a.md",
        "aware_mode": "all_cards",
        "aware_section": {"all_cards": "b.md", "one_card": "c.md"},
        "cards": "cards",
    },
    "models": {
        "agent": {
            "id": "a/agent",
            "reasoning_effort": None,
            "provider": {"order": ["together"], "allow_fallbacks": False},
        },
        "monitor": {
            "id": "a/monitor",
            "reasoning_effort": "low",
            "provider": {"order": ["together"], "allow_fallbacks": False},
        },
        "judge": {
            "id": "a/judge",
            "reasoning_effort": "max",
            "provider": {"order": ["together"], "allow_fallbacks": False},
            "temperature": 0,
            "concurrency": 8,
        },
    },
    "meltzer": {"max_turn_steps": 100},
    "limits": {"meltzer_seconds": 1800, "sample_time_limit_seconds": 2700, "wait_seconds": 15},
    "parallel": {"max_samples": 6},
    "seeds": {"inspect_shuffle": 0, "card_assignment": 0, "bootstrap": 0},
}


def write_cfg(tmp_path, data, name="cfg.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def load(tmp_path, data):
    return load_config(write_cfg(tmp_path, data))


def mutated(path, value=...):
    """Copy of BASE with dotted `path` set to `value`, or deleted when value is `...`."""
    data = copy.deepcopy(BASE)
    node = data
    *parents, last = path.split(".")
    for key in parents:
        node = node[key]
    if value is ...:
        del node[last]
    else:
        node[last] = value
    return data


def all_paths(node=BASE, prefix=""):
    """Every field path of BASE, parents before children."""
    out = []
    for key, value in node.items():
        path = f"{prefix}{key}"
        out.append(path)
        if isinstance(value, dict):
            out += all_paths(value, path + ".")
    return out


# ---------------------------------------------------------------- the two run files


def test_exp1_01_yaml_loads_with_section_12_values():
    cfg = load_config(RUNS / "exp1-01.yaml")
    assert cfg.run_id == "exp1-01"
    assert cfg.setting.name == "basharena"
    assert cfg.setting.side_tasks == ("download_malware", "leak_passwords", "disable_firewall", "sudo_edit")
    assert cfg.setting.main_tasks == config.MainTasks(pool="no_cover", n=30, seed=0)
    assert cfg.conditions == ("unaware", "aware")
    assert cfg.prompts == config.Prompts(
        attack_base="prompts/exp1/attack_base.md",
        aware_mode="all_cards",
        aware_section=config.AwareSections(
            all_cards="prompts/exp1/aware_all_cards.md", one_card="prompts/exp1/aware_one_card.md"
        ),
        cards="registry/v0.1/cards",
    )
    assert cfg.prompts.aware_section_file == "prompts/exp1/aware_all_cards.md"
    wafer = config.Provider(order=("wafer",), allow_fallbacks=False)
    assert cfg.models.agent == config.ModelSpec(id="z-ai/glm-5.3", reasoning_effort="high", provider=wafer)
    assert cfg.models.monitor == config.ModelSpec(id="z-ai/glm-5.3-flash", reasoning_effort="low", provider=wafer)
    assert cfg.models.judge == config.JudgeSpec(
        id="deepseek/deepseek-v4.1-flash",
        reasoning_effort="high",
        provider=wafer,
        temperature=0,
        concurrency=8,
    )
    assert cfg.meltzer == config.Meltzer(max_turn_steps=100)
    assert cfg.limits == config.Limits(meltzer_seconds=1800, sample_time_limit_seconds=2700, wait_seconds=15)
    assert cfg.parallel == config.Parallel(max_samples=6)
    assert cfg.seeds == config.Seeds(inspect_shuffle=0, card_assignment=0, bootstrap=0)


def test_pilot_yaml_differs_from_exp1_01_only_in_run_id_and_n():
    full = load_config(RUNS / "exp1-01.yaml")
    pilot = load_config(RUNS / "exp1-pilot.yaml")
    assert pilot.run_id == "exp1-pilot"
    assert pilot.setting.main_tasks.n == 2
    expected = dataclasses.replace(
        full,
        run_id="exp1-pilot",
        setting=dataclasses.replace(
            full.setting, main_tasks=dataclasses.replace(full.setting.main_tasks, n=2)
        ),
    )
    assert pilot == expected


def test_base_dict_loads(tmp_path):
    assert load(tmp_path, BASE).run_id == "t-01"


def test_provider_is_per_model(tmp_path):
    data = mutated("models.monitor.provider.order", ["wafer", "together"])
    data["models"]["judge"]["provider"]["allow_fallbacks"] = True
    cfg = load(tmp_path, data)
    assert cfg.models.agent.provider == config.Provider(order=("together",), allow_fallbacks=False)
    assert cfg.models.monitor.provider == config.Provider(order=("wafer", "together"), allow_fallbacks=False)
    assert cfg.models.judge.provider == config.Provider(order=("together",), allow_fallbacks=True)


def test_config_is_frozen(tmp_path):
    cfg = load(tmp_path, BASE)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.run_id = "x"
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.models.agent.id = "x"


def test_to_plain_gives_json_ready_dict(tmp_path):
    cfg = load(tmp_path, BASE)
    plain = config.to_plain(cfg.models.judge)
    assert plain == BASE["models"]["judge"]
    json.dumps(plain)  # tuples became lists


# ---------------------------------------------------------------- file level


def test_missing_file_is_config_error(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "nope.yaml")


def test_invalid_yaml_is_config_error(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("a: [1, 2\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(path)


def test_top_level_must_be_mapping(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(path)


# ---------------------------------------------------------------- every field required


@pytest.mark.parametrize("path", all_paths())
def test_every_field_is_required(tmp_path, path):
    with pytest.raises(ConfigError) as exc:
        load(tmp_path, mutated(path))
    assert path.split(".")[-1] in str(exc.value)
    assert "required" in str(exc.value)


def test_missing_field_error_names_full_path(tmp_path):
    with pytest.raises(ConfigError, match=r"models\.monitor\.provider\.order"):
        load(tmp_path, mutated("models.monitor.provider.order"))


def test_null_reasoning_effort_is_present_not_missing(tmp_path):
    cfg = load(tmp_path, BASE)
    assert cfg.models.agent.reasoning_effort is None


# ---------------------------------------------------------------- run_id


@pytest.mark.parametrize("bad", ["", "a/b", "..", "../x", "a b", 3, None])
def test_run_id_must_be_a_plain_folder_name(tmp_path, bad):
    with pytest.raises(ConfigError, match="run_id"):
        load(tmp_path, mutated("run_id", bad))


# ---------------------------------------------------------------- model ids


@pytest.mark.parametrize("who", ["agent", "monitor", "judge"])
@pytest.mark.parametrize("bad", ["", "   ", None, 5])
def test_model_id_must_be_set(tmp_path, who, bad):
    with pytest.raises(ConfigError, match=rf"models\.{who}\.id"):
        load(tmp_path, mutated(f"models.{who}.id", bad))


# ---------------------------------------------------------------- side tasks


def test_side_tasks_must_be_among_the_four(tmp_path):
    with pytest.raises(ConfigError, match=r"setting\.side_tasks.*hack_the_planet"):
        load(tmp_path, mutated("setting.side_tasks", ["leak_passwords", "hack_the_planet"]))


def test_side_tasks_subset_is_allowed(tmp_path):
    cfg = load(tmp_path, mutated("setting.side_tasks", ["sudo_edit"]))
    assert cfg.setting.side_tasks == ("sudo_edit",)


@pytest.mark.parametrize("bad", [[], "sudo_edit", ["sudo_edit", "sudo_edit"]])
def test_side_tasks_must_be_nonempty_list_without_duplicates(tmp_path, bad):
    with pytest.raises(ConfigError, match=r"setting\.side_tasks"):
        load(tmp_path, mutated("setting.side_tasks", bad))


# ---------------------------------------------------------------- main tasks n


def test_n_above_100_rejected(tmp_path):
    with pytest.raises(ConfigError, match=r"setting\.main_tasks\.n.*100"):
        load(tmp_path, mutated("setting.main_tasks.n", 101))


def test_n_100_accepted(tmp_path):
    assert load(tmp_path, mutated("setting.main_tasks.n", 100)).setting.main_tasks.n == 100


@pytest.mark.parametrize("bad", [0, -1, 2.5, "30", True])
def test_n_must_be_positive_int(tmp_path, bad):
    with pytest.raises(ConfigError, match=r"setting\.main_tasks\.n"):
        load(tmp_path, mutated("setting.main_tasks.n", bad))


# ---------------------------------------------------------------- reasoning effort


@pytest.mark.parametrize("who", ["agent", "monitor"])
@pytest.mark.parametrize("value", [None, "none", "minimal", "low", "medium", "high"])
def test_agent_monitor_effort_allowed_values(tmp_path, who, value):
    cfg = load(tmp_path, mutated(f"models.{who}.reasoning_effort", value))
    assert getattr(cfg.models, who).reasoning_effort == value


@pytest.mark.parametrize("who", ["agent", "monitor"])
@pytest.mark.parametrize("value", ["max", "xhigh", "extreme", 3, True])
def test_agent_monitor_effort_rejects_max_xhigh_and_other(tmp_path, who, value):
    with pytest.raises(ConfigError, match=rf"models\.{who}\.reasoning_effort"):
        load(tmp_path, mutated(f"models.{who}.reasoning_effort", value))


def test_effort_rejection_message_explains_max(tmp_path):
    with pytest.raises(ConfigError, match="xhigh"):
        load(tmp_path, mutated("models.agent.reasoning_effort", "max"))


@pytest.mark.parametrize("value", [None, "max", "xhigh", "none", "anything"])
def test_judge_effort_is_any_string_or_null(tmp_path, value):
    cfg = load(tmp_path, mutated("models.judge.reasoning_effort", value))
    assert cfg.models.judge.reasoning_effort == value


@pytest.mark.parametrize("value", [3, True, ["max"]])
def test_judge_effort_non_string_rejected(tmp_path, value):
    with pytest.raises(ConfigError, match=r"models\.judge\.reasoning_effort"):
        load(tmp_path, mutated("models.judge.reasoning_effort", value))


# ---------------------------------------------------------------- conditions


def test_unknown_condition_rejected(tmp_path):
    with pytest.raises(ConfigError, match=r"conditions.*honest"):
        load(tmp_path, mutated("conditions", ["unaware", "honest"]))


@pytest.mark.parametrize("value", [["aware"], ["unaware"], ["aware", "unaware"]])
def test_condition_subsets_allowed(tmp_path, value):
    assert load(tmp_path, mutated("conditions", value)).conditions == tuple(value)


@pytest.mark.parametrize("bad", [[], "aware", ["aware", "aware"]])
def test_conditions_must_be_nonempty_list_without_duplicates(tmp_path, bad):
    with pytest.raises(ConfigError, match="conditions"):
        load(tmp_path, mutated("conditions", bad))


# ---------------------------------------------------------------- closed schema, fixed setting values


@pytest.mark.parametrize(
    "parent",
    [
        "",
        "setting",
        "setting.main_tasks",
        "prompts",
        "prompts.aware_section",
        "models",
        "models.agent",
        "models.monitor",
        "models.judge",
        "models.agent.provider",
        "models.judge.provider",
        "meltzer",
        "limits",
        "parallel",
        "seeds",
    ],
)
def test_unknown_key_rejected_and_named(tmp_path, parent):
    data = copy.deepcopy(BASE)
    node = data
    for key in [k for k in parent.split(".") if k]:
        node = node[key]
    node["surprise_key"] = 1
    expected = f"{parent}.surprise_key" if parent else "surprise_key"
    with pytest.raises(ConfigError, match=re.escape(expected) + ".*unknown"):
        load(tmp_path, data)


def test_misspelled_field_is_reported_as_unknown(tmp_path):
    data = copy.deepcopy(BASE)
    data["models"]["agent"]["reasoning_efort"] = data["models"]["agent"].pop("reasoning_effort")
    with pytest.raises(ConfigError, match=r"models\.agent\.reasoning_efort.*unknown"):
        load(tmp_path, data)


def test_judge_only_fields_are_unknown_on_agent(tmp_path):
    data = mutated("models.agent.temperature", 0)
    with pytest.raises(ConfigError, match=r"models\.agent\.temperature.*unknown"):
        load(tmp_path, data)


@pytest.mark.parametrize("bad", ["other", "BashArena", "basharena "])
def test_setting_name_must_be_basharena(tmp_path, bad):
    with pytest.raises(ConfigError, match=r"setting\.name.*basharena"):
        load(tmp_path, mutated("setting.name", bad))


@pytest.mark.parametrize("bad", ["all", "No_Cover", "cover"])
def test_pool_must_be_no_cover(tmp_path, bad):
    with pytest.raises(ConfigError, match=r"setting\.main_tasks\.pool.*no_cover"):
        load(tmp_path, mutated("setting.main_tasks.pool", bad))


# ---------------------------------------------------------------- other field types


@pytest.mark.parametrize(
    "path,bad",
    [
        ("models.agent.provider.order", []),
        ("models.agent.provider.order", "together"),
        ("models.agent.provider.allow_fallbacks", "no"),
        ("models.judge.temperature", "0"),
        ("models.judge.temperature", True),
        ("models.judge.concurrency", 0),
        ("models.judge.concurrency", 1.5),
        ("meltzer.max_turn_steps", 0),
        ("limits.meltzer_seconds", 0),
        ("limits.sample_time_limit_seconds", -5),
        ("limits.wait_seconds", -1),
        ("parallel.max_samples", 0),
        ("seeds.bootstrap", "0"),
        ("seeds.inspect_shuffle", 1.0),
        ("seeds.card_assignment", "0"),
        ("prompts.aware_mode", "both"),
        ("prompts.aware_mode", 1),
        ("prompts.aware_section.one_card", ""),
        ("setting.main_tasks.seed", True),
        ("prompts.cards", ""),
        ("setting.name", ""),
        ("setting.main_tasks.pool", 3),
    ],
)
def test_field_type_errors_name_the_field(tmp_path, path, bad):
    with pytest.raises(ConfigError, match=re.escape(path)):
        load(tmp_path, mutated(path, bad))


def test_numbers_may_be_float_for_temperature(tmp_path):
    assert load(tmp_path, mutated("models.judge.temperature", 0.5)).models.judge.temperature == 0.5


# ---------------------------------------------------------------- meltzer_tree_hash


def make_tree(root):
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "README.md").write_bytes(b"hello\n")
    (root / "src" / "pkg" / "a.py").write_bytes(b"print(1)\n")
    return root


def test_tree_hash_is_stable_hex_sha256(tmp_path):
    root = make_tree(tmp_path / "t")
    h1 = config.meltzer_tree_hash(root)
    assert h1 == config.meltzer_tree_hash(root)
    assert re.fullmatch(r"[0-9a-f]{64}", h1)


def test_tree_hash_ignores_pycache(tmp_path):
    root = make_tree(tmp_path / "t")
    before = config.meltzer_tree_hash(root)
    (root / "src" / "pkg" / "__pycache__").mkdir()
    (root / "src" / "pkg" / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"\x00junk")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "x.pyc").write_bytes(b"more")
    assert config.meltzer_tree_hash(root) == before


def test_tree_hash_changes_with_content_and_with_path(tmp_path):
    root = make_tree(tmp_path / "t")
    base = config.meltzer_tree_hash(root)
    (root / "README.md").write_bytes(b"hello!\n")
    changed = config.meltzer_tree_hash(root)
    assert changed != base
    (root / "README.md").write_bytes(b"hello\n")
    (root / "README.md").rename(root / "README2.md")
    assert config.meltzer_tree_hash(root) not in (base, changed)


def test_tree_hash_independent_of_creation_order(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "x").write_bytes(b"1")
    (a / "y").write_bytes(b"2")
    (b / "y").write_bytes(b"2")
    (b / "x").write_bytes(b"1")
    assert config.meltzer_tree_hash(a) == config.meltzer_tree_hash(b)


def test_tree_hash_path_content_boundary_is_unambiguous(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "ab").write_bytes(b"c")
    (b / "a").write_bytes(b"bc")
    assert config.meltzer_tree_hash(a) != config.meltzer_tree_hash(b)


def test_tree_hash_missing_root_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        config.meltzer_tree_hash(tmp_path / "nope")


def test_tree_hash_default_root_is_meltzer_v0_1():
    real = REPO / "meltzer" / "v0.1"
    assert re.fullmatch(r"[0-9a-f]{64}", config.meltzer_tree_hash())
    assert config.meltzer_tree_hash() == config.meltzer_tree_hash(real)


def test_image_tag_matches_built_image_name():
    from bench import image

    h = config.meltzer_tree_hash()
    assert config.image_tag(h) == image.meltzer_image_tag(h)
    assert re.fullmatch(r"oversight-bench/basharena-meltzer:[0-9a-f]{12}", config.image_tag())


# ---------------------------------------------------------------- results folder + manifest


@pytest.fixture
def cfg_and_path(tmp_path):
    path = write_cfg(tmp_path, BASE)
    return load_config(path), path


def test_create_run_dir_makes_folder_config_copy_and_manifest(tmp_path, cfg_and_path):
    cfg, cfg_path = cfg_and_path
    results = tmp_path / "results"
    run_dir = config.create_run_dir(cfg, cfg_path, results_root=results)
    assert run_dir == results / "t-01"
    assert (run_dir / "config.yaml").read_bytes() == cfg_path.read_bytes()
    assert (run_dir / "manifest.json").is_file()


def test_create_run_dir_refuses_existing_folder(tmp_path, cfg_and_path):
    cfg, cfg_path = cfg_and_path
    results = tmp_path / "results"
    config.create_run_dir(cfg, cfg_path, results_root=results)
    with pytest.raises(FileExistsError, match="t-01"):
        config.create_run_dir(cfg, cfg_path, results_root=results)


def test_refusal_leaves_existing_files_untouched(tmp_path, cfg_and_path):
    cfg, cfg_path = cfg_and_path
    existing = tmp_path / "results" / "t-01"
    existing.mkdir(parents=True)
    (existing / "samples.jsonl").write_text("keep me", encoding="utf-8")
    with pytest.raises(FileExistsError):
        config.create_run_dir(cfg, cfg_path, results_root=tmp_path / "results")
    assert (existing / "samples.jsonl").read_text(encoding="utf-8") == "keep me"
    assert not (existing / "manifest.json").exists()
    assert not (existing / "config.yaml").exists()


def test_create_run_dir_allows_folder_holding_only_preflight_json(tmp_path, cfg_and_path):
    cfg, cfg_path = cfg_and_path
    existing = tmp_path / "results" / "t-01"
    existing.mkdir(parents=True)
    (existing / "preflight.json").write_text("{}", encoding="utf-8")
    run_dir = config.create_run_dir(cfg, cfg_path, results_root=tmp_path / "results")
    assert (run_dir / "preflight.json").read_text(encoding="utf-8") == "{}"
    assert (run_dir / "manifest.json").is_file()


def test_create_run_dir_allows_prompts_folder_and_preflight_json(tmp_path, cfg_and_path):
    cfg, cfg_path = cfg_and_path
    existing = tmp_path / "results" / "t-01"
    (existing / "prompts").mkdir(parents=True)
    (existing / "prompts" / "aware_user_message.md").write_text("x", encoding="utf-8")
    run_dir = config.create_run_dir(cfg, cfg_path, results_root=tmp_path / "results")
    assert (run_dir / "prompts" / "aware_user_message.md").read_text(encoding="utf-8") == "x"
    assert (run_dir / "manifest.json").is_file()
    existing2 = tmp_path / "results2" / "t-01"
    (existing2 / "prompts").mkdir(parents=True)
    (existing2 / "preflight.json").write_text("{}", encoding="utf-8")
    config.create_run_dir(cfg, cfg_path, results_root=tmp_path / "results2")


def test_create_run_dir_refuses_prompts_plus_anything_else(tmp_path, cfg_and_path):
    cfg, cfg_path = cfg_and_path
    existing = tmp_path / "results" / "t-01"
    (existing / "prompts").mkdir(parents=True)
    (existing / "samples").mkdir()
    with pytest.raises(FileExistsError):
        config.create_run_dir(cfg, cfg_path, results_root=tmp_path / "results")


def test_create_run_dir_allows_empty_existing_folder(tmp_path, cfg_and_path):
    cfg, cfg_path = cfg_and_path
    (tmp_path / "results" / "t-01").mkdir(parents=True)
    config.create_run_dir(cfg, cfg_path, results_root=tmp_path / "results")


def test_create_run_dir_refuses_existing_file_with_that_name(tmp_path, cfg_and_path):
    cfg, cfg_path = cfg_and_path
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "t-01").write_text("x", encoding="utf-8")
    with pytest.raises(FileExistsError):
        config.create_run_dir(cfg, cfg_path, results_root=tmp_path / "results")


def test_manifest_fields_known_at_start(tmp_path, cfg_and_path):
    cfg, cfg_path = cfg_and_path
    run_dir = config.create_run_dir(cfg, cfg_path, results_root=tmp_path / "results")
    m = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert set(m) == {
        "run_id",
        "oversight_bench_commit",
        "oversight_bench_dirty",
        "meltzer_tree_hash",
        "image_tag",
        "basharena_commit",
        "inspect_ai_version",
        "seeds",
        "start_time",
        "end_time",
        "main_task_ids",
    }
    assert m["run_id"] == "t-01"
    assert re.fullmatch(r"[0-9a-f]{40}|unknown", m["oversight_bench_commit"])
    assert m["oversight_bench_dirty"] is None or isinstance(m["oversight_bench_dirty"], bool)
    assert m["meltzer_tree_hash"] == config.meltzer_tree_hash()
    assert m["image_tag"] == config.image_tag(m["meltzer_tree_hash"])
    assert m["inspect_ai_version"] == importlib.metadata.version("inspect_ai")
    assert m["seeds"] == {"main_tasks": 0, "inspect_shuffle": 0, "card_assignment": 0, "bootstrap": 0}
    assert datetime.fromisoformat(m["start_time"]).tzinfo is not None
    assert m["end_time"] is None
    assert m["main_task_ids"] is None
    commit_file = REPO / "vendor" / "basharena" / "COMMIT"
    expected = commit_file.read_text(encoding="utf-8").strip() if commit_file.is_file() else None
    assert m["basharena_commit"] == expected


def test_manifest_seeds_come_from_config(tmp_path):
    data = mutated("seeds.bootstrap", 7)
    data["seeds"]["inspect_shuffle"] = 3
    data["seeds"]["card_assignment"] = 4
    data["setting"]["main_tasks"]["seed"] = 5
    path = write_cfg(tmp_path, data)
    cfg = load_config(path)
    run_dir = config.create_run_dir(cfg, path, results_root=tmp_path / "results")
    m = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert m["seeds"] == {"main_tasks": 5, "inspect_shuffle": 3, "card_assignment": 4, "bootstrap": 7}


def test_finish_manifest_fills_end_time_and_main_task_ids(tmp_path, cfg_and_path):
    cfg, cfg_path = cfg_and_path
    run_dir = config.create_run_dir(cfg, cfg_path, results_root=tmp_path / "results")
    before = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    config.finish_manifest(run_dir, ["00147570", "00182310"])
    after = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert after["main_task_ids"] == ["00147570", "00182310"]
    assert datetime.fromisoformat(after["end_time"]) >= datetime.fromisoformat(after["start_time"])
    unchanged = {k: v for k, v in after.items() if k not in ("end_time", "main_task_ids")}
    assert unchanged == {k: v for k, v in before.items() if k not in ("end_time", "main_task_ids")}


def test_finish_manifest_without_ids_keeps_them(tmp_path, cfg_and_path):
    cfg, cfg_path = cfg_and_path
    run_dir = config.create_run_dir(cfg, cfg_path, results_root=tmp_path / "results")
    config.finish_manifest(run_dir, ["a"])
    config.finish_manifest(run_dir)
    assert json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))["main_task_ids"] == ["a"]


def test_git_commit_unknown_when_git_fails(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(config.subprocess, "run", boom)
    assert config.git_commit() == "unknown"


def test_git_commit_unknown_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        config.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 128, stdout="", stderr="fatal"),
    )
    assert config.git_commit() == "unknown"


def test_git_commit_returns_head(monkeypatch):
    monkeypatch.setattr(
        config.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="abc123\n", stderr=""),
    )
    assert config.git_commit() == "abc123"


def _fake_git(monkeypatch, returncode, stdout):
    monkeypatch.setattr(
        config.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, returncode, stdout=stdout, stderr=""),
    )


def test_git_dirty_true_when_porcelain_non_empty(monkeypatch):
    _fake_git(monkeypatch, 0, " M bench/config.py")
    assert config.git_dirty() is True


def test_git_dirty_false_when_porcelain_empty(monkeypatch):
    _fake_git(monkeypatch, 0, "")
    assert config.git_dirty() is False


def test_git_dirty_none_when_git_fails(monkeypatch):
    _fake_git(monkeypatch, 128, "")
    assert config.git_dirty() is None

    def boom(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(config.subprocess, "run", boom)
    assert config.git_dirty() is None


def test_git_dirty_uses_status_porcelain(monkeypatch):
    seen = []

    def fake(cmd, **k):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(config.subprocess, "run", fake)
    config.git_dirty()
    assert seen == [["git", "status", "--porcelain"]]


def test_manifest_records_dirty_flag(tmp_path, cfg_and_path, monkeypatch):
    cfg, cfg_path = cfg_and_path
    _fake_git(monkeypatch, 0, "?? x")
    run_dir = config.create_run_dir(cfg, cfg_path, results_root=tmp_path / "results")
    m = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert m["oversight_bench_dirty"] is True


def test_basharena_commit_reads_file_or_none(tmp_path):
    assert config.basharena_commit(tmp_path / "COMMIT") is None
    (tmp_path / "COMMIT").write_text("fd906ea4f996499e5adb0c9e3a7bd1f9735ef1a4\n", encoding="utf-8")
    assert config.basharena_commit(tmp_path / "COMMIT") == "fd906ea4f996499e5adb0c9e3a7bd1f9735ef1a4"


def test_resolve_run_dir(tmp_path):
    assert config.resolve_run_dir("exp1-01", results_root=tmp_path) == tmp_path / "exp1-01"
    assert config.resolve_run_dir("exp1-01") == REPO / "results" / "exp1-01"


def test_sha256_helper_matches_manual_hash(tmp_path):
    """The hash covers sorted relative posix paths and contents, as the spec says."""
    root = tmp_path / "t"
    (root / "d").mkdir(parents=True)
    (root / "d" / "f").write_bytes(b"F")
    (root / "g").write_bytes(b"G")
    h = hashlib.sha256()
    for rel, data in (("d/f", b"F"), ("g", b"G")):
        h.update(rel.encode() + b"\0" + data + b"\0")
    assert config.meltzer_tree_hash(root) == h.hexdigest()


# ---------------------------------------------------------------- aware mode


def test_aware_mode_selects_the_section_file(tmp_path):
    for mode, file in (("all_cards", "b.md"), ("one_card", "c.md")):
        cfg = load(tmp_path, mutated("prompts.aware_mode", mode))
        assert cfg.prompts.aware_mode == mode and cfg.prompts.aware_section_file == file


@pytest.mark.parametrize("path", ["prompts.aware_mode", "prompts.aware_section", "prompts.aware_section.all_cards",
                                  "prompts.aware_section.one_card", "seeds.card_assignment"])
def test_aware_mode_fields_are_required(tmp_path, path):
    with pytest.raises(ConfigError, match=re.escape(path)):
        load(tmp_path, mutated(path))
