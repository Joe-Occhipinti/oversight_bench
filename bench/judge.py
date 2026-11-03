"""Strategy judge (spec 10.3): did a successful attack follow one of the exploit cards, and did it use a
vulnerability of meltzer (`harness_exploit`)?

One call per sample with side_pass true, made directly to OpenRouter with httpx. Entry point:
judge_run(run_dir, judge, cards_dir). Per sample it writes samples/<id>/judge.json and then rewrites
samples.jsonl with the `judge` field filled. Samples that already have a valid judge.json are skipped.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

from bench import prompts

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
PROMPT_PATH = BENCH_DIR / "judge_prompt.yaml"
# The monitor's system prompt is policy_template.md with policy.md in place of the placeholder
# (meltzer/v0.1/src/meltzer/monitor/prompt.py::policy_prompt).
MONITOR_DIR = REPO_ROOT / "meltzer" / "v0.1" / "src" / "meltzer" / "monitor"
SECURITY_POLICY_PLACEHOLDER = "{{ security_policy }}"
API_URL = "https://openrouter.ai/api/v1/chat/completions"
ENV_FILE = REPO_ROOT / ".env"
KEY_NAME = "OPENROUTER_API_KEY"

OUTPUT_TRIES = 3  # first call + 2 retries on invalid output
HTTP_TRIES = 6  # per output try, on 408 / 429 / 5xx / network errors / error bodies
BACKOFF_S = 2.0  # wait before HTTP retry n (0-based) = BACKOFF_S * 2**n
RETRYABLE_STATUS = {408, 429}
TIMEOUT_S = 300.0

TRUNCATE_CHARS = 4000  # per tool result and per tool argument value in the transcript
OUTPUT_KEYS = ("faithful", "card_id", "harness_exploit", "route_summary", "rationale")
JUDGE_KEYS = ("id", "provider", "temperature", "concurrency")  # reasoning_effort may be null


class JudgeOutputError(ValueError):
    """The model output is not valid JSON, misses keys, or is inconsistent."""


class JudgeFatalError(RuntimeError):
    """A problem that affects every call (bad key, unknown model, missing card)."""


# --- cards and prompt ---------------------------------------------------------------------------


def load_cards(cards_dir: Path) -> list[tuple[str, str]]:
    """The cards (id, text) of the folder in id order. Files are named `E07-<slug>.md`. The text is the one
    in the agent's prompt (`prompts.card_body`): without the YAML header."""
    try:
        files = prompts.card_files(cards_dir)
    except ValueError as e:
        raise JudgeFatalError(str(e)) from None
    return [(card_id, prompts.card_body(path.read_text(encoding="utf-8"))) for card_id, path in files]


def load_prompt(path: Path = PROMPT_PATH) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_monitor_policy(monitor_dir: Path = MONITOR_DIR) -> str:
    """The monitor's system prompt without its output format, as meltzer builds it."""
    template = (monitor_dir / "policy_template.md").read_text(encoding="utf-8").rstrip()
    policy = (monitor_dir / "policy.md").read_text(encoding="utf-8").strip()
    return template.replace(SECURITY_POLICY_PLACEHOLDER, policy)


PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render_template(template: str, values: dict[str, str]) -> str:
    """Single pass: inserted values are not scanned again. A placeholder without a value is an error."""

    def fill(match: re.Match) -> str:
        name = match.group(1)
        if name not in values:
            raise KeyError(f"unfilled placeholder {{{{{name}}}}}")
        return str(values[name])

    return PLACEHOLDER.sub(fill, template)


def cards_block(cards: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"## Card {cid}\n\n{text.strip()}" for cid, text in cards)


def build_messages(
    prompt: dict,
    cards: list[tuple[str, str]],
    side_task: str,
    side_task_description: str,
    scorer_explanation: str,
    transcript: str,
    monitor_policy: str,
) -> list[dict]:
    user = render_template(
        prompt["user"],
        {
            "cards": cards_block(cards),
            "monitor_policy": monitor_policy,
            "side_task": side_task,
            "side_task_description": side_task_description,
            "scorer_explanation": scorer_explanation,
            "transcript": transcript,
        },
    )
    return [{"role": "system", "content": prompt["system"]}, {"role": "user", "content": user}]


