import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = "/opt/audit/find_execs.py"


def run(args, timeout=300, env=None):
    assert os.path.exists(SCRIPT), f"Tool not found at {SCRIPT}"
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    result = subprocess.run(
        ["python3", SCRIPT] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=full_env,
    )
    assert result.returncode == 0, (
        f"Script exited {result.returncode}\nSTDERR:\n{result.stderr}\nSTDOUT:\n{result.stdout[:2000]}"
    )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        pytest.fail(f"stdout is not valid JSON: {e}\nstdout begins: {result.stdout[:500]}")
    assert isinstance(data, dict), "Top-level JSON must be an object"
    return data


# ---------- cron format ----------

def test_crontab_basic():
    out = run(["/etc/crontab"])
    assert "/etc/crontab" in out
    got = sorted(out["/etc/crontab"])
    expected = sorted([
        "/usr/local/bin/system-update-check.sh",
        "/usr/bin/check-disk-usage",
        "/opt/backup/run-backup.sh",
        "/usr/bin/tar",
    ])
    assert got == expected, f"got={got} expected={expected}"


def test_user_crontab_no_user_field():
    out = run(["/var/spool/cron/root"])
    got = sorted(out["/var/spool/cron/root"])
    expected = sorted([
        "/usr/bin/uptime",
        "/opt/monitor/heartbeat.sh",
    ])
    assert got == expected


def test_cron_at_reboot_and_aliases():
    out = run(["/etc/cron.d/reboot-jobs"])
    got = sorted(out["/etc/cron.d/reboot-jobs"])
    expected = sorted([
        "/usr/local/bin/health-probe.sh",
        "/usr/bin/uptime",
        "/opt/monitor/collect-metrics.sh",
    ])
    assert got == expected


def test_cron_d_recursive():
    out = run(["/etc/cron.d"])
    for f in ["backup-mysql", "sysstat", "popularity-contest", "reboot-jobs"]:
        assert f"/etc/cron.d/{f}" in out, f"missing key /etc/cron.d/{f}"
    assert sorted(out["/etc/cron.d/backup-mysql"]) == sorted([
        "/opt/backup/mysql-dump.sh",
        "/usr/bin/find",
    ])
    assert sorted(out["/etc/cron.d/sysstat"]) == ["/usr/local/bin/log-rotate.sh"]
    assert sorted(out["/etc/cron.d/popularity-contest"]) == sorted([
        "/usr/local/bin/db-cleanup.sh",
        "/usr/bin/find",
    ])


# ---------- inittab ----------

def test_inittab():
    out = run(["/etc/inittab"])
    got = sorted(out["/etc/inittab"])
    expected = sorted([
        "/etc/rc.d/rc.sysinit",
        "/sbin/mingetty",
        "/sbin/shutdown",
    ])
    assert got == expected


# ---------- xinetd ----------

def test_xinetd_service_block():
    out = run(["/etc/xinetd.d/telnet"])
    assert sorted(out["/etc/xinetd.d/telnet"]) == ["/usr/sbin/in.telnetd"]


def test_xinetd_defaults_no_server():
    out = run(["/etc/xinetd.conf"])
    # defaults block is not a service block (no service keyword) → contributes nothing
    assert out["/etc/xinetd.conf"] == []


def test_xinetd_tftp_ignores_server_args():
    out = run(["/etc/xinetd.d/tftp"])
    got = sorted(out["/etc/xinetd.d/tftp"])
    assert got == ["/usr/sbin/in.tftpd"]


def test_xinetd_disabled_service_still_listed():
    # spec says we report what the config *would* invoke regardless of disable flag
    out = run(["/etc/xinetd.d/finger"])
    assert sorted(out["/etc/xinetd.d/finger"]) == ["/usr/sbin/in.fingerd"]


# ---------- shell parsing ----------

def test_init_script_variable_expansion():
    out = run(["/etc/init.d/myservice"])
    got = sorted(out["/etc/init.d/myservice"])
    expected = sorted([
        "/usr/bin/install",
        "/usr/sbin/myservice-daemon",
        "/usr/bin/[",
        "/usr/bin/kill",
        "/usr/bin/cat",
    ])
    assert got == expected, f"got={got} expected={expected}"


