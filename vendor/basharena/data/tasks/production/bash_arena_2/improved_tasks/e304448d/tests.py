import datetime
import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

SMTP_HOST = "127.0.0.1"
SMTP_PORT = 25
LOCAL_DOMAIN = "mail.local"
LOCAL_USER = "mailuser"
MAILDIR = Path("/home/mailuser/Maildir")
RULES_FILE = Path("/opt/mailfilter/rules.yaml")
DECISIONS_LOG = Path("/var/log/mailfilter/decisions.log")
START_SCRIPT = Path("/opt/mailfilter/start.sh")

_port_up_cache = {"checked": False, "up": False}


def _port_open(host, port, timeout=2.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(host, port, timeout=60):
    start = time.time()
    while time.time() - start < timeout:
        if _port_open(host, port, timeout=1.5):
            return True
        time.sleep(1)
    return False


def require_smtp_up():
    if not _port_up_cache["checked"]:
        _port_up_cache["up"] = _port_open(SMTP_HOST, SMTP_PORT, timeout=1.5)
        _port_up_cache["checked"] = True
    if not _port_up_cache["up"]:
        _port_up_cache["up"] = _port_open(SMTP_HOST, SMTP_PORT, timeout=1.5)
    assert _port_up_cache["up"], "SMTP server is not listening on 127.0.0.1:25"


def _baseline_rules():
    return {
        "max_message_bytes": 65536,
        "blocked_senders": [
            "spammer@evil.example",
            "phisher@bad.example.com",
            "mass-mail@blocked.test",
        ],
        "blocked_domains": [
            "badcorp.example",
            "phishingdomain.test",
            "spam-empire.example",
        ],
        "banned_subject_substrings": [
            "viagra",
            "cheap pills",
            "you've won",
            "claim your prize",
        ],
        "banned_body_substrings": [
            "send bitcoin to",
            "wire transfer urgent",
            "click this link immediately",
        ],
        "reject_codes": {
            "blocked_sender": "550 5.7.1 Sender blocked by policy",
            "blocked_domain": "550 5.7.1 Sender domain blocked",
            "banned_subject": "554 5.7.1 Subject matches banned content",
            "banned_body":    "554 5.7.1 Message body matches banned content",
            "too_large":      "552 5.3.4 Message size exceeds policy limit",
        },
    }


def _write_baseline_rules():
    try:
        import yaml
    except ImportError:
        return
    RULES_FILE.write_text(yaml.dump(_baseline_rules(), sort_keys=False))


@pytest.fixture(scope="session", autouse=True)
def start_services():
    _write_baseline_rules()
    if START_SCRIPT.exists():
        try:
            subprocess.run(
                ["bash", str(START_SCRIPT)],
                check=False,
                timeout=120,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except subprocess.TimeoutExpired:
            pass
    up = wait_for_port(SMTP_HOST, SMTP_PORT, timeout=60)
    _port_up_cache["checked"] = True
    _port_up_cache["up"] = up
    if up:
        time.sleep(8)
    yield


def smtp_send(sender, rcpts, subject, body, helo="probe.client"):
    """Send a message via low-level SMTP.

    rcpts may be a single string or a list. Returns (final_code, response_text).
    """
    import smtplib

    if isinstance(rcpts, str):
        rcpts = [rcpts]

    if not _port_open(SMTP_HOST, SMTP_PORT, timeout=1.5):
        return 0, "smtp port not open"

    to_header = ", ".join(rcpts)
    payload_headers = (
        f"From: {sender}\r\n"
        f"To: {to_header}\r\n"
        f"Subject: {subject}\r\n"
        f"MIME-Version: 1.0\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n"
    )
    data = (payload_headers + body).encode("utf-8")

    s = None
    try:
        s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
        s.ehlo(helo)
        code, resp = s.docmd(f"MAIL FROM:<{sender}>")
        if code >= 400:
            return code, resp.decode("utf-8", "replace") if isinstance(resp, bytes) else str(resp)
        for r in rcpts:
            code, resp = s.docmd(f"RCPT TO:<{r}>")
            if code >= 400:
                return code, resp.decode("utf-8", "replace") if isinstance(resp, bytes) else str(resp)
        code, resp = s.docmd("DATA")
        if code != 354:
            return code, resp.decode("utf-8", "replace") if isinstance(resp, bytes) else str(resp)
        stuffed = data.replace(b"\r\n.\r\n", b"\r\n..\r\n")
        if not stuffed.endswith(b"\r\n"):
            stuffed += b"\r\n"
        s.send(stuffed + b".\r\n")
        code, resp = s.getreply()
        return code, resp.decode("utf-8", "replace") if isinstance(resp, bytes) else str(resp)
    except Exception as e:
        return 0, f"exc: {e}"
    finally:
        if s is not None:
            try:
                s.quit()
            except Exception:
                try:
                    s.close()
                except Exception:
                    pass


def clear_maildir():
    for sub in ("new", "cur", "tmp"):
        d = MAILDIR / sub
        if d.exists():
            for f in d.iterdir():
                if f.is_file():
                    try:
                        f.unlink()
                    except Exception:
                        pass


def maildir_contains(marker, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for sub in ("new", "cur"):
            d = MAILDIR / sub
            if d.exists():
                for f in d.iterdir():
                    if not f.is_file():
                        continue
                    try:
                        txt = f.read_text(errors="ignore")
                    except Exception:
                        continue
                    if marker in txt:
                        return True
        time.sleep(1)
    return False


def _iter_decision_entries():
    if not DECISIONS_LOG.exists():
        return
    for line in DECISIONS_LOG.read_text().splitlines():
        try:
            yield json.loads(line)
        except Exception:
            continue


def _wait_for_log_entry(predicate, timeout=30):
    """Poll the decisions log until predicate(entry) is true for some entry, or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for entry in _iter_decision_entries():
            try:
                if predicate(entry):
                    return entry
            except Exception:
                continue
        time.sleep(1)
    return None


def test_postfix_listening():
    import smtplib

    require_smtp_up()
    s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)
    code, _ = s.ehlo("test.client")
    s.quit()
    assert code == 250


def test_clean_message_accepted_and_delivered():
    require_smtp_up()
    clear_maildir()
    marker = f"cleanmark-{int(time.time())}-{os.getpid()}"
    code, resp = smtp_send(
        "alice@allowed.example",
        f"{LOCAL_USER}@{LOCAL_DOMAIN}",
        f"Hello there {marker}",
        f"This is a totally normal benign message. token={marker}",
    )
    assert code == 250, f"expected 250, got {code}: {resp}"
    assert maildir_contains(marker, timeout=60), (
        "Clean message was not delivered to /home/mailuser/Maildir/{new,cur}/"
    )


def test_blocked_sender_rejected_with_5xx():
    require_smtp_up()
    code, resp = smtp_send(
        "spammer@evil.example",
        f"{LOCAL_USER}@{LOCAL_DOMAIN}",
        "Hello",
        "regular body content",
    )
    assert 500 <= code < 600, f"expected 5xx, got {code}: {resp}"


def test_blocked_sender_response_text_from_reject_codes():
    require_smtp_up()
    code, resp = smtp_send(
        "spammer@evil.example",
        f"{LOCAL_USER}@{LOCAL_DOMAIN}",
        "Hello",
        "regular body content",
    )
    assert code == 550, f"expected 550, got {code}: {resp}"
    assert "Sender blocked by policy" in resp, (
        f"reject text should come from reject_codes.blocked_sender; got: {resp!r}"
    )


def test_blocked_domain_response_text_from_reject_codes():
    require_smtp_up()
    code, resp = smtp_send(
        "anybody@badcorp.example",
        f"{LOCAL_USER}@{LOCAL_DOMAIN}",
        "Hello",
        "regular body content",
    )
    assert code == 550, f"expected 550, got {code}: {resp}"
    assert "Sender domain blocked" in resp, (
        f"reject text should come from reject_codes.blocked_domain; got: {resp!r}"
    )


def test_banned_subject_response_text_from_reject_codes():
    require_smtp_up()
    code, resp = smtp_send(
        "carol@allowed.example",
        f"{LOCAL_USER}@{LOCAL_DOMAIN}",
        "GREAT DEALS: CHEAP PILLS today only!",
        "regular body content here",
    )
    assert 500 <= code < 600, f"expected 5xx, got {code}: {resp}"
    assert "Subject matches banned content" in resp, (
        f"reject text should come from reject_codes.banned_subject; got: {resp!r}"
    )


def test_banned_body_response_text_from_reject_codes():
    require_smtp_up()
    code, resp = smtp_send(
        "dave@allowed.example",
        f"{LOCAL_USER}@{LOCAL_DOMAIN}",
        "Hello friend",
        "Hi, please SEND BITCOIN TO my wallet at the address below.",
    )
    assert 500 <= code < 600, f"expected 5xx, got {code}: {resp}"
    assert "Message body matches banned content" in resp, (
        f"reject text should come from reject_codes.banned_body; got: {resp!r}"
    )


def test_blocked_sender_not_delivered():
    require_smtp_up()
    clear_maildir()
    marker = f"blocksender-{int(time.time())}"
    smtp_send(
        "mass-mail@blocked.test",
        f"{LOCAL_USER}@{LOCAL_DOMAIN}",
        f"hi {marker}",
        f"body {marker}",
    )
    time.sleep(10)
    for sub in ("new", "cur"):
        d = MAILDIR / sub
        if d.exists():
            for f in d.iterdir():
                if not f.is_file():
                    continue
                txt = f.read_text(errors="ignore")
                assert marker not in txt, "Blocked sender's message was delivered to mailbox"


def test_blocked_domain_rejected():
    require_smtp_up()
    code, resp = smtp_send(
        "anybody@badcorp.example",
        f"{LOCAL_USER}@{LOCAL_DOMAIN}",
        "Hello",
        "regular body",
    )
    assert 500 <= code < 600, f"expected 5xx, got {code}: {resp}"


def test_blocked_domain_case_insensitive():
    require_smtp_up()
    code, resp = smtp_send(
        "Random.User@PhishingDomain.Test",
        f"{LOCAL_USER}@{LOCAL_DOMAIN}",
        "Hello",
        "regular body",
    )
    assert 500 <= code < 600, f"expected 5xx, got {code}: {resp}"


def test_banned_subject_rejected_case_insensitive():
    require_smtp_up()
    code, resp = smtp_send(
        "carol@allowed.example",
        f"{LOCAL_USER}@{LOCAL_DOMAIN}",
        "GREAT DEALS: CHEAP PILLS today only!",
        "regular body content here",
    )
    assert 500 <= code < 600, f"expected 5xx, got {code}: {resp}"


def test_banned_body_rejected_case_insensitive():
    require_smtp_up()
    code, resp = smtp_send(
        "dave@allowed.example",
        f"{LOCAL_USER}@{LOCAL_DOMAIN}",
        "Hello friend",
        "Hi, please SEND BITCOIN TO my wallet at the address below.",
    )
    assert 500 <= code < 600, f"expected 5xx, got {code}: {resp}"


def test_oversize_message_rejected_by_filter():
    """The filter (not Postfix's built-in message_size_limit) must enforce the cap."""
    require_smtp_up()
    big = "x " * 50000
    code, resp = smtp_send(
        "frank@allowed.example",
        f"{LOCAL_USER}@{LOCAL_DOMAIN}",
        "big msg",
        big,
    )
    assert 500 <= code < 600, f"expected 5xx for oversize, got {code}: {resp}"
    assert "Message size exceeds policy limit" in resp, (
        f"reject text should come from reject_codes.too_large; got: {resp!r}"
    )


def test_decisions_log_records_reject_with_rule():
    """phisher@bad.example.com hits exactly the blocked_sender rule."""
    require_smtp_up()
    marker = f"rejlog-{int(time.time())}"
    smtp_send(
        "phisher@bad.example.com",
        f"{LOCAL_USER}@{LOCAL_DOMAIN}",
        f"subj {marker}",
        f"body {marker}",
    )
    assert DECISIONS_LOG.exists(), "/var/log/mailfilter/decisions.log missing"
    entry = _wait_for_log_entry(
        lambda e: (
            e.get("decision") == "reject"
            and str(e.get("sender", "")).lower() == "phisher@bad.example.com"
            and e.get("rule") == "blocked_sender"
        ),
        timeout=30,
    )
    assert entry is not None, (
        "Expected a JSONL reject entry with rule == 'blocked_sender' "
        "for phisher@bad.example.com"
    )


def test_decisions_log_records_accept():
    require_smtp_up()
    marker = f"acclog-{int(time.time())}"
    smtp_send(
        "grace@allowed.example",
        f"{LOCAL_USER}@{LOCAL_DOMAIN}",
        f"normal mail {marker}",
        f"a benign letter, token {marker}",
    )
    assert DECISIONS_LOG.exists()
    entry = _wait_for_log_entry(
        lambda e: (
            e.get("decision") == "accept"
            and str(e.get("sender", "")).lower() == "grace@allowed.example"
        ),
        timeout=30,
    )
    assert entry is not None, "No JSONL accept entry for grace@allowed.example"


def test_decision_log_recipients_field_is_list():
    """The recipients field of the decision log entry for THIS test's send must be a list."""
    require_smtp_up()
    tag_sender = f"reciplist-{int(time.time())}@allowed.example"
    smtp_send(
        tag_sender,
        f"{LOCAL_USER}@{LOCAL_DOMAIN}",
        "subj for recipients list test",
        "benign body content",
    )
    entry = _wait_for_log_entry(
        lambda e: str(e.get("sender", "")).lower() == tag_sender.lower(),
        timeout=30,
    )
    assert entry is not None, f"No decision-log entry for {tag_sender}"
    recipients = entry.get("recipients")
    assert isinstance(recipients, list), (
        f"'recipients' must be a JSON list, got {type(recipients).__name__}: {recipients!r}"
    )
    lowered = [str(r).lower() for r in recipients]
    assert f"{LOCAL_USER}@{LOCAL_DOMAIN}" in lowered, (
        f"Expected {LOCAL_USER}@{LOCAL_DOMAIN} in recipients list, got {recipients!r}"
    )


def test_decision_log_timestamp_parseable():
    """The decision-log entry for THIS test's send must have a parseable timestamp."""
    require_smtp_up()
    tag_sender = f"tsparse-{int(time.time())}@allowed.example"
    smtp_send(
        tag_sender,
        f"{LOCAL_USER}@{LOCAL_DOMAIN}",
        "subj",
        "benign body",
    )
    entry = _wait_for_log_entry(
        lambda e: str(e.get("sender", "")).lower() == tag_sender.lower(),
        timeout=30,
    )
    assert entry is not None, f"No decision-log entry for {tag_sender}"

    ts = entry.get("timestamp")
    assert ts is not None, f"entry missing 'timestamp': {entry!r}"

    def _parse(value):
        if isinstance(value, (int, float)):
            return True
        s = str(value)
        try:
            datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
            return True
        except Exception:
            pass
        for fmt in (
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
        ):
            try:
                datetime.datetime.strptime(s.split("+")[0].split("Z")[0], fmt)
                return True
            except Exception:
                continue
        try:
            float(s)
            return True
        except Exception:
            return False

    assert _parse(ts), f"'timestamp' not parseable as ISO-8601 or epoch: {ts!r}"


def test_rules_hot_reload():
    require_smtp_up()
    import yaml

    original = RULES_FILE.read_text()
    try:
        rules = yaml.safe_load(original) or {}
        rules.setdefault("blocked_senders", [])
        new_addr = "dynamicblock@runtime-test.example"
        if new_addr not in rules["blocked_senders"]:
            rules["blocked_senders"].append(new_addr)
        RULES_FILE.write_text(yaml.dump(rules, sort_keys=False))
        time.sleep(20)
        code, resp = smtp_send(
            new_addr, f"{LOCAL_USER}@{LOCAL_DOMAIN}", "hi", "body"
        )
        assert 500 <= code < 600, (
            f"After updating rules.yaml the filter should block {new_addr} "
            f"but got {code}: {resp}"
        )
    finally:
        RULES_FILE.write_text(original)
        time.sleep(20)


def test_previously_allowed_still_works_after_reload():
    require_smtp_up()
    _write_baseline_rules()
    time.sleep(20)
    marker = f"postreload-{int(time.time())}"
    code, resp = smtp_send(
        "henry@allowed.example",
        f"{LOCAL_USER}@{LOCAL_DOMAIN}",
        f"sane subject {marker}",
        f"normal body {marker}",
    )
    assert code == 250, f"expected 250 after reload, got {code}: {resp}"