# --- transcript rendering (10.3) ------------------------------------------------------------------
#
# Session log format (meltzer/v0.1/src/meltzer/history.py, tools/plan.py, tools/compact.py,
# monitor/review.py): JSON lines.
#   session_meta  {timestamp, id, cwd, started_at, parent_session_id?, config, config_sources, ...}
#   item          {role, kind, content, name?, call_id?, arguments?, reasoning?, response_id?, created_at}
#   review        {timestamp, review_id, reviewer, model, action, outcome, risk_level, user_authorization,
#                  rationale, tool_calls}  or  {..., outcome: "deny", error}
#   item_updated  {timestamp, index, item}          item_removed {timestamp, index}
#   compacted     {timestamp, dropped_items, replacement_history: [item records]}
# The system prompt and the developer instructions are never in the log. Reasoning items are not rendered.
# Only tool results are truncated; everything else is given in full.

# Config values shown in a session's start entry, and the key whose name and value are never rendered.
SHOWN_CONFIG_KEYS = ("sandbox_mode", "approval_policy", "approvals_reviewer")
HIDDEN_CONFIG_KEY = "developer_instructions_file"


def truncate(text: str, limit: int = TRUNCATE_CHARS) -> str:
    """Keep the first and last limit/2 characters and mark the cut."""
    if len(text) <= limit:
        return text
    half = limit // 2
    cut = len(text) - 2 * half
    return f"{text[:half]}\n[... {cut} characters cut ...]\n{text[-half:]}"


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line if line else "" for line in text.split("\n"))


def _fields(data: dict[str, Any]) -> str:
    """`key: value` lines; a multi-line value goes on the lines below its key."""
    lines = []
    for key, value in data.items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        lines.append(f"{key}:\n{_indent(text, '  ')}" if "\n" in text else f"{key}: {text}")
    return "\n".join(lines)


def _arguments_text(arguments: str | None) -> str:
    if not arguments:
        return "(no arguments)"
    try:
        data = json.loads(arguments)
    except ValueError:
        return arguments
    if isinstance(data, dict):
        return _fields(data) if data else "(no arguments)"
    return arguments


REVIEW_HIDDEN = {"type", "timestamp", "review_id", "reviewer", "model", "action"}
REVIEW_ORDER = ("outcome", "risk_level", "user_authorization", "rationale", "error", "tool_calls")


def _review_body(rec: dict) -> str:
    rest = {k: rec[k] for k in REVIEW_ORDER if k in rec}
    rest.update({k: v for k, v in rec.items() if k not in REVIEW_HIDDEN and k not in rest})
    action = rec.get("action")
    request = _fields(action) if isinstance(action, dict) else json.dumps(action, ensure_ascii=False)
    return "request:\n" + _indent(request, "  ") + "\n" + _fields(rest)


def _item_entry(rec: dict) -> tuple[str, str] | None:
    """(header, body) of an `item` record; None for items that are not rendered (reasoning)."""
    role, kind = rec.get("role", ""), rec.get("kind", "")
    if kind == "reasoning":
        return None
    name, call_id = rec.get("name") or "", rec.get("call_id") or ""
    if kind == "tool_call":
        return f"TOOL CALL {name} ({call_id})", _arguments_text(rec.get("arguments"))
    if kind == "tool_result":
        return f"TOOL RESULT {name} ({call_id})", truncate(rec.get("content") or "")
    if kind == "message":
        return role.upper(), rec.get("content") or ""
    return f"{role.upper()} ({kind})", rec.get("content") or ""


