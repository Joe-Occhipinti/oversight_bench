"""Taxonomy proposal (`python -m bench failure-taxonomy <run_id>... [--out DIR]`, spec-failure-analysis 6).

Reads the extraction outputs (`<run_dir>/failure_extract.jsonl`) of the runs given, collects the values of each
free-text field below, and runs one Docent reading per field over all its values (prompt
`failure_taxonomy_prompt.yaml`). Values are plain text: Docent's citation markup is removed, so the proposal
names examples by value id, and the review file links each example to its transcript in Docent.

Writes, in DIR (default `results/failure-taxonomy`):
- `proposal.json`: per field, the values (id, sample, run, attempt fields, text), the reading's output and
  its token counts;
- `proposal.md`: per field, each proposed category with its definition, borderline rule and examples, then
  the unassigned values and the notes.

Then a second reading per field assigns every value to one proposed category (`assignments` in each field of
`proposal.json`). These labels are provisional: the categories are not yet reviewed by Gio (spec 6-7).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path

import yaml

from bench.asr import run_label
from bench.config import RunConfig
from bench.failures import MAX_NEW_TOKENS, MODEL, OUT_FILE, REASONING_EFFORT, condition_key
from bench.judge import render_template

PROMPT_PATH = Path(__file__).with_name("failure_taxonomy_prompt.yaml")
CITATION = re.compile(r"\s?\[T\d+B\d+(?::<RANGE>.*?</RANGE>)?\]", re.S)
RANGE_TAG = re.compile(r"</?RANGE>")
FIELD_ORDER = ("failure_reason", "mechanism", "card_choice", "idea_formation", "harness_awareness_trigger",
               "engagement")  # fmt: skip

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "categories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "definition": {"type": "string"},
                    "borderline_rule": {"type": "string"},
                    "examples": {"type": "array", "items": {"type": "string", "description": "value id"}},
                },
                "required": ["name", "definition", "borderline_rule", "examples"],
            },
        },
        "unassigned": {"type": "array", "items": {"type": "string", "description": "value id"}},
        "notes": {"type": "string"},
    },
    "required": ["categories", "unassigned", "notes"],
}


def plain(value) -> str:
    """The text of an extraction field without Docent's citation markup."""
    text = value.get("text", "") if isinstance(value, dict) else (value or "")
    return RANGE_TAG.sub("", CITATION.sub("", text)).strip()


# Each selector yields (attempt fields shown with the value, text) for one extraction output.
Selector = Callable[[dict, str], Iterator[tuple[dict, str]]]


def _attempt_fields(a: dict) -> dict:
    return {"attempt": a.get("attempt_id"), "type": a.get("type"), "card": a.get("card_id") or None,
            "outcome": a.get("outcome")}  # fmt: skip


def _failure_reason(out: dict, key: str):
    for a in out.get("attempts") or []:
        if a.get("outcome") != "side_task_done" and plain(a.get("failure_reason")):
            yield _attempt_fields(a), plain(a["failure_reason"])


def _mechanism(out: dict, key: str):
    for a in out.get("attempts") or []:
        if a.get("type") in ("no_harness", "unlisted_harness") and plain(a.get("mechanism")):
            yield _attempt_fields(a), plain(a["mechanism"])


def _run_field(name: str, keys: tuple[str, ...]) -> Selector:
    def select(out: dict, key: str):
        if key in keys and plain(out.get(name)):
            yield {}, plain(out[name])

    return select


def _trigger(out: dict, key: str):
    aw = out.get("harness_awareness") or {}
    if key == "unaware" and aw.get("treats_harness_as_target") and plain(aw.get("trigger")):
        yield {}, f"Considered: {plain(aw.get('evidence'))}\nWhat led to it: {plain(aw['trigger'])}"


def _engagement(out: dict, key: str):
    if out.get("engagement") and out["engagement"] != "attempted":
        yield {"engagement": out["engagement"]}, plain(out.get("engagement_evidence"))


SELECTORS: dict[str, Selector] = {
    "failure_reason": _failure_reason,
    "mechanism": _mechanism,
    "card_choice": _run_field("card_choice", ("all_cards",)),
    "idea_formation": _run_field("idea_formation", ("explore",)),
    "harness_awareness_trigger": _trigger,
    "engagement": _engagement,
}


def collect_values(runs: list[tuple[Path, RunConfig]]) -> dict[str, list[dict]]:
    """Field -> values, each {id, sample_id, agent_run_id, run_id, run, ...attempt fields, text}; ids v1, v2, ...
    per field."""
    values: dict[str, list[dict]] = {field: [] for field in FIELD_ORDER}
    for run_dir, cfg in runs:
        key, label = condition_key(cfg), run_label(cfg)
        rows = [json.loads(line) for line in (Path(run_dir) / OUT_FILE).read_text(encoding="utf-8").splitlines() if line]
        for row in rows:
            if row.get("status") != "completed" or not row.get("output"):
                continue
            for field in FIELD_ORDER:
                for extra, text in SELECTORS[field](row["output"], key):
                    values[field].append({"sample_id": row["sample_id"], "agent_run_id": row["agent_run_id"],
                                          "run_id": cfg.run_id, "run": label, **extra, "text": text})  # fmt: skip
    for items in values.values():
        for n, item in enumerate(items, start=1):
            item["id"] = f"v{n}"
    return {field: items for field, items in values.items() if items}


def _value_line(item: dict) -> str:
    where = [f"run: {item['run']}"] + [f"{k}: {item[k]}" for k in ("attempt", "type", "card", "outcome", "engagement")
                                       if item.get(k) is not None]  # fmt: skip
    return f"[{item['id']}] ({'; '.join(where)})\n{item['text']}"


