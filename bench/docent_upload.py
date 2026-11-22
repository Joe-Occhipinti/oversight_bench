"""Upload a run's samples to Docent (`python -m bench docent-upload <run_id>`, spec-failure-analysis 4).

One AgentRun per sample, with one transcript: the records of all its meltzer sessions in the judge's time order
(`bench.judge.ordered_rows`). Mapping from the session log to Docent messages:

- user `message` -> UserMessage; other user items (e.g. `plan`) -> UserMessage starting with `(<kind>)`, since
  meltzer gives them the user role;
- assistant `reasoning`, `message` and `tool_call` items with the same `response_id` -> one AssistantMessage
  (reasoning in full as ContentReasoning, text as ContentText, tool calls with their arguments);
- `tool_result` -> ToolMessage, truncated as in the judge;
- session start, monitor `review`, `item_updated`, `item_removed`, `compacted` and other records -> SystemMessage
  with the judge's rendering. Developer instructions are never in the log.

The log's order is kept, so Docent's checks warn about system messages after the start and about tool calls
whose result comes after a monitor review or a subagent session; both are expected.

Messages of a session other than the main one start with its tag (`[S2 subagent]`). Each message's metadata
has `entry` (1, 2, ... in the transcript), `session` (tag), `log_index` (record index in the session file of
its first record) and `time`.

The mapping sample_id -> agent run id is kept in `<run_dir>/docent.json`; samples already in it are skipped.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from docent.data_models import AgentRun, Transcript
from docent.data_models.chat import (
    AssistantMessage,
    ContentReasoning,
    ContentText,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)

from bench.asr import exclusion_reason, read_rows, run_label
from bench.config import RunConfig
from bench.judge import Row, _format_time, ordered_rows, row_entry, session_files, truncate

STATE_FILE = "docent.json"
JUDGE_KEYS = ("faithful", "card_id", "harness_exploit", "route_summary")
SAMPLE_KEYS = (
    "sample_id", "main_task", "side_task", "assigned_card", "status", "exit_code",
    "side_pass", "main_score", "end_reason", "time_limit_hit",
)  # fmt: skip


def reasoning_text(rec: dict) -> str:
    """The text of a reasoning item: `reasoning.reasoning_details[].text` (or `summary`), joined."""
    data = rec.get("reasoning")
    if isinstance(data, str):
        return data
    if not isinstance(data, dict):
        return ""
    parts = [d.get("text") or d.get("summary") or "" for d in data.get("reasoning_details") or []]
    return "\n\n".join(p for p in parts if p)


def _arguments(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {"raw": text}
    return data if isinstance(data, dict) else {"value": data}


def _prefix(row: Row) -> str:
    return "" if row.tag.endswith(" main") else f"[{row.tag}] "


def _meta(row: Row) -> dict[str, Any]:
    return {"session": row.tag, "log_index": row.seq, "time": _format_time(row.when)}


def build_messages(rows: list[Row]) -> list:
    messages: list = []
    open_turn: tuple[int, str] | None = None  # (session, response_id) of the last AssistantMessage
    for row in rows:
        rec = row.rec
        role, kind = rec.get("role"), rec.get("kind")
        if rec.get("type") == "item" and role == "assistant" and kind in ("reasoning", "message", "tool_call"):
            key = (row.session, rec.get("response_id") or f"_{row.seq}")
            if open_turn != key:
                messages.append(AssistantMessage(content=[], tool_calls=[], metadata=_meta(row)))
                open_turn = key
                if _prefix(row):
                    messages[-1].content.append(ContentText(text=_prefix(row).rstrip()))
            turn = messages[-1]
            if kind == "reasoning":
                turn.content.append(ContentReasoning(reasoning=reasoning_text(rec)))
            elif kind == "message":
                turn.content.append(ContentText(text=rec.get("content") or ""))
            else:
                call = ToolCall(id=rec.get("call_id") or "", function=rec.get("name") or "", type="function",
                                arguments=_arguments(rec.get("arguments")))  # fmt: skip
                turn.tool_calls.append(call)
            continue
        open_turn = None
        if rec.get("type") == "item" and kind == "tool_result":
            text = _prefix(row) + truncate(rec.get("content") or "")
            messages.append(ToolMessage(content=text, tool_call_id=rec.get("call_id"), function=rec.get("name"),
                                        metadata=_meta(row)))  # fmt: skip
            continue
        if rec.get("type") == "item" and role == "user":
            label = "" if kind == "message" else f"({kind})\n"
            messages.append(UserMessage(content=_prefix(row) + label + (rec.get("content") or ""), metadata=_meta(row)))
            continue
        entry = row_entry(row)
        if entry is None:
            continue
        header, body = entry
        messages.append(SystemMessage(content=f"{_prefix(row)}{header}\n{body}", metadata=_meta(row)))
    for n, message in enumerate(messages, start=1):
        message.metadata["entry"] = n
        if isinstance(message, AssistantMessage) and not message.tool_calls:
            message.tool_calls = None
    return messages


def review_counts(rows: list[Row]) -> dict[str, int]:
    return dict(Counter(str(r.rec.get("outcome")) for r in rows if r.rec.get("type") == "review"))


def run_metadata(row: dict, run_dir: Path, cfg: RunConfig) -> dict[str, Any]:
    judge = row.get("judge") or {}
    return {
        "run_id": cfg.run_id,
        "label": run_label(cfg),
        "condition": row.get("condition"),
        "aware_mode": cfg.prompts.aware_mode if row.get("condition") == "aware" else None,
        **{k: row.get(k) for k in SAMPLE_KEYS},
        "excluded": exclusion_reason(row, run_dir),
        "judge": {k: judge.get(k) for k in JUDGE_KEYS} if judge else None,
    }


def build_agent_run(row: dict, run_dir: Path, cfg: RunConfig) -> AgentRun | None:
    """None when the sample has no session logs."""
    files = session_files(Path(run_dir) / "samples" / row["sample_id"])
    if not files:
        return None
    rows = ordered_rows(files)
    metadata = run_metadata(row, run_dir, cfg) | {"monitor_reviews": review_counts(rows), "sessions": len(files)}
    transcript = Transcript(name="meltzer sessions", messages=build_messages(rows))
    return AgentRun(name=row["sample_id"], transcripts=[transcript], metadata=metadata)


def upload_run(run_dir: Path, cfg: RunConfig, client, collection_id: str) -> dict:
    """Upload the samples of `samples.jsonl` not yet in docent.json; returns the updated docent.json content."""
    state_path = Path(run_dir) / STATE_FILE
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    if state and state.get("collection_id") != collection_id:
        raise ValueError(f"{state_path} is for collection {state.get('collection_id')}, not {collection_id}")
    uploaded: dict[str, str] = state.get("agent_runs", {})
    runs, skipped = [], []
    for row in read_rows(Path(run_dir) / "samples.jsonl"):
        if row["sample_id"] in uploaded:
            continue
        run = build_agent_run(row, run_dir, cfg)
        if run is None:
            skipped.append(row["sample_id"])
            continue
        runs.append(run)
    if runs:
        client.add_agent_runs(collection_id, runs)
        uploaded.update({r.name: r.id for r in runs})
    state = {"collection_id": collection_id, "agent_runs": uploaded, "no_session_logs": skipped}
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(f"docent: {len(runs)} uploaded, {len(uploaded)} in collection, {len(skipped)} without session logs")
    return state
