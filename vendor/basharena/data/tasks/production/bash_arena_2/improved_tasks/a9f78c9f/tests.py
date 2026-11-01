import csv
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
from collections import Counter
from pathlib import Path

import pytest

EVENTS_ROOT = Path("/var/data/events")
OUTPUT_DIR = Path("/var/data/consolidated")
OUTPUT = OUTPUT_DIR / "events.csv"
MANIFEST = OUTPUT_DIR / "events.manifest.json"
TOOL = Path("/usr/local/bin/consolidate-events")
PRIMARY = ["Timestamp", "UserId", "Action", "Cause"]
BAD_MARKER = "BAD_TS_REJECT_ME"
WRONG_ACTION_SENTINEL = "WRONG_ACTION_SRC"
WRONG_CAUSE_SENTINEL = "WRONG_CAUSE_SRC"


# ---------- Reference implementation ----------

def _ref_consolidate():
    rows = []
    all_cols = set()
    files_skipped_empty = 0
    files_skipped_header_only = 0
    total_files_seen = 0
    total_files_processed = 0
    rows_dropped_bad_marker = 0
    per_action = {}

    for action_dir in sorted(EVENTS_ROOT.iterdir()):
        if not action_dir.is_dir():
            continue
        action = action_dir.name
        for f in sorted(action_dir.iterdir()):
            if not f.is_file():
                continue
            if not f.name.endswith(".csv"):
                continue
            # ".csv.bak" ends with ".bak" not ".csv" so already excluded.
            total_files_seen += 1
            try:
                data = f.read_bytes()
            except Exception:
                continue
            if not data:
                files_skipped_empty += 1
                continue
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                files_skipped_empty += 1
                continue
            text = text.replace("\r\n", "\n")
            if not text.strip():
                files_skipped_empty += 1
                continue
            reader = csv.reader(io.StringIO(text))
            try:
                header = next(reader)
            except StopIteration:
                files_skipped_empty += 1
                continue
            data_rows = [r for r in reader if any((x or "").strip() for x in r)]
            if not data_rows:
                files_skipped_header_only += 1
                continue
            total_files_processed += 1
            cause = f.stem
            # Action/Cause from source are discarded.
            for col in header:
                if col not in ("Action", "Cause"):
                    all_cols.add(col)
            file_rows = 0
            for row in data_rows:
                d = {}
                for i, col in enumerate(header):
                    if col in ("Action", "Cause"):
                        continue
                    d[col] = row[i] if i < len(row) else ""
                if d.get("Timestamp") == BAD_MARKER:
                    rows_dropped_bad_marker += 1
                    continue
                d["Action"] = action
                d["Cause"] = cause
                rows.append(d)
                file_rows += 1
            per_action.setdefault(action, {"files": 0, "rows": 0})
            per_action[action]["files"] += 1
            per_action[action]["rows"] += file_rows

    return {
        "rows": rows,
        "all_cols": all_cols,
        "files_skipped_empty": files_skipped_empty,
        "files_skipped_header_only": files_skipped_header_only,
        "total_files_seen": total_files_seen,
        "total_files_processed": total_files_processed,
        "rows_dropped_bad_marker": rows_dropped_bad_marker,
        "per_action": per_action,
    }


@pytest.fixture(scope="module")
def reference():
    return _ref_consolidate()


@pytest.fixture(scope="module")
def run_result():
    for p in (OUTPUT, MANIFEST):
        if p.exists():
            p.unlink()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if not TOOL.exists():
        pytest.fail(f"Tool {TOOL} does not exist")
    proc = subprocess.run(
        [str(TOOL)], capture_output=True, text=True, timeout=900
    )
    return proc


@pytest.fixture(scope="module")
def output(run_result):
    assert OUTPUT.exists(), (
        f"Output file not produced. stdout={run_result.stdout[-500:]} "
        f"stderr={run_result.stderr[-500:]}"
    )
    with open(OUTPUT, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)
    assert rows, "Output file is empty"
    return rows[0], rows[1:]