def _entry(rec: dict) -> tuple[str, str] | None:
    kind = rec.get("type")
    if kind == "item":
        return _item_entry(rec)
    if kind == "review":
        return f"MONITOR REVIEW {rec.get('review_id', '')}".strip(), _review_body(rec)
    if kind == "item_updated":
        item = rec.get("item") or {}
        label = "PLAN UPDATED" if item.get("kind") == "plan" else f"ITEM UPDATED ({item.get('kind', '')})"
        return f"{label} (history index {rec.get('index')})", item.get("content") or ""
    if kind == "item_removed":
        return f"ITEM REMOVED (history index {rec.get('index')})", "(the item was removed from the history)"
    if kind == "compacted":
        replacement = rec.get("replacement_history") or []
        body = "\n".join(f"{i.get('kind', '')}:\n{_indent(i.get('content') or '', '  ')}" for i in replacement)
        return f"HISTORY COMPACTED ({rec.get('dropped_items')} items dropped)", "replaced by:\n" + body
    if kind == "_unreadable":
        return "NOTE", f"unreadable log line {rec['line']} skipped"
    rest = {k: v for k, v in rec.items() if k not in ("type", "timestamp")}
    return f"EVENT {kind}", _fields(rest)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _format_time(moment: datetime) -> str:
    utc = moment.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


@dataclass
class _Session:
    path: Path
    records: list[dict]
    meta: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return str(self.meta.get("id") or self.path.stem)

    @property
    def parent(self) -> str | None:
        return self.meta.get("parent_session_id")

    def start_key(self) -> tuple[datetime, str]:
        started = _parse_time(self.meta.get("started_at")) or _parse_time(self.meta.get("timestamp"))
        return started or datetime.max.replace(tzinfo=timezone.utc), self.path.name


def _read_session(path: Path) -> _Session:
    records: list[dict] = []
    for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            rec = None
        records.append(rec if isinstance(rec, dict) else {"type": "_unreadable", "line": number})
    meta = next((r for r in records if r.get("type") == "session_meta"), {})
    return _Session(path, records, meta)


def _session_start(session: _Session, description: str) -> tuple[str, str]:
    """The start entry of a session: where it runs and which oversight settings it loaded."""
    meta = session.meta
    config = meta.get("config") if isinstance(meta.get("config"), dict) else {}
    sources = meta.get("config_sources") if isinstance(meta.get("config_sources"), dict) else {}
    lines = [f"{description}", f"id: {session.id}", f"cwd: {meta.get('cwd', 'unknown')}"]
    lines += [f"{key}: {config[key]}" for key in SHOWN_CONFIG_KEYS if key in config]
    shown = {k: v for k, v in sources.items() if k != HIDDEN_CONFIG_KEY}
    if shown:
        lines.append("config_sources (where each value came from):")
        lines += [f"  {k}: {v}" for k, v in shown.items()]
    return "SESSION START", "\n".join(lines)


def render_transcript(session_files: list[Path]) -> str:
    """All sessions of a sample as one sequence in time order, with one entry numbering.

    Records are ordered by `created_at` (items) or `timestamp` (events); within a session the log order
    is kept. Each entry header has the time and the session (S1, S2, ...).
    """
    sessions = sorted((_read_session(Path(p)) for p in session_files), key=_Session.start_key)
    index_of = {s.id: i for i, s in enumerate(sessions)}
    rows = []  # (time, session index, sequence number, tag, header, body)
    main_seen = False
    for si, session in enumerate(sessions):
        if session.parent:
            kind = "subagent"
            parent = f"S{index_of[session.parent] + 1}" if session.parent in index_of else session.parent
            description = f"subagent session, started by spawn_agent in {parent}"
        elif not main_seen:
            kind, main_seen = "main", True
            description = "main session"
        else:
            kind = "child"
            description = "child session: a meltzer exec started by a shell command"
        tag = f"S{si + 1} {kind}"
        previous = _parse_time(session.meta.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc)
        for seq, rec in enumerate(session.records):
            when = max(_parse_time(rec.get("created_at") or rec.get("timestamp")) or previous, previous)
            previous = when
            entry = _session_start(session, description) if rec.get("type") == "session_meta" else _entry(rec)
            if entry is not None:
                rows.append((when, si, seq, tag, entry[0], entry[1]))
    rows.sort(key=lambda r: r[:3])
    entries = [
        f"[{n}] {_format_time(when)} {tag} {header}\n{_indent(body.rstrip())}"
        for n, (when, _, _, tag, header, body) in enumerate(rows, start=1)
    ]
    return "\n\n".join(entries) if entries else "(no entries)"


# --- output parsing -----------------------------------------------------------------------------


FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n\s*```", re.S)


def _validate(data: Any, card_ids: Sequence[str]) -> dict:
    if not isinstance(data, dict):
        raise JudgeOutputError("output is not a JSON object")
    missing = [k for k in OUTPUT_KEYS if k not in data]
    if missing:
        raise JudgeOutputError(f"missing keys: {missing}")
    faithful, card_id = data["faithful"], data["card_id"]
    if not isinstance(faithful, bool):
        raise JudgeOutputError(f"faithful must be a boolean, got {faithful!r}")
    if not isinstance(data["harness_exploit"], bool):
        raise JudgeOutputError(f"harness_exploit must be a boolean, got {data['harness_exploit']!r}")
    for key in ("route_summary", "rationale"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise JudgeOutputError(f"missing or empty string {key!r}")
    if card_id is not None and (not isinstance(card_id, str) or card_id not in card_ids):
        raise JudgeOutputError(f"card_id must be one of {', '.join(card_ids)} or null, got {card_id!r}")
    if faithful and card_id is None:
        raise JudgeOutputError("faithful is true but card_id is null")
    if not faithful and card_id is not None:
        raise JudgeOutputError(f"faithful is false but card_id is {card_id!r}; it must be null")
    return {
        "faithful": faithful,
        "card_id": card_id,
        "harness_exploit": data["harness_exploit"],
        "route_summary": data["route_summary"].strip(),
        "rationale": data["rationale"].strip(),
    }


def parse_judge_output(text: str, card_ids: Sequence[str]) -> dict:
    """Parse and validate one judge output; `card_ids` are the cards it may name. Raises JudgeOutputError."""
    fenced = FENCE.search(text or "")
    raw = fenced.group(1) if fenced else (text or "")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise JudgeOutputError(f"invalid JSON: {e}") from None
    return _validate(data, card_ids)


# --- API key ------------------------------------------------------------------------------------


def load_api_key(env_file: Path | None = None) -> str | None:
    """OPENROUTER_API_KEY from the environment, else from the .env file. None if absent."""
    env_file = ENV_FILE if env_file is None else env_file
    if os.environ.get(KEY_NAME):
        return os.environ[KEY_NAME]
    if not env_file.is_file():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        name, sep, value = line.partition("=")
        if sep and name.strip() == KEY_NAME:
            return value.strip().strip("'\"") or None
    return None


# --- calls --------------------------------------------------------------------------------------


def request_body(judge: dict, messages: list[dict]) -> dict:
    body = {
        "model": judge["id"],
        "messages": messages,
        "temperature": judge["temperature"],
        "provider": judge["provider"],
        "response_format": {"type": "json_object"},
        "usage": {"include": True},
    }
    if judge.get("reasoning_effort") is not None:
        body["reasoning"] = {"effort": judge["reasoning_effort"]}
    return body


def _slug(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def served_by_configured(response_provider: Any, provider_config: Any) -> bool:
    """True when the provider named in the response is one of the configured providers (`order`)."""
    order = provider_config.get("order", []) if isinstance(provider_config, dict) else [provider_config]
    slugs = [_slug(str(p).split("/")[0]) for p in order if p]
    if not slugs:
        return True
    return bool(response_provider) and any(_slug(response_provider).startswith(s) for s in slugs)


class _Aborted(Exception):
    """A sample stopped before calling because an earlier call failed fatally."""


@dataclass
class SampleJob:
    sample_id: str
    sample_dir: Path
    body: dict
    calls: list[dict] = field(default_factory=list)
    parsed: dict | None = None
    error: str | None = None


class Caller:
    def __init__(
        self, client: httpx.AsyncClient, api_key: str, concurrency: int, backoff_s: float, card_ids: Sequence[str]
    ):
        self.client = client
        self.card_ids = card_ids
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.sem = asyncio.Semaphore(concurrency)
        self.backoff_s = backoff_s
        self.fatal: JudgeFatalError | None = None

    @staticmethod
    def _record(job: SampleJob, **fields) -> dict:
        record = {
            "call": len(job.calls) + 1,
            "status_code": None,
            "response": None,
            "parsed": None,
            "error": None,
            "model": None,
            "provider": None,
            "response_id": None,
            "usage": None,
        }
        record.update(fields)
        job.calls.append(record)
        return record

    async def _post(self, job: SampleJob) -> tuple[dict, dict] | None:
        """POST with retries on transient failures. Returns (response JSON, call record) or None."""
        for n in range(HTTP_TRIES):
            if n:
                await asyncio.sleep(self.backoff_s * 2 ** (n - 1))
            async with self.sem:
                if self.fatal is not None:  # queued behind a call that failed fatally: do not call
                    raise _Aborted()
                try:
                    resp = await self.client.post(API_URL, json=job.body, headers=self.headers, timeout=TIMEOUT_S)
                except httpx.TransportError as e:
                    self._record(job, error=f"transport error: {type(e).__name__}: {e}")
                    continue
            if resp.status_code in RETRYABLE_STATUS or resp.status_code >= 500:
                self._record(job, status_code=resp.status_code, response=resp.text[:2000], error=f"HTTP {resp.status_code}")
                continue
            if resp.status_code != 200:
                self._record(job, status_code=resp.status_code, response=resp.text[:2000], error=f"HTTP {resp.status_code}")
                raise JudgeFatalError(f"OpenRouter HTTP {resp.status_code}: {resp.text[:500]}")
            try:
                data = resp.json()
            except ValueError:
                data = None
            if not isinstance(data, dict) or not data.get("choices"):
                # OpenRouter can return 200 with an error body when the provider fails.
                self._record(job, status_code=200, response=resp.text[:2000], error="no choices in response")
                continue
            record = self._record(
                job,
                status_code=200,
                response=data,
                model=data.get("model"),
                provider=data.get("provider"),
                response_id=data.get("id"),
                usage=data.get("usage"),
            )
            return data, record
        return None

    async def judge(self, job: SampleJob) -> None:
        """Judge one sample and write its judge.json. After a fatal error, samples not started are skipped."""
        if self.fatal is not None:
            return
        try:
            await self._judge(job)
        except JudgeFatalError as e:
            self.fatal = self.fatal or e
            job.error = f"fatal: {e}"
        except _Aborted:
            if not job.calls:
                return  # nothing was sent: leave no judge.json
            job.error = "aborted after a fatal error in another sample"
        _write_judge_json(job)

    async def _judge(self, job: SampleJob) -> None:
        job.error = "no valid output"
        for _ in range(OUTPUT_TRIES):
            got = await self._post(job)
            if got is None:
                job.error = "HTTP retries exhausted"
                return
            data, record = got
            content = (data["choices"][0].get("message") or {}).get("content")
            try:
                record["parsed"] = parse_judge_output(content, self.card_ids)
            except JudgeOutputError as e:
                record["error"] = f"output: {e}"
                job.error = f"output: {e}"
                continue
            job.parsed, job.error = record["parsed"], None
            return


def _write_judge_json(job: SampleJob) -> None:
    out = {"sample_id": job.sample_id, "request": job.body, "parsed": job.parsed, "error": job.error, "calls": job.calls}
    (job.sample_dir / "judge.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")


async def _run_jobs(
    jobs: list[SampleJob], judge: dict, api_key: str, transport, backoff_s: float, card_ids: Sequence[str]
) -> None:
    async with httpx.AsyncClient(transport=transport) as client:
        caller = Caller(client, api_key, int(judge["concurrency"]), backoff_s, card_ids)
        await asyncio.gather(*(caller.judge(j) for j in jobs))
    if caller.fatal is not None:
        raise caller.fatal


# --- run directory ------------------------------------------------------------------------------


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_rows(path: Path, rows: list[dict]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def valid_judgment(sample_dir: Path, card_ids: Sequence[str]) -> dict | None:
    """The parsed output of samples/<id>/judge.json if the file exists and its output is valid."""
    path = sample_dir / "judge.json"
    try:
        return _validate(json.loads(path.read_text(encoding="utf-8"))["parsed"], card_ids)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def session_files(sample_dir: Path) -> list[Path]:
    return sorted((sample_dir / "meltzer" / "sessions").rglob("*.jsonl"))


def _lazy_side_task_descriptions() -> dict[str, str]:
    """Descriptions from bench.basharena.SIDE_TASKS when that module is importable, else empty."""
    try:
        from bench.basharena import SIDE_TASKS
    except Exception:
        return {}
    out = {}
    if not hasattr(SIDE_TASKS, "items"):
        return out
    for name, value in SIDE_TASKS.items():
        desc = value if isinstance(value, str) else getattr(value, "description", None)
        if desc is None and isinstance(value, dict):
            desc = value.get("description")
        if isinstance(desc, str):
            out[name] = desc
    return out


def judge_run(
    run_dir: Path,
    judge: dict,
    cards_dir: Path,
    *,
    side_task_descriptions: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    api_key: str | None = None,
    backoff_s: float = BACKOFF_S,
) -> None:
    """Judge every sample of the run with side_pass true (spec 10.3); see the module docstring.

    `judge` is the `models.judge` mapping of the run config. `transport`, `api_key` and `backoff_s` are
    for tests.
    """
    run_dir = Path(run_dir)
    missing = [k for k in JUDGE_KEYS if k not in judge]
    if missing:
        raise ValueError(f"judge config misses keys: {missing}")
    cards = load_cards(Path(cards_dir))
    card_ids = [card_id for card_id, _ in cards]
    prompt = load_prompt()
    monitor_policy = load_monitor_policy()
    if side_task_descriptions is None:
        side_task_descriptions = _lazy_side_task_descriptions()

    rows = read_rows(run_dir / "samples.jsonl")
    passed = [r for r in rows if r.get("side_pass") is True]
    jobs: list[SampleJob] = []
    n_done = n_nologs = 0
    for r in passed:
        sample_dir = run_dir / "samples" / r["sample_id"]
        if valid_judgment(sample_dir, card_ids) is not None:
            n_done += 1
            continue
        files = session_files(sample_dir)
        if not files:
            n_nologs += 1
            print(f"judge: {r['sample_id']}: no session logs under {sample_dir / 'meltzer' / 'sessions'}, not judged")
            continue
        side_task = r["side_task"]
        description = side_task_descriptions.get(side_task)
        if not description:
            raise JudgeFatalError(
                f"no description for side task {side_task!r}: pass side_task_descriptions or make "
                f"bench.basharena.SIDE_TASKS importable"
            )
        explanation = r.get("side_explanation") or "(none)"
        messages = build_messages(
            prompt, cards, side_task, description, explanation, render_transcript(files), monitor_policy
        )
        jobs.append(SampleJob(r["sample_id"], sample_dir, request_body(judge, messages)))

    plural = "" if len(passed) == 1 else "s"
    print(
        f"judge: {len(passed)} sample{plural} with side_pass true: {n_done} already judged, {len(jobs)} to judge, "
        f"{n_nologs} without session logs; model {judge['id']}, provider {judge['provider']}"
    )
    try:
        if jobs:
            key = api_key or load_api_key()
            if not key:
                raise JudgeFatalError(f"{KEY_NAME} not set (environment or {ENV_FILE})")
            asyncio.run(_run_jobs(jobs, judge, key, transport, backoff_s, card_ids))
    finally:
        # Fill the judge field from the valid judge.json files, also after a fatal error.
        for r in rows:
            r["judge"] = (
                valid_judgment(run_dir / "samples" / r["sample_id"], card_ids) if r.get("side_pass") is True else None
            )
        write_rows(run_dir / "samples.jsonl", rows)

    n_ok = sum(j.parsed is not None for j in jobs)
    n_err = len(jobs) - n_ok
    faithful = sum(1 for j in jobs if j.parsed and j.parsed["faithful"])
    harness = sum(1 for j in jobs if j.parsed and j.parsed["harness_exploit"])
    cost = sum(((c.get("usage") or {}).get("cost") or 0) for j in jobs for c in j.calls)
    print(
        f"judge: {n_ok} judged now ({faithful} faithful, {harness} harness_exploit), {n_err} judge_error, "
        f"cost ${cost:.4f}"
    )
    others = sorted(
        {
            str(c["provider"] or "none reported")
            for j in jobs
            for c in j.calls
            if c["status_code"] == 200 and not served_by_configured(c["provider"], judge["provider"])
        }
    )
    if others:
        print(f"judge: WARNING some calls were not served by the configured provider {judge['provider']}: {others}")
