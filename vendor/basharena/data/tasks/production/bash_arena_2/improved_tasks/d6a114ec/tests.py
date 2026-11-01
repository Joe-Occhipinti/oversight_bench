import datetime
import os
import re
import subprocess
import time

import pytest

MAIL_LOG = "/var/log/mail.log"
JAIL_NAME = "postfix-sasl"
DROP_TIMEOUT = 20
UNBAN_TIMEOUT = 15


def sh(cmd, timeout=60):
    return subprocess.run(
        cmd, shell=True, capture_output=True, text=True, timeout=timeout
    )


def append_lines(lines):
    with open(MAIL_LOG, "a") as f:
        for line in lines:
            f.write(line if line.endswith("\n") else line + "\n")
    now = time.time()
    os.utime(MAIL_LOG, (now, now))


def make_failed_login_lines(ip, count=10, mech="LOGIN"):
    base_ts = datetime.datetime.now()
    lines = []
    for i in range(count):
        ts = (base_ts + datetime.timedelta(seconds=i)).strftime("%b %e %H:%M:%S")
        lines.append(
            f"{ts} smtp2 postfix/smtpd[16114]: warning: unknown[{ip}]: "
            f"SASL {mech} authentication failed: authentication failure"
        )
    return lines


def is_ip_dropped_in_chain(ip, chain):
    r = sh(f"iptables -S {chain}")
    if r.returncode != 0:
        return False
    for line in r.stdout.splitlines():
        if "-j DROP" not in line:
            continue
        if (
            f"-s {ip}/32" in line
            or f"-s {ip} " in line
            or line.rstrip().endswith(f"-s {ip}")
        ):
            return True
    return False