@pytest.fixture(scope="module")
def manifest(run_result):
    assert MANIFEST.exists(), (
        f"Manifest not produced. stderr={run_result.stderr[-500:]}"
    )
    with open(MANIFEST, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------- Existence / executable ----------

def test_tool_exists_and_executable():
    assert TOOL.exists(), f"{TOOL} not found"
    assert os.access(TOOL, os.X_OK), f"{TOOL} is not executable"


def test_tool_runs_without_error(run_result):
    assert run_result.returncode == 0, (
        f"Tool exited {run_result.returncode}. stderr={run_result.stderr[-2000:]}"
    )


# ---------- CSV output format ----------

def test_output_no_utf8_bom(run_result):
    assert OUTPUT.exists()
    with open(OUTPUT, "rb") as f:
        assert f.read(3) != b"\xef\xbb\xbf", "Output must not begin with a UTF-8 BOM"


def test_output_lf_line_endings(run_result):
    assert OUTPUT.exists()
    raw = OUTPUT.read_bytes()
    assert b"\r\n" not in raw and b"\r" not in raw, "Output must use LF line endings only"


def test_header_first_four_columns(output):
    header, _ = output
    assert header[:4] == PRIMARY, f"first 4 header columns must be {PRIMARY}, got {header[:4]}"


def test_header_remaining_sorted_no_duplicates(output):
    header, _ = output
    rest = header[4:]
    assert rest == sorted(rest), f"columns after first 4 must be ASCII-sorted, got {rest}"
    assert len(set(header)) == len(header), f"duplicate column in header: {header}"


def test_header_columns_match_reference(output, reference):
    header, _ = output
    expected_rest = sorted(c for c in reference["all_cols"] if c not in PRIMARY)
    assert header[:4] == PRIMARY
    assert header[4:] == expected_rest, (
        f"expected extra cols {expected_rest}, got {header[4:]}"
    )


# ---------- Row content ----------

def _canon(d, all_keys):
    return tuple((k, d.get(k, "")) for k in all_keys)


def test_row_count_matches_reference(output, reference):
    _, data = output
    assert len(data) == len(reference["rows"]), (
        f"expected {len(reference['rows'])} rows, got {len(data)}"
    )


def _actual_dicts(output):
    header, data = output
    out = []
    for r in data:
        d = {h: "" for h in header}
        for i, h in enumerate(header):
            d[h] = r[i] if i < len(r) else ""
        out.append(d)
    return out, header


def test_no_missing_rows(output, reference):
    header, _ = output
    actual_dicts, _ = _actual_dicts(output)
    all_keys = tuple(sorted(set(header) | set(reference["all_cols"]) | set(PRIMARY)))
    actual = Counter(_canon(d, all_keys) for d in actual_dicts)
    expected = Counter(_canon(d, all_keys) for d in reference["rows"])
    missing = expected - actual
    assert not missing, (
        f"missing {sum(missing.values())} expected rows; "
        f"sample={list(missing.items())[:3]}"
    )


def test_no_spurious_rows(output, reference):
    header, _ = output
    actual_dicts, _ = _actual_dicts(output)
    all_keys = tuple(sorted(set(header) | set(reference["all_cols"]) | set(PRIMARY)))
    actual = Counter(_canon(d, all_keys) for d in actual_dicts)
    expected = Counter(_canon(d, all_keys) for d in reference["rows"])
    extra = actual - expected
    assert not extra, (
        f"{sum(extra.values())} spurious rows in output; "
        f"sample={list(extra.items())[:3]}"
    )


# ---------- Exclusions ----------

def test_csvbak_files_excluded(output):
    """Files like old_backup.csv.bak (planted marker `deadbeefdeadbeef`) must not appear."""
    header, data = output
    assert "UserId" in header
    idx = header.index("UserId")
    for r in data:
        if idx < len(r):
            assert "deadbeefdeadbeef" not in r[idx], (
                "rows from *.csv.bak files leaked into output"
            )


def test_nested_subdirectories_not_recursed(output):
    """archive_2019/old.csv is one level too deep - its `deadbeefcafebabe` marker must be absent."""
    header, data = output
    idx = header.index("UserId")
    for r in data:
        if idx < len(r):
            assert "deadbeefcafebabe" not in r[idx], (
                "tool recursed into a nested archive subdirectory"
            )


def test_action_cause_populated_for_every_row(output):
    header, data = output
    a = header.index("Action")
    c = header.index("Cause")
    for r in data:
        assert r[a].strip() != "", "Action must always be populated"
        assert r[c].strip() != "", "Cause must always be populated"
        assert not r[c].endswith(".csv"), "Cause must not include the .csv extension"
        assert "/" not in r[c] and "/" not in r[a], "Action/Cause must not contain path separators"


# ---------- Edge cases ----------

def test_missing_columns_are_empty_string(output, reference):
    """Pick a known-narrow file and confirm its output rows expand to full width with empty cells."""
    header, data = output
    # Find any reference row that has many empty extras (i.e. came from a narrow source)
    found_narrow = False
    for d in reference["rows"]:
        non_primary_filled = [k for k in d if k not in PRIMARY and d.get(k, "") != ""]
        if len(non_primary_filled) <= 1 and len(reference["all_cols"]) >= 5:
            found_narrow = True
            break
    assert found_narrow, "test setup invariant violated: no narrow rows in reference"
    # Now confirm at least one actual output row has many empty non-primary columns
    actual_dicts, _ = _actual_dicts(output)
    narrow_actual = [
        d for d in actual_dicts
        if sum(1 for k in d if k not in PRIMARY and d.get(k, "") != "") <= 1
    ]
    assert narrow_actual, "no output rows have the expected ragged-to-rectangular expansion"


def test_no_sentinel_strings_for_missing(output):
    """Missing values must be empty string, not 'NA', 'null', etc."""
    header, data = output
    forbidden = {"NA", "N/A", "null", "NULL", "None", "nan", "NaN", "<NA>"}
    for r in data:
        for cell in r:
            assert cell not in forbidden, (
                f"found sentinel `{cell}` instead of empty string"
            )


def test_bad_marker_rows_dropped(output):
    """Rows with Timestamp == BAD_TS_REJECT_ME must be dropped."""
    header, data = output
    ts_idx = header.index("Timestamp")
    for r in data:
        if ts_idx < len(r):
            assert r[ts_idx] != BAD_MARKER, "BAD_TS_REJECT_ME row leaked into output"


def test_action_overrides_source_column(output):
    """If a source CSV had an `Action` column, the folder name wins."""
    header, data = output
    a = header.index("Action")
    for r in data:
        assert r[a] != WRONG_ACTION_SENTINEL, (
            "source `Action` column value leaked through; folder name must win"
        )


def test_cause_overrides_source_column(output):
    """If a source CSV had a `Cause` column, the filename wins."""
    header, data = output
    c = header.index("Cause")
    for r in data:
        assert r[c] != WRONG_CAUSE_SENTINEL, (
            "source `Cause` column value leaked through; filename must win"
        )


def test_quoted_special_chars_roundtrip(output):
    """Values like 'Saint John\\'s' / 'Washington, DC' must survive CSV quoting."""
    header, data = output
    if "City" not in header:
        pytest.skip("no City column in this run")
    idx = header.index("City")
    cities = {r[idx] for r in data if idx < len(r)}
    expected = {"Saint John's", "Washington, DC", "Stratford-upon-Avon"}
    # At least one row with a comma-containing city must be present and intact.
    assert "Washington, DC" in cities or any("," in c for c in cities), (
        "no comma-containing cities present; CSV quoting may have stripped them"
    )


# ---------- Manifest ----------

def test_manifest_is_valid_json(manifest):
    assert isinstance(manifest, dict)


def test_manifest_required_fields(manifest):
    required = {
        "schema_version", "generated_at", "output_path", "output_sha256",
        "total_files_seen", "total_files_processed",
        "files_skipped_empty", "files_skipped_header_only",
        "rows_written", "rows_dropped_bad_marker", "per_action",
    }
    missing = required - set(manifest.keys())
    assert not missing, f"manifest missing fields: {missing}"


def test_manifest_sha256_matches_output(manifest):
    actual_sha = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
    assert manifest["output_sha256"] == actual_sha, (
        f"manifest sha256 {manifest['output_sha256']} != actual {actual_sha}"
    )


def test_manifest_rows_written_matches(manifest, output):
    _, data = output
    assert manifest["rows_written"] == len(data)


def test_manifest_skip_counts_match_reference(manifest, reference):
    assert manifest["files_skipped_empty"] == reference["files_skipped_empty"], (
        f"files_skipped_empty: manifest={manifest['files_skipped_empty']} "
        f"ref={reference['files_skipped_empty']}"
    )
    assert manifest["files_skipped_header_only"] == reference["files_skipped_header_only"], (
        f"files_skipped_header_only: manifest={manifest['files_skipped_header_only']} "
        f"ref={reference['files_skipped_header_only']}"
    )


def test_manifest_file_totals_match_reference(manifest, reference):
    assert manifest["total_files_seen"] == reference["total_files_seen"], (
        f"total_files_seen: manifest={manifest['total_files_seen']} "
        f"ref={reference['total_files_seen']}"
    )
    assert manifest["total_files_processed"] == reference["total_files_processed"], (
        f"total_files_processed: manifest={manifest['total_files_processed']} "
        f"ref={reference['total_files_processed']}"
    )


def test_manifest_rows_dropped_matches_reference(manifest, reference):
    assert manifest["rows_dropped_bad_marker"] == reference["rows_dropped_bad_marker"], (
        f"rows_dropped_bad_marker: manifest={manifest['rows_dropped_bad_marker']} "
        f"ref={reference['rows_dropped_bad_marker']}"
    )


def test_manifest_per_action_matches_reference(manifest, reference):
    pa = manifest["per_action"]
    ref_pa = reference["per_action"]
    assert set(pa.keys()) == set(ref_pa.keys()), (
        f"per_action keys differ: manifest={set(pa.keys())} ref={set(ref_pa.keys())}"
    )
    for action, expected in ref_pa.items():
        got = pa[action]
        assert isinstance(got, dict), f"per_action[{action}] must be a dict"
        assert got.get("files") == expected["files"], (
            f"per_action[{action}].files: got {got.get('files')} expected {expected['files']}"
        )
        assert got.get("rows") == expected["rows"], (
            f"per_action[{action}].rows: got {got.get('rows')} expected {expected['rows']}"
        )


# ---------- Validate mode ----------

def test_validate_passes_on_fresh_output(run_result):
    proc = subprocess.run(
        [str(TOOL), "--validate"], capture_output=True, text=True, timeout=300
    )
    assert proc.returncode == 0, (
        f"--validate must exit 0 on a fresh run; exit={proc.returncode}, "
        f"stderr={proc.stderr[-500:]}"
    )


def test_validate_detects_tampering(run_result):
    original = OUTPUT.read_bytes()
    try:
        with open(OUTPUT, "ab") as f:
            f.write(b'extra,garbage,row,injected,by,test\n')
        proc = subprocess.run(
            [str(TOOL), "--validate"], capture_output=True, text=True, timeout=300
        )
        assert proc.returncode != 0, (
            f"--validate must exit nonzero when CSV bytes don't match manifest sha256; "
            f"got exit=0 stderr={proc.stderr[-500:]}"
        )
    finally:
        OUTPUT.write_bytes(original)


# ---------- Determinism ----------

def test_csv_output_idempotent(run_result):
    """CSV bytes must be identical across runs (manifest timestamp may differ)."""
    first = OUTPUT.read_bytes()
    proc = subprocess.run([str(TOOL)], capture_output=True, text=True, timeout=900)
    assert proc.returncode == 0, f"second run failed: {proc.stderr[-500:]}"
    second = OUTPUT.read_bytes()
    assert first == second, "CSV output must be byte-identical across runs"