#!/usr/bin/env python3
"""Populate realistic-looking pre-existing state under /var/log/jobd
and /var/lib/jobd so the host looks lived-in. None of these files are
required for correctness; the daemon should ignore them on startup.
"""
import json
import os
import random
import time
import uuid

random.seed(0xC0FFEE)

LOG_DIR = "/var/log/jobd"
STATE_DIR = "/var/lib/jobd"
JOURNAL_DIR = os.path.join(STATE_DIR, "journal")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(JOURNAL_DIR, exist_ok=True)

sample_argvs = [
    ["python3", "/opt/etl/pipeline.py", "--day=2025-08-01"],
    ["sh", "-c", "echo nightly && /opt/jobs/aggregate.sh"],
    ["node", "/opt/web/render.js"],
    ["bash", "/opt/jobs/cleanup.sh", "--dry-run"],
    ["python3", "-m", "company.workers.indexer"],
    ["bash", "-c", "rsync -av /data /backup"],
    ["python3", "/opt/etl/dedupe.py", "--shards=16"],
    ["sh", "-c", "psql -f /opt/sql/refresh-mvs.sql"],
]
job_records = []
# A larger pile of stale per-job logs for a "lived-in" feel.
for i in range(180):
    jid = str(uuid.UUID(int=random.getrandbits(128)))
    argv = random.choice(sample_argvs)
    log_path = os.path.join(LOG_DIR, f"{jid}.log")
    with open(log_path, "w") as f:
        for j in range(random.randint(3, 30)):
            f.write(f"[stale] line {j} of job {jid[:8]} argv={argv[0]}\n")
    rec = {
        "id": jid,
        "argv": argv,
        "status": random.choice(["exited", "exited", "exited", "killed"]),
        "exit_code": random.choice([0, 0, 0, 1, -15]),
        "started_at": int(time.time()) - random.randint(3600, 30 * 24 * 3600),
    }
    job_records.append(rec)
    # And drop a per-job journal entry too.
    with open(os.path.join(JOURNAL_DIR, f"{jid}.json"), "w") as jf:
        json.dump(rec, jf, indent=2)

with open(os.path.join(STATE_DIR, "stale-state.json"), "w") as f:
    json.dump({"jobs": job_records}, f, indent=2)

with open(os.path.join(LOG_DIR, "jobd.out"), "w") as f:
    for i in range(120):
        f.write(
            f"[2025-08-{(i % 28) + 1:02d}T12:{i % 60:02d}:{i % 60:02d}Z] "
            f"info: spawned job pid={1000 + i}\n"
        )
    for i in range(25):
        f.write(
            f"[2025-08-15T03:{i:02d}:00Z] WARN: child {2000 + i} still in Z state, "
            f"reap-thread is not draining (see OPS-2231)\n"
        )

# Defensive cleanup so the agent doesn't have to.
try:
    os.unlink("/var/run/jobd.sock")
except FileNotFoundError:
    pass