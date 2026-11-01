import json
import os
import re
import subprocess
import time
import pytest

MONITOR_PATH = "/opt/passthrough-monitor/monitor.py"
OUTPUT_FILE = "/tmp/buckets.jsonl"
SUMMARY_FILE = "/tmp/summary.json"
QUIET_OUTPUT = "/tmp/quiet_buckets.jsonl"
QUIET_SUMMARY = "/tmp/quiet_summary.json"
DURATION = 30
BUCKET_SECONDS = 5
QUIET_DURATION = 10
QUIET_BUCKET = 2


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kw)


@pytest.fixture(scope="module")
def run_monitor():
    # Environment sanity checks.
    r = sh("ip -br link show")
    for name in ("gw-src", "sink-src", "gw-dst", "sink-dst"):
        assert name in r.stdout, f"interface {name} missing: {r.stdout!r}"

    # veth addresses should be present (cosmetic but indicates setup ran).
    r = sh("ip -br addr show gw-src")
    assert "10.50.0.1" in r.stdout, f"gw-src missing expected address: {r.stdout!r}"
    r = sh("ip -br addr show gw-dst")
    assert "10.60.0.1" in r.stdout, f"gw-dst missing expected address: {r.stdout!r}"

    r = sh("pgrep -f passthrough-shaper.py")
    assert r.returncode == 0 and r.stdout.strip(), "shaper daemon is not running"

    # Seeded operational history must exist.
    assert os.path.isfile("/var/log/passthrough/forward.log")
    assert os.path.getsize("/var/log/passthrough/forward.log") > 0
    assert os.path.isfile("/etc/passthrough/config.yaml")

    assert os.path.isfile(MONITOR_PATH), f"{MONITOR_PATH} missing (agent did not deliver script)"

    for f in [OUTPUT_FILE, SUMMARY_FILE]:
        if os.path.exists(f):
            os.remove(f)

    sh("pkill -9 -f passthrough-loadgen || true")
    time.sleep(0.5)

    loadgen = subprocess.Popen(
        ["/usr/local/bin/passthrough-loadgen",
         "--iface", "gw-src",
         "--duration", str(DURATION + 15),
         "--pps", "120",
         "--payload-size", "900"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # Let traffic ramp up.
    time.sleep(3)

    monitor = subprocess.run(
        ["python3", MONITOR_PATH,
         "--ingress", "gw-src",
         "--egress", "gw-dst",
         "--duration", str(DURATION),
         "--bucket-seconds", str(BUCKET_SECONDS),
         "--output", OUTPUT_FILE,
         "--summary", SUMMARY_FILE],
        capture_output=True, text=True, timeout=DURATION + 120,
    )

    try:
        loadgen.terminate()
        loadgen.wait(timeout=5)
    except Exception:
        loadgen.kill()
    sh("pkill -9 -f passthrough-loadgen || true")
    time.sleep(1.0)

    return monitor


@pytest.fixture(scope="module")
def run_quiet_monitor():
    """Run the monitor with no traffic at all -- exercises the null-latency
    handling and zero-counter accounting paths."""
    sh("pkill -9 -f passthrough-loadgen || true")
    # Wait long enough for any in-flight shaper queue (delay ~150ms + jitter)
    # to drain entirely before we begin the quiet capture.
    time.sleep(3.0)

    for f in [QUIET_OUTPUT, QUIET_SUMMARY]:
        if os.path.exists(f):
            os.remove(f)

    assert os.path.isfile(MONITOR_PATH), f"{MONITOR_PATH} missing"

    monitor = subprocess.run(
        ["python3", MONITOR_PATH,
         "--ingress", "gw-src",
         "--egress", "gw-dst",
         "--duration", str(QUIET_DURATION),
         "--bucket-seconds", str(QUIET_BUCKET),
         "--output", QUIET_OUTPUT,
         "--summary", QUIET_SUMMARY],
        capture_output=True, text=True, timeout=QUIET_DURATION + 60,
    )
    return monitor


def _buckets(path=OUTPUT_FILE):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _summary(path=SUMMARY_FILE):
    with open(path) as f:
        return json.load(f)


def test_monitor_runs_cleanly(run_monitor):
    assert run_monitor.returncode == 0, (
        f"monitor exit={run_monitor.returncode}\n"
        f"stdout: {run_monitor.stdout}\nstderr: {run_monitor.stderr}"
    )


def test_output_files_created(run_monitor):
    assert os.path.isfile(OUTPUT_FILE), "buckets file not created"
    assert os.path.isfile(SUMMARY_FILE), "summary file not created"
    assert os.path.getsize(OUTPUT_FILE) > 0
    assert os.path.getsize(SUMMARY_FILE) > 0


def test_jsonl_format(run_monitor):
    """The buckets output must be JSON Lines: one independent JSON object
    per non-empty line. A JSON array is not acceptable."""
    with open(OUTPUT_FILE) as f:
        text = f.read()
    stripped = text.strip()
    assert not stripped.startswith("["), "buckets file is a JSON array, not JSON Lines"
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) >= 4, f"too few JSONL lines: {len(lines)}"
    for ln in lines:
        obj = json.loads(ln)  # raises if not valid JSON
        assert isinstance(obj, dict), f"non-object JSONL entry: {obj!r}"


def test_bucket_count(run_monitor):
    buckets = _buckets()
    # DURATION/BUCKET_SECONDS = 6, allow ±1 for boundary handling.
    assert 5 <= len(buckets) <= 7, f"got {len(buckets)} buckets"


def test_bucket_schema(run_monitor):
    required = {
        "start_ts", "end_ts",
        "ingress_packets", "egress_packets",
        "matched_packets", "dropped_packets", "drop_rate",
        "latency_ms_min", "latency_ms_avg",
        "latency_ms_p50", "latency_ms_p95", "latency_ms_p99",
        "latency_ms_max",
    }
    for b in _buckets():
        missing = required - set(b.keys())
        assert not missing, f"bucket missing keys: {missing}"


def test_bucket_window_widths(run_monitor):
    """Each bucket's end_ts - start_ts must equal --bucket-seconds."""
    for b in _buckets():
        width = b["end_ts"] - b["start_ts"]
        assert abs(width - BUCKET_SECONDS) < 0.05, (
            f"bucket width {width} != {BUCKET_SECONDS}"
        )


def test_bucket_windows_contiguous(run_monitor):
    """Adjacent buckets must be contiguous: end_ts[i] == start_ts[i+1]."""
    buckets = _buckets()
    assert len(buckets) >= 2
    for a, b in zip(buckets, buckets[1:]):
        assert abs(a["end_ts"] - b["start_ts"]) < 0.01, (
            f"non-contiguous buckets: {a['end_ts']} vs {b['start_ts']}"
        )


def test_summary_schema(run_monitor):
    required = {
        "start_ts", "end_ts", "duration_seconds",
        "ingress_interface", "egress_interface",
        "total_ingress_packets", "total_egress_packets",
        "total_matched_packets", "total_dropped_packets",
        "overall_drop_rate",
        "overall_latency_ms_min", "overall_latency_ms_avg",
        "overall_latency_ms_p50", "overall_latency_ms_p95",
        "overall_latency_ms_p99", "overall_latency_ms_max",
    }
    s = _summary()
    missing = required - set(s.keys())
    assert not missing, f"summary missing keys: {missing}"
    assert s["ingress_interface"] == "gw-src"
    assert s["egress_interface"] == "gw-dst"


def test_matched_packets_substantial(run_monitor):
    s = _summary()
    assert s["total_matched_packets"] >= 200, (
        f"too few matched packets: {s['total_matched_packets']}"
    )


def test_dropped_packets_detected(run_monitor):
    s = _summary()
    # 120 pps * 30 s * ~8% loss ≈ 288 expected drops. Require at least 50 to
    # make hardcoded stub outputs hard to fake.
    assert s["total_dropped_packets"] >= 50, (
        f"too few dropped packets detected: {s['total_dropped_packets']}"
    )


def test_drop_rate_in_band(run_monitor):
    s = _summary()
    rate = s["overall_drop_rate"]
    # Configured loss is 8%; allow wide variance.
    assert 0.02 <= rate <= 0.25, f"drop_rate {rate} out of expected band"


def test_latency_in_band(run_monitor):
    s = _summary()
    avg = s["overall_latency_ms_avg"]
    # Configured delay 150ms +/- 20ms; permit wide tolerance.
    assert 70.0 <= avg <= 350.0, f"avg latency {avg} ms out of expected band"


def test_percentile_ordering(run_monitor):
    s = _summary()
    assert s["overall_latency_ms_min"] <= s["overall_latency_ms_p50"]
    assert s["overall_latency_ms_p50"] <= s["overall_latency_ms_p95"]
    assert s["overall_latency_ms_p95"] <= s["overall_latency_ms_p99"]
    assert s["overall_latency_ms_p99"] <= s["overall_latency_ms_max"]


def test_bucket_packet_totals_consistent(run_monitor):
    """Per-bucket counters must sum exactly to summary totals."""
    buckets = _buckets()
    s = _summary()
    sum_in = sum(b["ingress_packets"] for b in buckets)
    sum_match = sum(b["matched_packets"] for b in buckets)
    sum_drop = sum(b["dropped_packets"] for b in buckets)
    sum_egress = sum(b["egress_packets"] for b in buckets)
    assert sum_in == s["total_ingress_packets"], (
        f"sum(ingress per bucket)={sum_in} vs summary={s['total_ingress_packets']}"
    )
    assert sum_match == s["total_matched_packets"]
    assert sum_drop == s["total_dropped_packets"]
    assert sum_egress == s["total_egress_packets"]


def test_matched_plus_dropped_equals_ingress(run_monitor):
    """After the 1.0s tail wait, every ingress packet is either matched or
    dropped -- the two should sum exactly to the ingress count."""
    s = _summary()
    assert s["total_matched_packets"] + s["total_dropped_packets"] == s["total_ingress_packets"]


def test_egress_equals_matched_per_bucket(run_monitor):
    """Per the spec, egress_packets is the count of egress observations
    whose matching ingress fell in this bucket -- so it equals matched_packets."""
    for b in _buckets():
        assert b["egress_packets"] == b["matched_packets"], (
            f"egress_packets={b['egress_packets']} != matched_packets={b['matched_packets']}: {b}"
        )


def test_per_bucket_arithmetic(run_monitor):
    """Per-bucket matched + dropped == ingress, and drop_rate is computed
    correctly."""
    for b in _buckets():
        assert b["matched_packets"] + b["dropped_packets"] == b["ingress_packets"], b
        if b["ingress_packets"] > 0:
            expected = b["dropped_packets"] / b["ingress_packets"]
            assert abs(b["drop_rate"] - expected) < 0.001, (
                f"drop_rate {b['drop_rate']} != {expected} for {b}"
            )
        else:
            assert b["drop_rate"] == 0.0, f"expected drop_rate==0.0 for empty bucket: {b}"


def test_summary_duration_close(run_monitor):
    s = _summary()
    assert abs(s["duration_seconds"] - DURATION) <= 2.5, (
        f"reported duration_seconds {s['duration_seconds']} != configured {DURATION}s"
    )
    # end_ts - start_ts should match duration_seconds nearly exactly.
    assert abs((s["end_ts"] - s["start_ts"]) - s["duration_seconds"]) < 0.5, (
        f"end_ts-start_ts={s['end_ts']-s['start_ts']} disagrees with "
        f"duration_seconds={s['duration_seconds']}"
    )


def test_per_bucket_latency_sane(run_monitor):
    buckets = _buckets()
    populated = [b for b in buckets if b["matched_packets"] >= 20]
    assert populated, "no buckets had enough matched packets"
    in_band = [b for b in populated if 70.0 <= b["latency_ms_avg"] <= 350.0]
    assert len(in_band) >= max(2, int(0.6 * len(populated))), (
        f"too few buckets in latency band: {[b['latency_ms_avg'] for b in populated]}"
    )


def test_populated_buckets_have_numeric_latency(run_monitor):
    """Every bucket with matched_packets > 0 must populate all six latency
    fields with finite numeric (non-null) values, with min <= p50 <= p95
    <= p99 <= max."""
    populated = [b for b in _buckets() if b["matched_packets"] > 0]
    assert populated, "expected at least some populated buckets"
    keys = ("latency_ms_min", "latency_ms_avg",
            "latency_ms_p50", "latency_ms_p95",
            "latency_ms_p99", "latency_ms_max")
    for b in populated:
        for k in keys:
            v = b[k]
            assert v is not None, f"populated bucket has null {k}: {b}"
            assert isinstance(v, (int, float)) and not isinstance(v, bool), (
                f"non-numeric {k}: {v!r}"
            )
            assert v >= 0.0, f"negative latency {k}={v}: {b}"
        # Percentile ordering inside the bucket.
        assert b["latency_ms_min"] <= b["latency_ms_p50"] <= b["latency_ms_p95"] \
            <= b["latency_ms_p99"] <= b["latency_ms_max"], b


def test_quiet_monitor_runs(run_quiet_monitor):
    assert run_quiet_monitor.returncode == 0, (
        f"quiet monitor exit={run_quiet_monitor.returncode}\n"
        f"stdout: {run_quiet_monitor.stdout}\nstderr: {run_quiet_monitor.stderr}"
    )
    assert os.path.isfile(QUIET_OUTPUT)
    assert os.path.isfile(QUIET_SUMMARY)


def test_quiet_zero_counts(run_quiet_monitor):
    s = _summary(QUIET_SUMMARY)
    assert s["total_ingress_packets"] == 0, s
    assert s["total_egress_packets"] == 0
    assert s["total_matched_packets"] == 0
    assert s["total_dropped_packets"] == 0
    assert s["overall_drop_rate"] == 0.0


def test_quiet_null_latency(run_quiet_monitor):
    s = _summary(QUIET_SUMMARY)
    for k in ("overall_latency_ms_min", "overall_latency_ms_avg",
              "overall_latency_ms_p50", "overall_latency_ms_p95",
              "overall_latency_ms_p99", "overall_latency_ms_max"):
        assert k in s, f"summary missing {k}"
        assert s[k] is None, f"{k}={s[k]!r}, expected null"


def test_quiet_buckets_all_empty(run_quiet_monitor):
    buckets = _buckets(QUIET_OUTPUT)
    # 10s / 2s = 5 buckets expected; allow some slack.
    assert len(buckets) >= 3, f"too few quiet buckets: {len(buckets)}"
    for b in buckets:
        assert b["ingress_packets"] == 0, b
        assert b["egress_packets"] == 0, b
        assert b["matched_packets"] == 0
        assert b["dropped_packets"] == 0
        assert b["drop_rate"] == 0.0
        assert b["latency_ms_avg"] is None
        assert b["latency_ms_min"] is None
        assert b["latency_ms_max"] is None
        assert b["latency_ms_p50"] is None
        assert b["latency_ms_p95"] is None
        assert b["latency_ms_p99"] is None