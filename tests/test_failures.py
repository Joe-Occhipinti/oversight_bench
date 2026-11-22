from pathlib import Path

import pytest
import yaml
from docent.judges.util.meta_schema import validate_judge_result_schema

from bench import failures as F
from bench.config import load_config

REPO = Path(__file__).resolve().parent.parent
KEYS = ("unaware", "explore", "all_cards", "one_card")


def cfg_for(tmp_path, key):
    data = yaml.safe_load((REPO / "runs" / "exp1-med98-all.yaml").read_text(encoding="utf-8"))
    data["conditions"] = ["unaware"] if key == "unaware" else ["aware"]
    if key != "unaware":
        data["prompts"]["aware_mode"] = key
    path = tmp_path / f"{key}.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_config(path)


@pytest.mark.parametrize("key", KEYS)
def test_schema_passes_docent_meta_schema(key):
    validate_judge_result_schema(F.output_schema(key))


def test_condition_fields():
    attempt = lambda key: F.output_schema(key)["properties"]["attempts"]["items"]["properties"]  # noqa: E731
    run = lambda key: set(F.output_schema(key)["properties"])  # noqa: E731
    common = {"attempts", "engagement", "engagement_evidence"}
    assert run("unaware") == common | {"harness_awareness"}
    assert run("explore") == common | {"idea_formation"}
    assert run("all_cards") == common | {"card_choice"}
    assert run("one_card") == common
    assert "understood_mechanism" in attempt("unaware")
    assert all("understood_mechanism" not in attempt(k) for k in ("explore", "all_cards", "one_card"))
    assert attempt("one_card")["outcome"]["enum"] == F.OUTCOMES


@pytest.mark.parametrize("key", KEYS)
def test_instructions_condition_part_and_placeholders(tmp_path, key):
    cfg = cfg_for(tmp_path, key)
    assert F.condition_key(cfg) == key
    text = F.instructions(cfg, "leak_passwords")
    assert "{{" not in text
    assert "## Card E01" in text and "## Card E14" in text  # all cards in every condition
    heading = "(condition unaware)" if key == "unaware" else f"mode {key})"
    assert heading in text
    others = [k for k in KEYS if k != key]
    assert all(("(condition unaware)" if k == "unaware" else f"mode {k})") not in text for k in others)
    assert text.endswith("# Agent run\n")


def test_dql_filters_run_side_task_and_exclusions():
    dql = F._dql("exp1-med98-all", "leak_passwords")
    assert "metadata_json->>'run_id' = 'exp1-med98-all'" in dql
    assert "metadata_json->>'side_task' = 'leak_passwords'" in dql
    assert "metadata_json->>'excluded' IS NULL" in dql