def build_prompt(field: str, items: list[dict], prompt: dict | None = None) -> str:
    prompt = prompt or yaml.safe_load(PROMPT_PATH.read_text(encoding="utf-8"))
    spec = prompt["fields"][field]
    return render_template(prompt["prompt"], {
        "field": field, "question": spec["question"], "context": spec["context"],
        "values": "\n\n".join(_value_line(item) for item in items),
    })  # fmt: skip


def propose(runs: list[tuple[Path, RunConfig]], client, collection_id: str, out_dir: Path) -> dict:
    values = collect_values(runs)
    prompt = yaml.safe_load(PROMPT_PATH.read_text(encoding="utf-8"))
    readings = {
        field: client.read(
            prompts_list=[[build_prompt(field, items, prompt)]],
            collection_id=collection_id,
            model=MODEL,
            output_schema=OUTPUT_SCHEMA,
            reasoning_effort=REASONING_EFFORT,
            max_new_tokens=MAX_NEW_TOKENS,
            cache_mode="results",
            name=f"taxonomy proposal: {field}",
        )
        for field, items in values.items()
    }
    client.flush(open_in_browser=False, auto_approve=True)
    proposal = {"runs": [cfg.run_id for _, cfg in runs], "collection_id": collection_id, "fields": {}}
    for field, reading in readings.items():
        (r,) = client.get_reading_results(collection_id, reading.id)
        proposal["fields"][field] = {
            "values": values[field],
            "reading_id": reading.id,
            **{k: r.get(k) for k in ("status", "error", "input_tokens", "output_tokens")},
            "output": r.get("output"),
        }
    assign(proposal, client, collection_id, prompt)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "proposal.json").write_text(json.dumps(proposal, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out_dir / "proposal.md").write_text(render_markdown(proposal, client.frontend_url), encoding="utf-8")
    return proposal


UNASSIGNED = "unassigned"


def assignment_schema(names: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "assignments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"value_id": {"type": "string"},
                                   "category": {"type": "string", "enum": [*names, UNASSIGNED]}},
                    "required": ["value_id", "category"],
                },  # fmt: skip
            }
        },
        "required": ["assignments"],
    }


def build_assignment_prompt(field: str, items: list[dict], categories: list[dict], prompt: dict) -> str:
    cats = "\n\n".join(
        f"## {c['name']}\n{c['definition']}\nBorderline: {c['borderline_rule']}" for c in categories
    )
    return render_template(prompt["assign"], {
        "field": field, "question": prompt["fields"][field]["question"], "categories": cats,
        "values": "\n\n".join(_value_line(item) for item in items),
    })  # fmt: skip


def assign(proposal: dict, client, collection_id: str, prompt: dict) -> None:
    """Provisional labels: one reading per field assigns every value to one proposed category (or
    `unassigned`). Stored as `assignments` {value id: category} in each field."""
    readings = {}
    for field, data in proposal["fields"].items():
        categories = (data.get("output") or {}).get("categories") or []
        if not categories:
            continue
        readings[field] = client.read(
            prompts_list=[[build_assignment_prompt(field, data["values"], categories, prompt)]],
            collection_id=collection_id,
            model=MODEL,
            output_schema=assignment_schema([c["name"] for c in categories]),
            reasoning_effort=REASONING_EFFORT,
            max_new_tokens=MAX_NEW_TOKENS,
            cache_mode="results",
            name=f"taxonomy assignment: {field}",
        )
    client.flush(open_in_browser=False, auto_approve=True)
    for field, reading in readings.items():
        (r,) = client.get_reading_results(collection_id, reading.id)
        data = proposal["fields"][field]
        out = r.get("output") or {}
        given = {a["value_id"]: a["category"] for a in out.get("assignments") or []}
        data["assignments"] = {v["id"]: given.get(v["id"], UNASSIGNED) for v in data["values"]}
        data["assignment"] = {"reading_id": reading.id, "missing": sorted(set(data["assignments"]) - set(given)),
                              **{k: r.get(k) for k in ("status", "error", "input_tokens", "output_tokens")}}  # fmt: skip


def render_markdown(proposal: dict, frontend_url: str) -> str:
    cid = proposal["collection_id"]
    lines = ["# Taxonomy proposal", "", f"Runs: {', '.join(proposal['runs'])}.", ""]
    for field, data in proposal["fields"].items():
        by_id = {v["id"]: v for v in data["values"]}

        def link(vid: str) -> str:
            v = by_id.get(vid)
            if v is None:
                return f"{vid} (unknown id)"
            where = f"{v['run']}, attempt {v['attempt']}" if v.get("attempt") is not None else v["run"]
            return f"[{vid}]({frontend_url}/dashboard/{cid}/agent_run/{v['agent_run_id']}) ({where}, {v['sample_id']})"

        lines += [f"## {field}", "", f"{len(data['values'])} values.", ""]
        out = data.get("output")
        if not out:
            lines += [f"Reading {data.get('status')}: {data.get('error')}", ""]
            continue
        counts = Counter(data.get("assignments", {}).values())
        for n, cat in enumerate(out["categories"], start=1):
            count = f" ({counts[cat['name']]} values, provisional)" if counts else ""
            lines += [f"### {n}. {cat['name']}{count}", "", cat["definition"], "",
                      f"Borderline: {cat['borderline_rule']}", ""]  # fmt: skip
            for vid in cat["examples"]:
                text = by_id[vid]["text"] if vid in by_id else ""
                lines += [f"- {link(vid)}: {text[:400]}{'...' if len(text) > 400 else ''}"]
            lines.append("")
        if out["unassigned"]:
            lines += ["### Unassigned", ""] + [f"- {link(vid)}" for vid in out["unassigned"]] + [""]
        if out.get("notes"):
            lines += ["### Notes", "", out["notes"], ""]
    return "\n".join(lines)
