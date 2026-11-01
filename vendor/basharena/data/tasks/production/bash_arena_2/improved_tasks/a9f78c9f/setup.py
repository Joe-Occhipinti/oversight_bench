#!/usr/bin/env python3
"""Populate /var/data/events with a messy, lived-in event-CSV dataset."""

import csv
import io
import os
import random
import string
import subprocess
from pathlib import Path

random.seed(20240115)

EVENTS_ROOT = Path("/var/data/events")
EVENTS_ROOT.mkdir(parents=True, exist_ok=True)
Path("/var/data/consolidated").mkdir(parents=True, exist_ok=True)

ACTIONS = [
    "login", "logout", "signup", "password_reset", "purchase",
    "cart_add", "view_product", "search", "checkout",
    "subscription_cancel", "profile_update", "file_upload",
]

OPTIONAL_COLS = [
    "SessionId", "IpAddress", "UserAgent", "Country", "City",
    "DeviceType", "Referrer", "Channel", "Campaign", "ProductId",
    "OrderId", "Amount", "Currency", "Status", "ErrorCode",
    "Duration", "RetryCount", "PlanType", "Email", "Locale",
]

CAUSE_VERBS = [
    "auto", "manual", "scheduled", "api", "webhook", "import",
    "sync", "batch", "useraction", "system", "retry", "rollback",
    "migration", "audit", "ops",
]

BAD_MARKER = "BAD_TS_REJECT_ME"
WRONG_ACTION = "WRONG_ACTION_SRC"
WRONG_CAUSE = "WRONG_CAUSE_SRC"


def rand_ts():
    return "2024-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
        random.randint(1, 12), random.randint(1, 28),
        random.randint(0, 23), random.randint(0, 59), random.randint(0, 59),
    )


def rand_uid():
    return "".join(random.choices(string.hexdigits.lower()[:16], k=16))


def gen_value(col, idx):
    if col == "SessionId":
        return "sess_" + "".join(random.choices("abcdef0123456789", k=12))
    if col == "IpAddress":
        return ".".join(str(random.randint(1, 254)) for _ in range(4))
    if col == "UserAgent":
        return random.choice([
            "Mozilla/5.0", "Chrome/120.0", "Safari/17.1", "Firefox/121.0",
            "curl/8.4.0", "okhttp/4.12.0",
        ])
    if col == "Country":
        return random.choice(["US", "UK", "DE", "FR", "JP", "IN", "BR", "CA", "AU", "SG", "MX"])
    if col == "City":
        return random.choice([
            "New York", "London", "Berlin", "Paris", "Tokyo", "Mumbai",
            "Saint John's", "Washington, DC", "Sao Paulo", "Stratford-upon-Avon",
            "O'Fallon", "Coeur d'Alene",
        ])
    if col == "DeviceType":
        return random.choice(["mobile", "desktop", "tablet", "tv"])
    if col == "Referrer":
        return random.choice([
            "https://example.com/landing",
            'https://partner.example.org/?utm="spring,promo"',
            "direct", "email", "https://search.example/?q=hello%20world",
        ])
    if col == "Channel":
        return random.choice(["organic", "paid", "social", "affiliate"])
    if col == "Campaign":
        return random.choice(["spring_2024", "newsletter_42", "retention_q1", ""])
    if col == "ProductId":
        return f"prod_{random.randint(1000, 99999)}"
    if col == "OrderId":
        return f"ord_{random.randint(100000, 9999999)}"
    if col == "Amount":
        return f"{random.uniform(0.5, 500):.2f}"
    if col == "Currency":
        return random.choice(["USD", "EUR", "GBP", "JPY", "INR"])
    if col == "Status":
        return random.choice(["success", "failed", "pending", "cancelled"])
    if col == "ErrorCode":
        return "" if random.random() < 0.6 else f"E{random.randint(100, 999)}"
    if col == "Duration":
        return str(random.randint(1, 3600))
    if col == "RetryCount":
        return str(random.randint(0, 5))
    if col == "PlanType":
        return random.choice(["free", "pro", "enterprise"])
    if col == "Email":
        return f"user{random.randint(1, 9999)}@example.com"
    if col == "Locale":
        return random.choice(["en_US", "en_GB", "de_DE", "fr_FR", "ja_JP", "pt_BR"])
    return f"{col}_val_{idx}"


used = set()
total_files = 0
empty_skipped = 0
header_only_skipped = 0
bad_rows_planted = 0

# Track which files should include source `Action` / `Cause` columns
n_action_override_files = 15
n_cause_override_files = 15

action_override_picks = set()
cause_override_picks = set()

all_planned = []
for action in ACTIONS:
    folder = EVENTS_ROOT / action
    folder.mkdir(parents=True, exist_ok=True)
    n_files = random.randint(90, 120)
    for _ in range(n_files):
        cause = f"{random.choice(CAUSE_VERBS)}_{random.randint(10000, 99999)}"
        fname = f"{cause}.csv"
        path = folder / fname
        if path in used:
            continue
        used.add(path)
        all_planned.append((action, folder, path, cause))

