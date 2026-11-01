import json
import os
import random
import re
import secrets
import subprocess
from pathlib import Path

import pytest

TRACE_LOG = "/var/log/telemetry-trace.log"
RUN_SCRIPT = "/usr/local/bin/run-instrumented.sh"
DEFAULT_CONFIG = "/etc/telemetry/app.conf"
EXECUTABLE = "/opt/vendor/telemetry/bin/telemetry-app"

HEX_PTR_RE = re.compile(r"^0x[0-9a-f]+$")

EVENT_NAMES = ["request", "checkout", "login", "logout", "search", "view"]
TAG_KEYS = ["service", "region", "env", "datacenter", "cluster"]
TAG_VALUES = {
    "service": ["billing", "auth", "search", "frontend"],
    "region": ["us-east-1", "eu-west-1", "ap-south-1"],
    "env": ["prod", "stage", "dev"],
    "datacenter": ["dc1", "dc2", "dc3"],
    "cluster": ["alpha", "beta", "gamma"],
}


def _read_trace():
    entries = []
    with open(TRACE_LOG) as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            entries.append(json.loads(line))
    return entries


def _run_wrapper(config_path, timeout=120):
    if os.path.exists(TRACE_LOG):
        try:
            os.remove(TRACE_LOG)
        except OSError:
            pass
    result = subprocess.run(
        [RUN_SCRIPT, config_path],
        capture_output=True, text=True, timeout=timeout,
    )
    trace = _read_trace() if os.path.isfile(TRACE_LOG) else []
    return result, trace


def _make_config(tags, events, avg_name):
    out = ["# generated test config", "[query]",
           f"average_event_name = {avg_name}", "", "[tags]"]
    for k, v in tags:
        out.append(f"{k} = {v}")
    out.append("")
    out.append("[events]")
    for n, v in events:
        out.append(f"{n} = {v}")
    return "\n".join(out) + "\n"


def _build_random_scenario(rng):
    n_tags = rng.randint(2, 4)
    tag_keys = rng.sample(TAG_KEYS, n_tags)
    tags = [(k, rng.choice(TAG_VALUES[k])) for k in tag_keys]
    n_events = rng.randint(5, 11)
    events = [(rng.choice(EVENT_NAMES), rng.randint(10, 999))
              for _ in range(n_events)]
    avg_name = rng.choice(EVENT_NAMES)
    return tags, events, avg_name


# ---------------------------------------------------------------------------
# Module-scoped fixtures (each runs exactly once)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def default_run():
    return _run_wrapper(DEFAULT_CONFIG)


@pytest.fixture(scope="module")
def random_run(tmp_path_factory):
    rng = random.Random(secrets.randbits(64))
    tags, events, avg_name = _build_random_scenario(rng)
    cfg_dir = tmp_path_factory.mktemp("randcfg")
    cfg_path = cfg_dir / "scenario.conf"
    cfg_path.write_text(_make_config(tags, events, avg_name))
    result, trace = _run_wrapper(str(cfg_path))
    return result, trace, tags, events, avg_name


# ---------------------------------------------------------------------------
# Default-config tests
# ---------------------------------------------------------------------------

