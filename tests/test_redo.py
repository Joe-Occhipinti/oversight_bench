"""bench.redo: which finished samples are lost to a provider outage (the marking itself is tested in tests/test_resume.py)."""

from __future__ import annotations

import pytest

from bench import redo

LINE = "meltzer: request to model provider failed: timed out"


@pytest.mark.parametrize(
    "exit_code, stderr, expected",
    [
        (1, LINE, True),
        (1, "I will look.\n" + LINE + "\n", True),
        (1, "meltzer: config error", False),  # another failure
        (0, LINE, False),  # meltzer finished
        (130, LINE, False),  # stopped by us
        (None, LINE, False),
    ],
)
def test_provider_failure_needs_exit_code_1_and_the_line(tmp_path, exit_code, stderr, expected):
    (tmp_path / "stderr.txt").write_text(stderr, encoding="utf-8")
    assert redo.is_provider_failure(tmp_path, exit_code) is expected


def test_a_missing_stderr_file_is_not_a_provider_failure(tmp_path):
    assert redo.is_provider_failure(tmp_path, 1) is False


def test_the_line_is_what_meltzer_prints():
    """meltzer prints `meltzer: <ModelError text>` and `ModelError` starts with this text (client.py)."""
    from pathlib import Path

    client = (Path(redo.__file__).resolve().parent.parent / "meltzer" / "v0.1" / "src" / "meltzer" / "client.py").read_text(encoding="utf-8")
    assert redo.PROVIDER_FAILURE in client