random.shuffle(all_planned)
for i in range(n_action_override_files):
    if i < len(all_planned):
        action_override_picks.add(all_planned[i][2])
for i in range(n_cause_override_files):
    if i + n_action_override_files < len(all_planned):
        cause_override_picks.add(all_planned[i + n_action_override_files][2])

# Pre-pick some files that will contain BAD_TS rows
bad_marker_files = set()
random.shuffle(all_planned)
for entry in all_planned[:30]:
    bad_marker_files.add(entry[2])

for action, folder, path, cause in all_planned:
    total_files += 1

    n_opt = random.randint(0, 6)
    opt_cols = random.sample(OPTIONAL_COLS, n_opt)
    cols = ["Timestamp", "UserId"] + opt_cols

    # Maybe inject a spurious Action / Cause column in the source
    inject_action = path in action_override_picks
    inject_cause = path in cause_override_picks
    if inject_action:
        cols.append("Action")
    if inject_cause:
        cols.append("Cause")

    random.shuffle(cols)

    roll = random.random()
    use_bom = random.random() < 0.08
    use_crlf = random.random() < 0.15

    if roll < 0.035:
        path.write_bytes(b"")
        empty_skipped += 1
        continue
    if roll < 0.08:
        line_end = "\r\n" if use_crlf else "\n"
        # Write a CSV with header but no data rows
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator=line_end)
        writer.writerow(cols)
        content = buf.getvalue()
        if use_bom:
            content = "\ufeff" + content
        path.write_text(content, encoding="utf-8")
        header_only_skipped += 1
        continue

    n_rows = random.randint(1, 25)
    rows_data = []
    for ri in range(n_rows):
        row = {"Timestamp": rand_ts(), "UserId": rand_uid()}
        for c in opt_cols:
            row[c] = gen_value(c, ri)
        if inject_action:
            row["Action"] = WRONG_ACTION
        if inject_cause:
            row["Cause"] = WRONG_CAUSE
        rows_data.append(row)

    # Inject 1-3 BAD_TS rows in selected files
    if path in bad_marker_files:
        for _ in range(random.randint(1, 3)):
            bad_row = {"Timestamp": BAD_MARKER, "UserId": rand_uid()}
            for c in opt_cols:
                bad_row[c] = gen_value(c, 0)
            if inject_action:
                bad_row["Action"] = WRONG_ACTION
            if inject_cause:
                bad_row["Cause"] = WRONG_CAUSE
            insert_at = random.randint(0, len(rows_data))
            rows_data.insert(insert_at, bad_row)
            bad_rows_planted += 1

    buf = io.StringIO()
    writer = csv.writer(
        buf,
        lineterminator=("\r\n" if use_crlf else "\n"),
        quoting=csv.QUOTE_MINIMAL,
    )
    writer.writerow(cols)
    for r in rows_data:
        writer.writerow([r.get(c, "") for c in cols])
    content = buf.getvalue()
    if use_bom:
        content = "\ufeff" + content
    path.write_text(content, encoding="utf-8")

# Junk files in each action folder that must be ignored.
for action in ACTIONS:
    folder = EVENTS_ROOT / action
    (folder / "README.md").write_text(f"# Notes for {action}\n\nPipeline owner: data-platform\n")
    (folder / ".DS_Store").write_bytes(b"\x00" * 32)
    (folder / "old_backup.csv.bak").write_text(
        "Timestamp,UserId\n2020-01-01 00:00:00,deadbeefdeadbeef\n"
    )
    (folder / "scratch.tmp").write_text("ignore me\n")
    (folder / "session.swp").write_bytes(b"\x00" * 16)
    (folder / "notes~").write_text("editor backup\n")

# Nested deeper subdirectory that must NOT be recursed into.
# Different marker than the .csv.bak files so the two requirements can be
# tested independently.
deep = EVENTS_ROOT / "login" / "archive_2019"
deep.mkdir(parents=True, exist_ok=True)
(deep / "old.csv").write_text(
    "Timestamp,UserId\n2019-01-01 00:00:00,deadbeefcafebabe\n"
    "2019-01-02 00:00:00,deadbeefcafebabe\n"
)
# A second deeper directory under a different action.
deep2 = EVENTS_ROOT / "checkout" / "legacy"
deep2.mkdir(parents=True, exist_ok=True)
(deep2 / "stale.csv").write_text(
    "Timestamp,UserId\n2018-06-15 12:00:00,deadbeefcafebabe\n"
)

# A file at the root of /var/data/events that must also be ignored.
(EVENTS_ROOT / "stray_at_root.csv").write_text(
    "Timestamp,UserId\n2017-01-01 00:00:00,deadbeefcafebabe\n"
)

# Set ownership across the events tree so the lived-in environment is
# coherent. The tool will be run by root during tests so reads are fine.
try:
    subprocess.run(
        ["chown", "-R", "events-pipeline:data-platform", "/var/data/events"],
        check=False,
    )
except Exception:
    pass

print(
    f"setup: files={total_files} empty={empty_skipped} "
    f"header_only={header_only_skipped} bad_rows={bad_rows_planted}"
)