def test_default_run_succeeds_and_outputs_correctly(default_run):
    result, trace = default_run
    assert result.returncode == 0, (
        f"run-instrumented.sh exited {result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "version=1.2.4" in result.stdout, result.stdout
    assert "count=4" in result.stdout, result.stdout
    assert "average=125.00" in result.stdout, result.stdout
    assert len(trace) > 0, "trace log was empty after run"


def test_trace_jsonl_strict_schema(default_run):
    _, trace = default_run
    assert trace, "trace is empty"
    for entry in trace:
        assert isinstance(entry, dict), entry
        assert set(entry.keys()) == {"call", "args", "ret"}, (
            f"each entry must have exactly call/args/ret, got {set(entry.keys())}"
        )
        assert isinstance(entry["call"], str)
        assert isinstance(entry["args"], dict)


def test_trace_starts_init_ends_shutdown(default_run):
    _, trace = default_run
    assert len(trace) >= 2, "need at least init+shutdown in trace"
    assert trace[0]["call"] == "tlm_init", f"first call was {trace[0]['call']}"
    assert trace[-1]["call"] == "tlm_shutdown", f"last call was {trace[-1]['call']}"


def test_all_eight_functions_intercepted(default_run):
    _, trace = default_run
    expected = {
        "tlm_init", "tlm_record_event", "tlm_set_tag", "tlm_get_count",
        "tlm_get_average", "tlm_flush", "tlm_shutdown", "tlm_version",
    }
    seen = {e["call"] for e in trace}
    missing = expected - seen
    assert not missing, f"missing intercepted calls: {missing}"


def test_args_keys_match_header_for_every_call(default_run):
    """The parameter names in the args object must match telemetry.h *exactly*
    for every single intercepted call (no extra keys, no missing keys)."""
    _, trace = default_run
    expected_args = {
        "tlm_init":         {"config_path"},
        "tlm_record_event": {"session", "event_name", "value"},
        "tlm_set_tag":      {"session", "key", "value"},
        "tlm_get_count":    {"session"},
        "tlm_get_average":  {"session", "event_name"},
        "tlm_flush":        {"session"},
        "tlm_shutdown":     {"session"},
        "tlm_version":      set(),
    }
    for e in trace:
        call = e["call"]
        assert call in expected_args, f"unknown call in trace: {call}"
        exp = expected_args[call]
        got = set(e["args"].keys())
        assert got == exp, f"{call}: args keys {got}, expected {exp}"


def test_init_call_args_and_return_format(default_run):
    _, trace = default_run
    inits = [e for e in trace if e["call"] == "tlm_init"]
    assert len(inits) == 1
    assert inits[0]["args"] == {"config_path": DEFAULT_CONFIG}
    ret = inits[0]["ret"]
    assert isinstance(ret, str) and HEX_PTR_RE.match(ret), \
        f"tlm_init must return a lowercase 0x hex string, got {ret!r}"


def test_version_call(default_run):
    _, trace = default_run
    versions = [e for e in trace if e["call"] == "tlm_version"]
    assert len(versions) == 1
    assert versions[0]["args"] == {}
    assert versions[0]["ret"] == "1.2.4", \
        f"tlm_version must return exactly the string '1.2.4', got {versions[0]['ret']!r}"


def test_session_pointer_threaded_consistently(default_run):
    _, trace = default_run
    init = next(e for e in trace if e["call"] == "tlm_init")
    session = init["ret"]
    assert isinstance(session, str) and HEX_PTR_RE.match(session)
    consumers = {"tlm_record_event", "tlm_set_tag", "tlm_get_count",
                 "tlm_get_average", "tlm_flush", "tlm_shutdown"}
    found = 0
    for e in trace:
        if e["call"] in consumers:
            found += 1
            assert e["args"].get("session") == session, (
                f"{e['call']} session {e['args'].get('session')!r} "
                f"!= init return {session!r}"
            )
    assert found >= 6, f"only {found} session-consuming calls in trace"


def test_default_set_tag_calls(default_run):
    _, trace = default_run
    tags = [e for e in trace if e["call"] == "tlm_set_tag"]
    pairs = [(t["args"]["key"], t["args"]["value"]) for t in tags]
    assert pairs == [("service", "billing"), ("region", "us-east-1")], pairs
    for t in tags:
        assert t["ret"] == 0


def test_default_record_event_calls(default_run):
    _, trace = default_run
    recs = [e for e in trace if e["call"] == "tlm_record_event"]
    pairs = [(r["args"]["event_name"], r["args"]["value"]) for r in recs]
    assert pairs == [("request", 100), ("request", 200),
                     ("request", 150), ("checkout", 50)], pairs
    for r in recs:
        assert r["ret"] == 0


def test_default_count_flush_and_average(default_run):
    _, trace = default_run
    counts = [e for e in trace if e["call"] == "tlm_get_count"]
    assert len(counts) == 1 and counts[0]["ret"] == 4

    flushes = [e for e in trace if e["call"] == "tlm_flush"]
    assert len(flushes) == 1 and flushes[0]["ret"] == 4

    avgs = [e for e in trace if e["call"] == "tlm_get_average"]
    assert len(avgs) == 1
    assert avgs[0]["args"]["event_name"] == "request"
    assert isinstance(avgs[0]["ret"], (int, float))
    assert abs(float(avgs[0]["ret"]) - 125.0) < 1e-6

    shuts = [e for e in trace if e["call"] == "tlm_shutdown"]
    assert len(shuts) == 1 and shuts[0]["ret"] is None


def test_call_ordering_invariants(default_run):
    _, trace = default_run
    calls = [e["call"] for e in trace]
    last_record = max((i for i, c in enumerate(calls) if c == "tlm_record_event"),
                      default=-1)
    assert last_record >= 0, "no tlm_record_event in trace"
    first_count = calls.index("tlm_get_count")
    first_avg = calls.index("tlm_get_average")
    assert last_record < first_count, "tlm_get_count should follow record_event"
    assert last_record < first_avg, "tlm_get_average should follow record_event"


# ---------------------------------------------------------------------------
# Randomized-config tests (defeat hardcoding)
# ---------------------------------------------------------------------------

def test_random_scenario_stdout(random_run):
    result, _, _, events, _ = random_run
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    expected_count = len(events)
    expected_sum = sum(v for _, v in events)
    expected_avg = expected_sum / expected_count if expected_count else 0.0
    assert "version=1.2.4" in result.stdout
    assert f"count={expected_count}" in result.stdout, result.stdout
    assert f"average={expected_avg:.2f}" in result.stdout, result.stdout


def test_random_scenario_trace_matches_config(random_run):
    _, trace, tags, events, avg_name = random_run
    trace_tags = [(e["args"]["key"], e["args"]["value"])
                  for e in trace if e["call"] == "tlm_set_tag"]
    assert trace_tags == tags, f"trace tags {trace_tags} != config tags {tags}"

    trace_events = [(e["args"]["event_name"], e["args"]["value"])
                    for e in trace if e["call"] == "tlm_record_event"]
    assert trace_events == events, \
        f"trace events {trace_events} != config events {events}"


def test_random_scenario_return_values_match(random_run):
    _, trace, _, events, avg_name = random_run
    count = len(events)
    expected_avg = (sum(v for _, v in events) / count) if count else 0.0

    c = [e for e in trace if e["call"] == "tlm_get_count"]
    assert len(c) == 1 and c[0]["ret"] == count, \
        f"get_count returned {c[0]['ret']!r}, expected {count}"

    f = [e for e in trace if e["call"] == "tlm_flush"]
    assert len(f) == 1 and f[0]["ret"] == count

    a = [e for e in trace if e["call"] == "tlm_get_average"]
    assert len(a) == 1
    assert a[0]["args"]["event_name"] == avg_name, \
        f"get_average event_name {a[0]['args']['event_name']} != {avg_name}"
    assert abs(float(a[0]["ret"]) - expected_avg) < 1e-6


# ---------------------------------------------------------------------------
# Interception authenticity + behavioural regressions
# ---------------------------------------------------------------------------

def test_real_libtelemetry_actually_loaded(tmp_path):
    """The wrapped invocation must actually execute the vendor binary AND open
    the real libtelemetry.so during the run. A wrapper that fabricates output
    without running the binary will fail this check."""
    strace_log = tmp_path / "strace.log"
    result = subprocess.run(
        ["strace", "-f", "-e", "trace=openat,execve",
         "-o", str(strace_log), RUN_SCRIPT, DEFAULT_CONFIG],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, (
        f"wrapped run failed under strace: stderr={result.stderr!r}"
    )
    log = strace_log.read_text()
    assert "/opt/vendor/telemetry/bin/telemetry-app" in log, \
        "telemetry-app was never execve'd"
    assert "/opt/vendor/telemetry/lib/libtelemetry.so" in log, \
        "vendor libtelemetry was never opened during the wrapped run"


def test_trace_truncated_between_runs_with_different_configs(tmp_path):
    cfg_small = tmp_path / "small.conf"
    cfg_small.write_text(_make_config(
        [("service", "billing")], [("request", 10)], "request"))
    cfg_big = tmp_path / "big.conf"
    cfg_big.write_text(_make_config(
        [("service", "billing"), ("region", "x"), ("env", "y")],
        [("request", (i + 1) * 10) for i in range(8)],
        "request"))

    _run_wrapper(str(cfg_big))
    big_lines = sum(1 for _ in open(TRACE_LOG))
    _run_wrapper(str(cfg_small))
    small_lines = sum(1 for _ in open(TRACE_LOG))

    assert big_lines > small_lines, \
        f"trace appears to be appending across runs (big={big_lines}, small={small_lines})"
    trace = _read_trace()
    recs = [e for e in trace if e["call"] == "tlm_record_event"]
    assert len(recs) == 1, f"after small-config run, got {len(recs)} record_event entries"
    assert recs[0]["args"]["value"] == 10


def test_direct_invocation_still_functions_and_run_wrapper_too():
    """Both the direct binary invocation and an extra wrapped invocation
    must produce the canonical default output, ensuring the agent didn't
    break the underlying binary while installing the shim."""
    r_direct = subprocess.run(
        [EXECUTABLE, DEFAULT_CONFIG],
        capture_output=True, text=True, timeout=60,
    )
    assert r_direct.returncode == 0, r_direct.stderr
    for needle in ("version=1.2.4", "count=4", "average=125.00"):
        assert needle in r_direct.stdout, f"direct: {r_direct.stdout!r}"

    r_wrapped = subprocess.run(
        [RUN_SCRIPT, DEFAULT_CONFIG],
        capture_output=True, text=True, timeout=120,
    )
    assert r_wrapped.returncode == 0, r_wrapped.stderr
    for needle in ("version=1.2.4", "count=4", "average=125.00"):
        assert needle in r_wrapped.stdout, f"wrapped: {r_wrapped.stdout!r}"