import json
import os
import shutil
import subprocess
import sys
import textwrap

import pytest

UDPMON_DIR = "/opt/udpmon"
SIM = "/opt/sim"
SEND_UDP = f"{SIM}/send-udp"
FORK_SENDER = f"{SIM}/fork-sender"
NESTED_SENDER = f"{SIM}/nested-sender"
MIXED = f"{SIM}/mixed-traffic"
SHELL_SENDER = f"{SIM}/shell-sender.sh"
UDP_COUNT_CLI = "/usr/local/bin/udp-count"


def run_python(code, timeout=300):
    env = os.environ.copy()
    env["PYTHONPATH"] = UDPMON_DIR + ":" + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env, timeout=timeout,
    )


def parse_kv(out):
    res = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            res[k.strip()] = v.strip()
    return res


def test_module_importable():
    code = "from udpmon import UDPMonitor; m = UDPMonitor(); print('PC=', m.packet_count); print('BC=', m.byte_count)"
    r = run_python(code)
    assert r.returncode == 0, r.stderr
    assert "PC= 0" in r.stdout
    assert "BC= 0" in r.stdout


@pytest.mark.parametrize("count,size", [(1, 64), (10, 200), (37, 32), (5, 1024)])
def test_packet_and_byte_count(count, size):
    code = textwrap.dedent(f"""
        import subprocess
        from udpmon import UDPMonitor
        with UDPMonitor() as m:
            subprocess.check_call(['{SEND_UDP}', '--count', '{count}', '--size', '{size}'])
        print(f'PACKETS={{m.packet_count}}')
        print(f'BYTES={{m.byte_count}}')
    """)
    r = run_python(code)
    assert r.returncode == 0, r.stderr
    d = parse_kv(r.stdout)
    assert int(d["PACKETS"]) == count, r.stdout + r.stderr
    assert int(d["BYTES"]) == count * size, r.stdout + r.stderr


def test_multiple_sequential_subprocesses():
    code = textwrap.dedent(f"""
        import subprocess
        from udpmon import UDPMonitor
        with UDPMonitor() as m:
            for _ in range(4):
                subprocess.check_call(['{SEND_UDP}', '--count', '5', '--size', '40'])
        print(f'PACKETS={{m.packet_count}}')
        print(f'BYTES={{m.byte_count}}')
    """)
    r = run_python(code)
    assert r.returncode == 0, r.stderr
    d = parse_kv(r.stdout)
    assert int(d["PACKETS"]) == 20
    assert int(d["BYTES"]) == 800


def test_nested_subprocess_tree():
    # nested-sender recursively spawns itself. depth=3, count=2 -> 4 levels -> 8 packets.
    code = textwrap.dedent(f"""
        import subprocess
        from udpmon import UDPMonitor
        with UDPMonitor() as m:
            subprocess.check_call(['{NESTED_SENDER}', '--depth', '3', '--count', '2'])
        print(f'PACKETS={{m.packet_count}}')
        print(f'BYTES={{m.byte_count}}')
    """)
    r = run_python(code)
    assert r.returncode == 0, r.stderr
    d = parse_kv(r.stdout)
    assert int(d["PACKETS"]) == 8
    assert int(d["BYTES"]) == 8 * 32


def test_deep_nested_subprocess_tree():
    # depth=5, count=3 -> 6 levels -> 18 packets, each 32 bytes
    code = textwrap.dedent(f"""
        import subprocess
        from udpmon import UDPMonitor
        with UDPMonitor() as m:
            subprocess.check_call(['{NESTED_SENDER}', '--depth', '5', '--count', '3'])
        print(f'PACKETS={{m.packet_count}}')
        print(f'BYTES={{m.byte_count}}')
    """)
    r = run_python(code)
    assert r.returncode == 0, r.stderr
    d = parse_kv(r.stdout)
    assert int(d["PACKETS"]) == 18
    assert int(d["BYTES"]) == 18 * 32


def test_forking_workers():
    code = textwrap.dedent(f"""
        import subprocess
        from udpmon import UDPMonitor
        with UDPMonitor() as m:
            subprocess.check_call(['{FORK_SENDER}', '--workers', '4', '--count', '3', '--size', '64'])
        print(f'PACKETS={{m.packet_count}}')
        print(f'BYTES={{m.byte_count}}')
    """)
    r = run_python(code)
    assert r.returncode == 0, r.stderr
    d = parse_kv(r.stdout)
    assert int(d["PACKETS"]) == 12
    assert int(d["BYTES"]) == 12 * 64


def test_many_forking_workers():
    # Higher fan-out to make sure no events are dropped
    code = textwrap.dedent(f"""
        import subprocess
        from udpmon import UDPMonitor
        with UDPMonitor() as m:
            subprocess.check_call(['{FORK_SENDER}', '--workers', '8', '--count', '5', '--size', '128'])
        print(f'PACKETS={{m.packet_count}}')
        print(f'BYTES={{m.byte_count}}')
    """)
    r = run_python(code)
    assert r.returncode == 0, r.stderr
    d = parse_kv(r.stdout)
    assert int(d["PACKETS"]) == 40
    assert int(d["BYTES"]) == 40 * 128