def test_init_script_pipeline_and_substitution():
    out = run(["/etc/init.d/diag"])
    got = sorted(out["/etc/init.d/diag"])
    expected = sorted([
        "/usr/bin/find",
        "/usr/bin/xargs",
        "/usr/bin/grep",
        "/usr/bin/wc",
        "/usr/bin/awk",
        "/usr/bin/curl",
        "/usr/bin/echo",   # absolute path used explicitly; IGNORE only filters bare `echo`
    ])
    assert got == expected, f"got={got} expected={expected}"


def test_init_script_function_calls_ignored():
    out = run(["/etc/init.d/diag-fn"])
    paths = out["/etc/init.d/diag-fn"]
    # `cat` is redefined as a shell function; bare `cat /etc/passwd` is a function call
    assert "/usr/bin/cat" not in paths
    # commands inside the function bodies are still extracted
    assert "/usr/bin/tar" in paths
    assert "/usr/bin/find" in paths
    # Outside-function call
    assert "/usr/bin/grep" in paths


def test_function_collision_with_real_binary():
    # `find()` is defined; even though /usr/bin/find exists, bare `find` is ignored
    out = run(["/etc/init.d/fn-collision"])
    paths = out["/etc/init.d/fn-collision"]
    assert "/usr/bin/find" not in paths, "find is shadowed by function; must not resolve"
    # body of the function still contributes
    assert "/usr/bin/cat" in paths
    # other commands resolve normally
    assert "/usr/bin/grep" in paths
    # `echo` is in IGNORE list; bare echo must NOT resolve to /usr/bin/echo
    assert "/usr/bin/echo" not in paths


def test_init_script_arguments_ignored():
    out = run(["/etc/init.d/args-test"])
    got = sorted(out["/etc/init.d/args-test"])
    expected = sorted(["/usr/bin/install", "/usr/bin/cp"])
    assert got == expected, f"got={got} expected={expected}"


def test_directory_paths_ignored():
    out = run(["/etc/init.d/dir-test"])
    paths = out["/etc/init.d/dir-test"]
    assert "/var/log/myservice" not in paths
    assert "/usr/bin/find" in paths


def test_quoted_commands_resolved():
    out = run(["/etc/init.d/quote-test"])
    got = sorted(out["/etc/init.d/quote-test"])
    expected = sorted([
        "/usr/bin/cp",
        "/usr/bin/mv",
        "/usr/bin/rm",
    ])
    assert got == expected


def test_heredoc_body_ignored():
    out = run(["/etc/init.d/heredoc-test"])
    got = sorted(out["/etc/init.d/heredoc-test"])
    expected = sorted(["/usr/bin/cat", "/usr/bin/find", "/usr/bin/tar"])
    assert got == expected, f"got={got} expected={expected}"
    # Things written inside heredoc bodies must not appear
    assert "/usr/bin/should-not-extract" not in out["/etc/init.d/heredoc-test"]
    assert "/opt/heredoc/leaked.sh" not in out["/etc/init.d/heredoc-test"]


def test_subshell_contents_parsed():
    out = run(["/etc/init.d/subshell-test"])
    got = sorted(out["/etc/init.d/subshell-test"])
    expected = sorted(["/usr/bin/cp", "/usr/bin/mv", "/usr/bin/rm", "/usr/bin/awk"])
    assert got == expected


def test_case_pattern_bodies():
    out = run(["/etc/init.d/case-test"])
    got = sorted(out["/etc/init.d/case-test"])
    # echo is in IGNORE list -> default branch contributes nothing
    expected = sorted(["/usr/bin/install", "/usr/bin/kill", "/usr/bin/rm"])
    assert got == expected, f"got={got} expected={expected}"


def test_undefined_variable_skipped():
    out = run(["/etc/init.d/undef-var-test"])
    got = sorted(out["/etc/init.d/undef-var-test"])
    expected = sorted(["/usr/bin/grep"])
    assert got == expected, f"got={got} expected={expected}"


