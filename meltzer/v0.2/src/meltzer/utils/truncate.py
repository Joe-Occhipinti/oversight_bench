"""Shorten long text to its start and end, with a marker saying how much was left out."""

from __future__ import annotations

MAX_OUTPUT_CHARS = 30_000


def truncate_output(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """text when it has at most limit characters, else its first and last limit // 2 characters."""
    if len(text) <= limit:
        return text
    half = limit // 2
    omitted = len(text) - 2 * half
    return f"{text[:half]}\n[... {omitted} characters omitted ...]\n{text[-half:]}"
