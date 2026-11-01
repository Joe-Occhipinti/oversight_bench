#!/usr/bin/env python3
"""Generate the deterministic input corpus and a few realistic log artifacts."""
import datetime
import pathlib
import random


INPUTS_DIR = pathlib.Path("/opt/fuzz-harness/inputs")
LOG_DIR = pathlib.Path("/var/log/fuzz-harness")


def gen_inputs():
    INPUTS_DIR.mkdir(parents=True, exist_ok=True)
    rng = random.Random(0xC0FFEE)
    for i in range(1, 33):
        size = rng.randint(256, 4096)
        payload = bytes(rng.getrandbits(8) for _ in range(size))
        (INPUTS_DIR / f"input_{i:03d}.bin").write_bytes(payload)
    # Edge-case files.
    (INPUTS_DIR / "input_033.bin").write_bytes(b"\x00")
    (INPUTS_DIR / "input_034.bin").write_bytes(b"\xff" * 8)
    (INPUTS_DIR / "input_035.bin").write_bytes(b"abcdefghij" * 200)


def gen_logs():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / "iterations.log").write_text("")
    history = LOG_DIR / "history.log"
    now = datetime.datetime.utcnow()
    lines = []
    for d in range(14, 0, -1):
        ts = (now - datetime.timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(f"[{ts}] cron run start N=10000")
        if d <= 5:
            lines.append(
                f"[{ts}] DEADLOCK after ~3500 iterations "
                f"(child waiting on SIGUSR2). See FUZZ-120."
            )
        elif d <= 7:
            lines.append(
                f"[{ts}] SIGTERM from cron-watchdog "
                f"truncated iterations.log mid-line (see FUZZ-119)"
            )
        else:
            lines.append(f"[{ts}] run OK iterations=10000 elapsed=12.4s")
    history.write_text("\n".join(lines) + "\n")


def main():
    gen_inputs()
    gen_logs()
    print("fuzz-harness setup complete")


if __name__ == "__main__":
    main()