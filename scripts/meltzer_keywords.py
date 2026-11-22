"""List words in a meltzer tree that could point at a planted design choice.

Usage: python scripts/meltzer_keywords.py [--version v0.2]

Searches source, tests and docs (spec-meltzer-v0.2.md section 10). Every hit
is listed for triage; a hit is not necessarily wrong. Exit status 1 when there
are hits.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Word stems and phrases (case-insensitive). Benchmark-side words are included:
# none of them belongs in the shipped tree.
PATTERNS = [
    r"vulnerab", r"exploit", r"bypass", r"\battack", r"insecure", r"\bunsafe\b", r"loophole",
    r"backdoor", r"malicious", r"exfiltrat", r"not checked", r"not reviewed", r"intentional",
    r"deliberate", r"on purpose", r"\bTODO\b", r"\bFIXME\b", r"\bXXX\b", r"\bhack\b",
    r"\bplanted\b", r"\bside task", r"\bbenchmark", r"\bevaluator\b", r"\boversight\b",
    r"\bchain\b", r"\bcards?\b", r"red.team", r"\bweakness", r"\bflaw",
]
# Lines known to be fine: v0.1 monitor policy text and the documented CLI flag.
ALLOWED = [
    r"--dangerously-bypass-approvals-and-sandbox",
    r"dangerously_bypass_approvals_and_sandbox",
    r"test_bypass_flag",
    r"bypass_readonly",  # prompt_toolkit argument
]
# Model-facing monitor policy text, kept from v0.1; its risk categories name these words.
ALLOWED_FILES = {"src/meltzer/monitor/policy.md", "src/meltzer/monitor/policy_template.md"}
SUFFIXES = {".py", ".md", ".toml", ".json", ".sbpl"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="v0.2")
    args = parser.parse_args()
    tree = ROOT / "meltzer" / args.version
    pattern = re.compile("|".join(PATTERNS), re.IGNORECASE)
    allowed = re.compile("|".join(ALLOWED))
    hits = 0
    for path in sorted(tree.rglob("*")):
        if not path.is_file() or path.suffix not in SUFFIXES or "__pycache__" in path.parts:
            continue
        if path.relative_to(tree).as_posix() in ALLOWED_FILES:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if pattern.search(allowed.sub("", line)):
                hits += 1
                print(f"{path.relative_to(tree)}:{number}: {line.strip()[:160]}")
    print(f"{hits} hit(s)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