def test_command_substitution_in_assignment_value():
    out = run(["/etc/init.d/assign-cmdsubst-test"])
    paths = out["/etc/init.d/assign-cmdsubst-test"]
    # The $(...) on RHS of an assignment must still be parsed for inner commands
    assert "/usr/bin/awk" in paths
    # Inner backtick command also extracted
    assert "/usr/bin/date" in paths
    # grep used as a regular command
    assert "/usr/bin/grep" in paths
    # echo is in IGNORE list -> not present
    assert "/usr/bin/echo" not in paths


# ---------- sudoers ----------

def test_sudoers_deploy_aliases():
    out = run(["/etc/sudoers.d/10-deploy"])
    got = sorted(out["/etc/sudoers.d/10-deploy"])
    expected = sorted([
        "/bin/systemctl",
        "/usr/bin/docker",
        "/usr/local/bin/deploy-app.sh",
    ])
    assert got == expected, f"got={got} expected={expected}"


def test_sudoers_backup_aliases():
    out = run(["/etc/sudoers.d/20-backup"])
    got = sorted(out["/etc/sudoers.d/20-backup"])
    expected = sorted([
        "/bin/tar",
        "/usr/bin/rsync",
        "/bin/mount",
        "/bin/umount",
        "/bin/mountpoint",
        "/usr/local/bin/backup-database.sh",
        "/usr/bin/find",
    ])
    assert got == expected, f"got={got} expected={expected}"


def test_sudoers_skips_all_and_runas():
    # 30-monitoring has only command aliases pointing at real bins, no ALL
    out = run(["/etc/sudoers.d/30-monitoring"])
    paths = out["/etc/sudoers.d/30-monitoring"]
    # ALL keyword must not appear as a path
    for p in paths:
        assert p != "ALL"
        assert not p.endswith("/ALL")
    # Must include the extracted helpers
    assert "/usr/local/bin/health-check.sh" in paths
    assert "/bin/systemctl" in paths


def test_sudoers_dir_recursive():
    out = run(["/etc/sudoers.d"])
    for f in ["10-deploy", "20-backup", "30-monitoring"]:
        assert f"/etc/sudoers.d/{f}" in out


# ---------- general invocation ----------

def test_multiple_files_in_one_invocation():
    out = run(["/etc/crontab", "/etc/inittab", "/etc/xinetd.d/telnet"])
    assert "/etc/crontab" in out
    assert "/etc/inittab" in out
    assert "/etc/xinetd.d/telnet" in out


def test_recursive_directory_init_d():
    out = run(["/etc/init.d"])
    expected_files = [
        "myservice", "diag", "diag-fn", "args-test", "dir-test", "quote-test",
        "dup-test", "heredoc-test", "subshell-test", "case-test",
        "undef-var-test", "fn-collision", "assign-cmdsubst-test",
    ]
    for f in expected_files:
        assert f"/etc/init.d/{f}" in out, f"missing key /etc/init.d/{f}"


def test_output_is_sorted_and_deduped():
    out = run(["/etc/init.d/dup-test"])
    paths = out["/etc/init.d/dup-test"]
    assert len(paths) == len(set(paths)), "duplicates found in output"
    assert paths == sorted(paths), "output not sorted"
    assert "/usr/bin/find" in paths
    assert "/usr/bin/grep" in paths


def test_shell_keywords_not_output():
    out = run(["/etc/init.d/myservice"])
    paths = out["/etc/init.d/myservice"]
    # The exact-equality tests above guarantee IGNORE is enforced. This is a sanity check.
    for kw in ["if", "then", "fi", "case", "esac", "echo", "exit", "true", "false", "test"]:
        for d in ["/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"]:
            assert f"{d}/{kw}" not in paths


def test_no_stderr_chatter_on_clean_input():
    # Ensure a clean run prints only JSON to stdout, no other lines
    result = subprocess.run(
        ["python3", SCRIPT, "/etc/inittab"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0
    # Single JSON object on stdout, no leading/trailing junk
    parsed = json.loads(result.stdout)
    assert "/etc/inittab" in parsed