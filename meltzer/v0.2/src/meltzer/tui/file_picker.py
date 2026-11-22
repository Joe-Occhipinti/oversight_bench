"""The `@` file picker: typing `@` and part of a path offers matching files of the session root.

Choosing a file replaces `@query` with the path (in double quotes when it has spaces).
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable, Iterable

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document

from meltzer.tui.widgets import plain_text
from meltzer.utils.file_search import list_files as default_list_files
from meltzer.utils.file_search import search

# A file listing is reused for this long before the directory is listed again.
LISTING_MAX_AGE_SECONDS = 30.0

# `@` at the start of the input or after whitespace, followed by the query up to the cursor.
MENTION = re.compile(r"(?:^|\s)@(\S*)$")


def mention_at_cursor(text_before_cursor: str) -> str | None:
    """The query of the `@` mention that ends at the cursor, or None."""
    match = MENTION.search(text_before_cursor)
    return match.group(1) if match else None


class FileCompleter(Completer):
    def __init__(
        self,
        root: Callable[[], Path],
        *,
        list_files: Callable[[Path], list[str]] = default_list_files,
    ):
        self._root = root
        self._list_files = list_files
        self._listing: tuple[Path, float, list[str]] | None = None

    def _files(self) -> list[str]:
        root = self._root()
        now = time.monotonic()
        if self._listing is None or self._listing[0] != root or now - self._listing[1] > LISTING_MAX_AGE_SECONDS:
            self._listing = (root, now, self._list_files(root))
        return self._listing[2]

    def get_completions(self, document: Document, complete_event: CompleteEvent) -> Iterable[Completion]:
        query = mention_at_cursor(document.text_before_cursor)
        if query is None:
            return
        for path in search(query, self._files()):
            text = f'"{path}"' if any(char.isspace() for char in path) else path
            yield Completion(text, start_position=-(len(query) + 1), display=plain_text(path))