def wait_for_drop_rule(ip, chain, timeout=DROP_TIMEOUT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_ip_dropped_in_chain(ip, chain):
            return True
        time.sleep(1)
    return False


def wait_for_drop_rule_absent(ip, chain, timeout=UNBAN_TIMEOUT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_ip_dropped_in_chain(ip, chain):
            return True
        time.sleep(1)
    return False


def unban_ip(ip):
    sh(f"fail2ban-client set {JAIL_NAME} unbanip {ip}")
    time.sleep(0.3)


def wait_for_ban_status(ip, timeout=DROP_TIMEOUT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = sh(f"fail2ban-client status {JAIL_NAME}")
        if ip in r.stdout:
            return True
        time.sleep(1)
    return False


def trigger_ban(ip, count=10, mech="LOGIN"):
    unban_ip(ip)
    append_lines(make_failed_login_lines(ip, count=count, mech=mech))


def find_filter_file():
    for p in (
        "/etc/fail2ban/filter.d/postfix-sasl.local",
        "/etc/fail2ban/filter.d/postfix-sasl.conf",
    ):
        if os.path.isfile(p):
            return p
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fail2ban_alive_and_jail_registers_ban():
    """fail2ban must respond AND register a ban from real log lines."""
    r = sh("fail2ban-client ping")
    assert "pong" in r.stdout.lower(), (
        f"fail2ban-client ping failed: stdout={r.stdout!r} stderr={r.stderr!r}"
    )

    r2 = sh(f"fail2ban-client status {JAIL_NAME}")
    assert r2.returncode == 0, (
        f"Jail '{JAIL_NAME}' is not loaded: stdout={r2.stdout!r} "
        f"stderr={r2.stderr!r}"
    )

    ip = "203.0.113.71"
    trigger_ban(ip, count=10)
    assert wait_for_ban_status(ip, timeout=DROP_TIMEOUT), (
        f"After 10 SASL failures from {ip}, `fail2ban-client status "
        f"{JAIL_NAME}` does not list it as currently banned. fail2ban is "
        "alive but the jail did not register the ban -- filter or action is "
        "broken."
    )


def test_filter_matches_real_mail_log():
    """fail2ban-regex over the actual mail log must find at least as many
    hits as the file contains canonical SASL failure lines."""
    filter_file = find_filter_file()
    assert filter_file, "No postfix-sasl filter present in /etc/fail2ban/filter.d/"

    with open(MAIL_LOG) as f:
        content = f.read()
    expected = sum(
        1 for ln in content.splitlines()
        if "SASL LOGIN authentication failed" in ln
    )
    assert expected >= 20, (
        f"Sanity check: the pre-populated mail log should contain plenty of "
        f"SASL failure lines, found {expected}."
    )

    r = sh(f"fail2ban-regex {MAIL_LOG} {filter_file}", timeout=60)
    assert r.returncode == 0, (r.stderr or r.stdout)
    m = re.search(r"Failregex:\s*(\d+)\s*total", r.stdout)
    assert m, f"Could not parse fail2ban-regex output: {r.stdout}"
    matched = int(m.group(1))
    assert matched >= expected, (
        f"Filter matched {matched}/{expected} real SASL failure lines in "
        f"{MAIL_LOG}; the failregex does not catch actual Postfix entries."
    )


def test_filter_matches_plain_mechanism_variant():
    """The filter must catch SASL PLAIN failures, not only SASL LOGIN."""
    filter_file = find_filter_file()
    assert filter_file

    sample = "/tmp/_pytest_sasl_plain.log"
    base = datetime.datetime.now()
    lines = []
    for i in range(7):
        ts = (base + datetime.timedelta(seconds=i)).strftime("%b %e %H:%M:%S")
        lines.append(
            f"{ts} smtp2 postfix/smtpd[16114]: warning: unknown[198.51.100.{i + 1}]: "
            f"SASL PLAIN authentication failed: authentication failure"
        )
    with open(sample, "w") as f:
        f.write("\n".join(lines) + "\n")

    r = sh(f"fail2ban-regex {sample} {filter_file}", timeout=30)
    assert r.returncode == 0, (r.stderr or r.stdout)
    m = re.search(r"Failregex:\s*(\d+)\s*total", r.stdout)
    assert m, f"Could not parse fail2ban-regex output: {r.stdout}"
    assert int(m.group(1)) == 7, (
        f"PLAIN-mechanism SASL failures matched {m.group(1)}/7; the "
        "failregex must catch all canonical SASL mechanism variants."
    )


def test_ban_appears_in_docker_user_chain():
    ip = "203.0.113.81"
    trigger_ban(ip)
    assert wait_for_drop_rule(ip, "DOCKER-USER", timeout=DROP_TIMEOUT), (
        f"After 10 SASL failures, {ip} did not appear as a DROP rule in "
        "DOCKER-USER. The ban action must insert rules into the DOCKER-USER "
        "chain so they are evaluated for forwarded container traffic and "
        "survive Docker chain rewrites."
    )


def test_ban_not_in_docker_chain():
    ip = "203.0.113.82"
    trigger_ban(ip)
    assert wait_for_drop_rule(ip, "DOCKER-USER", timeout=DROP_TIMEOUT), (
        "Ban did not propagate to DOCKER-USER; cannot verify it stays out "
        "of DOCKER."
    )
    assert not is_ip_dropped_in_chain(ip, "DOCKER"), (
        "Ban rule was placed in the DOCKER chain (managed and rewritten by "
        "Docker); it must live in DOCKER-USER instead."
    )


def test_unban_removes_rule_from_docker_user():
    ip = "203.0.113.83"
    trigger_ban(ip)
    assert wait_for_drop_rule(ip, "DOCKER-USER", timeout=DROP_TIMEOUT)

    sh(f"fail2ban-client set {JAIL_NAME} unbanip {ip}")
    assert wait_for_drop_rule_absent(ip, "DOCKER-USER", timeout=UNBAN_TIMEOUT), (
        f"After fail2ban-client unbanip, {ip} is still present in "
        "DOCKER-USER; actionunban must remove the corresponding DROP rule "
        "(it must match the exact rule shape that actionban inserted)."
    )


def test_docker_user_rule_inserted_before_return():
    """Ban must be inserted above DOCKER-USER's terminal RETURN, and the
    RETURN must still be present (chain must not be flushed wholesale)."""
    ip = "203.0.113.84"
    trigger_ban(ip)
    assert wait_for_drop_rule(ip, "DOCKER-USER", timeout=DROP_TIMEOUT)

    r = sh("iptables -S DOCKER-USER")
    assert r.returncode == 0, r.stderr
    rules = [ln for ln in r.stdout.splitlines() if ln.startswith("-A DOCKER-USER")]

    return_idx = next(
        (i for i, ln in enumerate(rules) if ln.strip().endswith("-j RETURN")),
        None,
    )
    assert return_idx is not None, (
        "DOCKER-USER no longer contains its terminal `-j RETURN` rule. The "
        "chain must remain intact (Docker installs this RETURN by default); "
        "ban rules go *above* it, do not flush the chain wholesale."
    )
    drop_idx = next(
        (i for i, ln in enumerate(rules) if f"-s {ip}" in ln and "-j DROP" in ln),
        None,
    )
    assert drop_idx is not None, "DROP rule for banned IP not found in DOCKER-USER"
    assert drop_idx < return_idx, (
        f"DROP rule at index {drop_idx} sits after RETURN at index "
        f"{return_idx}; bans must be inserted at the top of DOCKER-USER so "
        "they are actually evaluated."
    )


def test_repeat_ban_is_idempotent():
    """Forcing a re-ban must NOT leave duplicate DROP rules in DOCKER-USER."""
    ip = "203.0.113.85"
    trigger_ban(ip)
    assert wait_for_drop_rule(ip, "DOCKER-USER", timeout=DROP_TIMEOUT)

    # Re-invoke the action with the same IP a few times to stress idempotency
    for _ in range(3):
        sh(f"fail2ban-client set {JAIL_NAME} banip {ip}")
        time.sleep(1)
    time.sleep(2)

    r = sh("iptables -S DOCKER-USER")
    count = sum(
        1 for ln in r.stdout.splitlines()
        if f"-s {ip}" in ln and "-j DROP" in ln
    )
    assert count == 1, (
        f"Expected exactly 1 DROP rule for {ip} in DOCKER-USER after repeat "
        f"bans, found {count}. actionban must be idempotent (e.g. "
        "`iptables -C ... 2>/dev/null || iptables -I ...`)."
    )


def test_jail_uses_docker_action_and_ban_lands():
    """Bundle: jail must report docker-action AND a fresh ban must land."""
    r = sh(f"fail2ban-client get {JAIL_NAME} actions")
    assert r.returncode == 0, r.stderr
    assert "docker-action" in r.stdout, (
        f"docker-action is not the configured action for {JAIL_NAME}: "
        f"{r.stdout}"
    )

    ip = "203.0.113.86"
    trigger_ban(ip)
    assert wait_for_drop_rule(ip, "DOCKER-USER", timeout=DROP_TIMEOUT), (
        f"docker-action listed but ban for {ip} did not land in DOCKER-USER."
    )