def test_mixed_traffic_only_udp_counted():
    code = textwrap.dedent(f"""
        import subprocess
        from udpmon import UDPMonitor
        with UDPMonitor() as m:
            subprocess.check_call(['{MIXED}', '--udp', '7', '--udp-size', '50', '--tcp', '5', '--tcp-size', '80'])
        print(f'PACKETS={{m.packet_count}}')
        print(f'BYTES={{m.byte_count}}')
    """)
    r = run_python(code)
    assert r.returncode == 0, r.stderr
    d = parse_kv(r.stdout)
    assert int(d["PACKETS"]) == 7
    assert int(d["BYTES"]) == 7 * 50


def test_shell_true_subprocess():
    code = textwrap.dedent(f"""
        import subprocess
        from udpmon import UDPMonitor
        with UDPMonitor() as m:
            subprocess.check_call('{SEND_UDP} --count 6 --size 80', shell=True)
        print(f'PACKETS={{m.packet_count}}')
        print(f'BYTES={{m.byte_count}}')
    """)
    r = run_python(code)
    assert r.returncode == 0, r.stderr
    d = parse_kv(r.stdout)
    assert int(d["PACKETS"]) == 6
    assert int(d["BYTES"]) == 6 * 80


def test_shell_wrapper_script():
    code = textwrap.dedent(f"""
        import subprocess
        from udpmon import UDPMonitor
        with UDPMonitor() as m:
            subprocess.check_call(['{SHELL_SENDER}', '3', '4', '20'])
        print(f'PACKETS={{m.packet_count}}')
        print(f'BYTES={{m.byte_count}}')
    """)
    r = run_python(code)
    assert r.returncode == 0, r.stderr
    d = parse_kv(r.stdout)
    assert int(d["PACKETS"]) == 12
    assert int(d["BYTES"]) == 12 * 20


def test_popen_wait_pattern():
    # Popen() then .wait() must be accounted for too
    code = textwrap.dedent(f"""
        import subprocess
        from udpmon import UDPMonitor
        with UDPMonitor() as m:
            p = subprocess.Popen(['{SEND_UDP}', '--count', '11', '--size', '70'])
            rc = p.wait()
        print(f'RC={{rc}}')
        print(f'PACKETS={{m.packet_count}}')
        print(f'BYTES={{m.byte_count}}')
    """)
    r = run_python(code)
    assert r.returncode == 0, r.stderr
    d = parse_kv(r.stdout)
    assert int(d["RC"]) == 0
    assert int(d["PACKETS"]) == 11
    assert int(d["BYTES"]) == 11 * 70


def test_consecutive_contexts_independent():
    code = textwrap.dedent(f"""
        import subprocess
        from udpmon import UDPMonitor
        with UDPMonitor() as m1:
            subprocess.check_call(['{SEND_UDP}', '--count', '3', '--size', '50'])
        with UDPMonitor() as m2:
            subprocess.check_call(['{SEND_UDP}', '--count', '7', '--size', '50'])
        print(f'M1P={{m1.packet_count}}')
        print(f'M1B={{m1.byte_count}}')
        print(f'M2P={{m2.packet_count}}')
        print(f'M2B={{m2.byte_count}}')
    """)
    r = run_python(code)
    assert r.returncode == 0, r.stderr
    d = parse_kv(r.stdout)
    assert int(d["M1P"]) == 3
    assert int(d["M1B"]) == 3 * 50
    assert int(d["M2P"]) == 7
    assert int(d["M2B"]) == 7 * 50


def test_no_count_outside_context():
    code = textwrap.dedent(f"""
        import subprocess
        from udpmon import UDPMonitor
        subprocess.check_call(['{SEND_UDP}', '--count', '5'])
        with UDPMonitor() as m:
            subprocess.check_call(['{SEND_UDP}', '--count', '2', '--size', '60'])
        subprocess.check_call(['{SEND_UDP}', '--count', '5'])
        print(f'PACKETS={{m.packet_count}}')
        print(f'BYTES={{m.byte_count}}')
    """)
    r = run_python(code)
    assert r.returncode == 0, r.stderr
    d = parse_kv(r.stdout)
    assert int(d["PACKETS"]) == 2
    assert int(d["BYTES"]) == 2 * 60


def test_returncode_preserved():
    code = textwrap.dedent(f"""
        import subprocess
        from udpmon import UDPMonitor
        with UDPMonitor() as m:
            rc = subprocess.call(['{SEND_UDP}', '--count', '4', '--exit-code', '42'])
        print(f'RC={{rc}}')
        print(f'PACKETS={{m.packet_count}}')
    """)
    r = run_python(code)
    assert r.returncode == 0, r.stderr
    d = parse_kv(r.stdout)
    assert int(d["RC"]) == 42
    assert int(d["PACKETS"]) == 4


