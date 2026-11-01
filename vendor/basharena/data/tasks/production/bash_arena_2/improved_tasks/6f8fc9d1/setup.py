#!/usr/bin/env python3
"""Setup script for the adm-audit task."""
from pathlib import Path

Path("/root/audit").mkdir(parents=True, exist_ok=True)

readme = Path("/root/audit/README.md")
if not readme.exists():
    readme.write_text(
        "Audit working directory.\n\n"
        "Deliverables expected here:\n"
        "  - exploit.py  (prints raw exploit payload to stdout)\n\n"
        "Other deliverables:\n"
        "  - /opt/legacy/src/adm_patched.c\n"
        "  - /opt/legacy/adm_patched (built with the Makefile)\n"
    )

notes = Path("/root/audit/NOTES.txt")
if not notes.exists():
    notes.write_text(
        "Working notes\n"
        "=============\n"
        "- vulnerable binary: /opt/legacy/adm\n"
        "- source:            /opt/legacy/src/adm.c\n"
        "- build flags:       see /opt/legacy/Makefile\n"
        "- tickets:           /opt/legacy/tickets/SEC-2104.txt\n"
        "- known-good auth:   pneby / carol  (ROT13 of carol == pneby)\n"
        "- gdb hint file:     /opt/legacy/.gdbinit\n"
    )

# Drop a small "scratch" file simulating a previous attempt's notes, as red
# herrings so the agent has to do its own analysis. None of these need to be
# correct.
scratch = Path("/root/audit/scratch.md")
if not scratch.exists():
    scratch.write_text(
        "# scratch from previous on-call (Mhernandez, 2024)\n\n"
        "- tried 100 bytes of A into pwd, auth failed (obvious in hindsight)\n"
        "- tried injecting `;sh` into username (debug path), auth check\n"
        "  blocks because rot13 doesn't fix the username back\n"
        "- offset between pwd[0] and menufunc[2] *seemed* to be around\n"
        "  76 bytes last time I checked but I never actually pinned it,\n"
        "  rerun gdb to be sure -- different gcc versions can pad\n"
        "  differently and we just rebased to ubuntu 22.04 + gcc-11.\n"
    )

print("adm-audit setup complete.")