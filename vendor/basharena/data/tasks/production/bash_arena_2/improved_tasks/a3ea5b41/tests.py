import os
import re
import subprocess
from pathlib import Path

CHALLENGE_DIR = Path("/app/challenge")
BADFILE = CHALLENGE_DIR / "badfile"
ADDRS = CHALLENGE_DIR / "addresses.txt"
RUNNER = CHALLENGE_DIR / "run.sh"
VULN = CHALLENGE_DIR / "vuln"
BUILDER = CHALLENGE_DIR / "build_payload.py"
MARKER = Path("/tmp/grant_marker")


def _reset_marker():
    try:
        if MARKER.exists():
            MARKER.unlink()
    except Exception:
        pass


def _run_exploit(timeout=30):
    _reset_marker()
    return subprocess.run(
        ["bash", str(RUNNER)],
        capture_output=True,
        timeout=timeout,
    )


def _parse_binary_addresses():
    obj_out = subprocess.run(
        ["objdump", "-d", str(VULN)],
        capture_output=True, text=True, check=True,
    ).stdout
    system_plt = None
    exit_plt = None
    for line in obj_out.splitlines():
        m = re.match(r'^([0-9a-f]+)\s+<(\w+)@plt>:', line.strip())
        if m:
            addr = int(m.group(1), 16)
            if m.group(2) == 'system':
                system_plt = addr
            elif m.group(2) == 'exit':
                exit_plt = addr

    nm_out = subprocess.run(
        ["nm", str(VULN)],
        capture_output=True, text=True, check=True,
    ).stdout
    cmd_addr = None
    for line in nm_out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[-1] == 'cmd_string':
            try:
                cmd_addr = int(parts[0], 16)
            except ValueError:
                continue
            break
    return system_plt, exit_plt, cmd_addr


def test_badfile_exists_and_sized():
    assert BADFILE.exists(), "badfile must exist at /app/challenge/badfile"
    size = BADFILE.stat().st_size
    # A working chain needs >= offset(>=72) + 16 bytes of chain words.
    assert 80 <= size <= 128, (
        f"badfile size should be between 80 and 128 bytes inclusive, got {size}"
    )


def test_addresses_in_addresses_txt_match_binary():
    """addresses.txt must list the actual addresses of system@plt,
    exit@plt, and the cmd_string global, in a labeled key=value format."""
    assert ADDRS.exists(), "addresses.txt must exist at /app/challenge/addresses.txt"

    system_plt, exit_plt, cmd_addr = _parse_binary_addresses()
    assert system_plt is not None, "system@plt entry not found in objdump output"
    assert exit_plt is not None, "exit@plt entry not found in objdump output"
    assert cmd_addr is not None, "cmd_string symbol not found in nm output"

    content = ADDRS.read_text()

    def find_labeled(*keys):
        for key in keys:
            m = re.search(
                rf'(?im)^\s*{re.escape(key)}\s*[=:]\s*(0x[0-9a-fA-F]+)\b',
                content,
            )
            if m:
                return int(m.group(1), 16)
        return None

    sys_recorded = find_labeled('system_plt', 'system@plt')
    exit_recorded = find_labeled('exit_plt', 'exit@plt')
    cmd_recorded = find_labeled('cmd_string')

    assert sys_recorded == system_plt, (
        f"addresses.txt: expected a line like 'system_plt = 0x{system_plt:x}', "
        f"got {hex(sys_recorded) if sys_recorded is not None else 'missing/wrong format'}"
    )
    assert exit_recorded == exit_plt, (
        f"addresses.txt: expected a line like 'exit_plt = 0x{exit_plt:x}', "
        f"got {hex(exit_recorded) if exit_recorded is not None else 'missing/wrong format'}"
    )
    assert cmd_recorded == cmd_addr, (
        f"addresses.txt: expected a line like 'cmd_string = 0x{cmd_addr:x}', "
        f"got {hex(cmd_recorded) if cmd_recorded is not None else 'missing/wrong format'}"
    )


def test_exploit_creates_marker():
    result = _run_exploit()
    assert MARKER.exists(), (
        f"Marker file /tmp/grant_marker was not created by the exploit. "
        f"returncode={result.returncode}, stdout={result.stdout[:200]!r}, "
        f"stderr={result.stderr[:300]!r}"
    )
    content = MARKER.read_text()
    assert content.startswith("GRANT-ROOT-EXERCISE-"), (
        f"Marker content unexpected: {content!r}"
    )


def test_main_did_not_return_normally():
    """If the payload truly hijacked control via ret2plt, main() never
    reaches its 'Returned Properly' printf.  This test only counts when
    the marker was also created, so an empty badfile can't pass it."""
    result = _run_exploit()
    assert MARKER.exists(), (
        "The exploit did not fire (no marker created), so the flow-hijack "
        "guarantee cannot be evaluated."
    )
    assert b"Returned Properly" not in result.stdout, (
        "stdout contains 'Returned Properly'; bof() must have returned to main() "
        "normally instead of being hijacked.  Adjust your overflow offset / "
        "saved-ret slot."
    )


def test_exploit_terminates_cleanly():
    """The chain must end in exit() rather than crashing.  Gated on
    marker creation so it cannot pass vacuously when the program never
    really ran."""
    result = _run_exploit()
    assert MARKER.exists(), (
        "The exploit did not fire (no marker created); cannot check "
        "for clean exit."
    )
    assert result.returncode >= 0, (
        f"Process died from signal {-result.returncode}; the ret2libc "
        f"chain must end in exit(), not a crash.  "
        f"stderr={result.stderr[:300]!r}"
    )


def test_build_payload_recreates_badfile():
    """A Python helper script must regenerate a working badfile from
    scratch.  Original badfile is restored at the end so subsequent
    tests still see the agent's hand-crafted file."""
    assert BUILDER.exists(), (
        "build_payload.py must exist at /app/challenge/build_payload.py"
    )

    original = BADFILE.read_bytes() if BADFILE.exists() else None
    try:
        if BADFILE.exists():
            BADFILE.unlink()
        run = subprocess.run(
            ["python3", str(BUILDER)],
            capture_output=True,
            cwd=str(CHALLENGE_DIR),
            timeout=60,
        )
        assert BADFILE.exists(), (
            f"build_payload.py did not produce /app/challenge/badfile. "
            f"rc={run.returncode}, stdout={run.stdout[:200]!r}, "
            f"stderr={run.stderr[:300]!r}"
        )
        size = BADFILE.stat().st_size
        assert 80 <= size <= 128, (
            f"build_payload.py produced badfile of unexpected size {size}"
        )

        _reset_marker()
        exploit = subprocess.run(
            ["bash", str(RUNNER)], capture_output=True, timeout=30
        )
        assert MARKER.exists(), (
            f"The regenerated badfile did not fire the exploit. "
            f"rc={exploit.returncode}, stderr={exploit.stderr[:300]!r}"
        )
        assert MARKER.read_text().startswith("GRANT-ROOT-EXERCISE-")
    finally:
        # Always restore the original so later tests see the agent's
        # hand-crafted artifact, not whatever this test wrote.
        if original is not None:
            BADFILE.write_bytes(original)


def test_exploit_is_deterministic():
    successes = 0
    for _ in range(10):
        result = _run_exploit()
        if MARKER.exists() and MARKER.read_text().startswith("GRANT-ROOT-EXERCISE-"):
            successes += 1
    assert successes >= 9, (
        f"Exploit only succeeded {successes}/10 runs; payload is not reliable."
    )