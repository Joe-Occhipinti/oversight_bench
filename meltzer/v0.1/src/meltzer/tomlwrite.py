"""Formatting values for the TOML files meltzer writes."""

from __future__ import annotations

import json


def toml_string(value: str) -> str:
    """A TOML basic string. JSON string escapes are valid in TOML; DEL must be escaped as well."""
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