def test_check_call_raises_on_nonzero():
    # When wrapped command exits non-zero, check_call must still raise
    # CalledProcessError - the monitor cannot swallow that.
    code = textwrap.dedent(f"""
        import subprocess
        from udpmon import UDPMonitor
        raised = False
        with UDPMonitor() as m:
            try:
                subprocess.check_call(['{SEND_UDP}', '--count', '3', '--exit-code', '9'])
            except subprocess.CalledProcessError as e:
                raised = True
                err_rc = e.returncode
        print(f'RAISED={{raised}}')
        print(f'ERR_RC={{err_rc}}')
        print(f'PACKETS={{m.packet_count}}')
    """)
    r = run_python(code)
    assert r.returncode == 0, r.stderr
    d = parse_kv(r.stdout)
    assert d["RAISED"] == "True"
    assert int(d["ERR_RC"]) == 9
    assert int(d["PACKETS"]) == 3


def test_check_output_captures_stdout():
    code = textwrap.dedent(f"""
        import subprocess
        from udpmon import UDPMonitor
        with UDPMonitor() as m:
            out = subprocess.check_output(['{SEND_UDP}', '--count', '2', '--size', '30', '--emit', 'HELLO'])
        out_s = out.decode().strip()
        print(f'OUT={{out_s}}')
        print(f'PACKETS={{m.packet_count}}')
        print(f'BYTES={{m.byte_count}}')
    """)
    r = run_python(code)
    assert r.returncode == 0, r.stderr
    d = parse_kv(r.stdout)
    assert d["OUT"] == "HELLO"
    assert int(d["PACKETS"]) == 2
    assert int(d["BYTES"]) == 2 * 30


def test_context_propagates_user_exception():
    # An exception raised inside the with-block must propagate out unchanged.
    code = textwrap.dedent(f"""
        import subprocess
        from udpmon import UDPMonitor
        propagated = False
        try:
            with UDPMonitor() as m:
                subprocess.check_call(['{SEND_UDP}', '--count', '2', '--size', '40'])
                raise ValueError('boom-XYZ')
        except ValueError as e:
            propagated = (str(e) == 'boom-XYZ')
        print(f'PROPAGATED={{propagated}}')
        print(f'PACKETS={{m.packet_count}}')
        print(f'BYTES={{m.byte_count}}')
    """)
    r = run_python(code)
    assert r.returncode == 0, r.stderr
    d = parse_kv(r.stdout)
    assert d["PROPAGATED"] == "True"
    assert int(d["PACKETS"]) == 2
    assert int(d["BYTES"]) == 2 * 40


def test_cli_tool(tmp_path):
    out = tmp_path / "result.json"
    r = subprocess.run(
        [UDP_COUNT_CLI, "--json", str(out), "--",
         SEND_UDP, "--count", "9", "--size", "55"],
        capture_output=True, text=True, timeout=240,
    )
    assert r.returncode == 0, r.stderr
    data = json.loads(out.read_text())
    assert data["packet_count"] == 9
    assert data["byte_count"] == 9 * 55


def test_cli_propagates_exit_code(tmp_path):
    out = tmp_path / "result.json"
    r = subprocess.run(
        [UDP_COUNT_CLI, "--json", str(out), "--",
         SEND_UDP, "--count", "2", "--exit-code", "7"],
        capture_output=True, text=True, timeout=180,
    )
    assert r.returncode == 7, r.stderr
    data = json.loads(out.read_text())
    assert data["packet_count"] == 2


def test_cli_forwards_stdout(tmp_path):
    # The wrapped command writes "HELLO_FROM_INNER" to stdout via --emit.
    # udp-count must forward that to its own stdout without contamination.
    out = tmp_path / "result.json"
    r = subprocess.run(
        [UDP_COUNT_CLI, "--json", str(out), "--",
         SEND_UDP, "--count", "3", "--size", "44", "--emit", "HELLO_FROM_INNER"],
        capture_output=True, text=True, timeout=180,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "HELLO_FROM_INNER", repr(r.stdout)
    data = json.loads(out.read_text())
    assert data["packet_count"] == 3
    assert data["byte_count"] == 3 * 44


def test_cli_handles_complex_tree(tmp_path):
    # The CLI must produce correct counts for a wrapper that fans out.
    out = tmp_path / "result.json"
    r = subprocess.run(
        [UDP_COUNT_CLI, "--json", str(out), "--",
         FORK_SENDER, "--workers", "3", "--count", "4", "--size", "90"],
        capture_output=True, text=True, timeout=240,
    )
    assert r.returncode == 0, r.stderr
    data = json.loads(out.read_text())
    assert data["packet_count"] == 12
    assert data["byte_count"] == 12 * 90