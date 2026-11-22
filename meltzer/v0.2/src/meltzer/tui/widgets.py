"""Parts of the full-screen interface: transcript, dialog, and the text they show.

Text from the model, tools and files is shown as plain text: escape sequences
and control characters are removed, and nothing is read as markup.
"""

from __future__ import annotations

import json
import shlex
from typing import Any, Callable, Generic, TypeVar

from prompt_toolkit.application import Application
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Frame, TextArea

from meltzer.approval.policy import ApprovalChoice, ApprovalRequest, patch_preview
from meltzer.ui import COMMAND_ARGUMENTS, PROMPT_RULE_HEADING, REQUEST_HEADINGS, plain_text

T = TypeVar("T")

DEFAULT_HEADING = "meltzer wants to run outside the sandbox:"
PATCH_HEADING = "meltzer wants to apply a patch that writes outside the writable paths:"
DIALOG_HINT = "Up/Down choose · Enter confirm · Tab request/choices · Esc refuse"

# Lines moved per PageUp/PageDown in the transcript.
SCROLL_LINES = 10


def request_text(request: ApprovalRequest) -> str:
    """What the approval dialog shows for a request: the same text as the line-based prompt."""
    if request.patch is None:
        if request.tool in COMMAND_ARGUMENTS and not request.escalate:
            heading = PROMPT_RULE_HEADING
        else:
            heading = REQUEST_HEADINGS.get(request.tool, DEFAULT_HEADING)
        lines = [heading, json.dumps(request.planned_action(), indent=2)]
    else:
        lines = [PATCH_HEADING, *(f"  {path}" for path in request.paths), "Patch:"]
        lines.append(patch_preview(request.patch).rstrip("\n"))
    return plain_text("\n".join(lines))


def request_choices(request: ApprovalRequest) -> list[tuple[ApprovalChoice, str]]:
    """The answers offered for a request, as in the line-based prompt; refusing is last."""
    if request.tool in ("request_permissions", "network_access"):
        return [
            (ApprovalChoice.APPROVE_ONCE, "Yes, once" if request.tool == "network_access" else "Yes, for this turn"),
            (ApprovalChoice.APPROVE_FOR_SESSION, "Yes, for this session"),
            (ApprovalChoice.DENY, "No"),
        ]
    choices = [(ApprovalChoice.APPROVE_ONCE, "Yes, once")]
    if request.suggested_prefix:
        prefix = plain_text(shlex.join(request.suggested_prefix))
        choices.append((ApprovalChoice.ALWAYS_ALLOW_PREFIX, f"Yes, and always allow commands starting with `{prefix}`"))
    choices.append((ApprovalChoice.DENY, "No"))
    return choices


class Transcript:
    """The scrolling, read-only pane with the conversation. It follows new text unless scrolled up."""

    def __init__(self) -> None:
        self.area = TextArea(read_only=True, focusable=False, scrollbar=True, wrap_lines=True)

    @property
    def text(self) -> str:
        return self.area.text

    def append(self, text: str) -> None:
        buffer = self.area.buffer
        following = buffer.cursor_position == len(buffer.text)
        updated = buffer.text + plain_text(text) + "\n"
        cursor = len(updated) if following else buffer.cursor_position
        buffer.set_document(Document(updated, cursor), bypass_readonly=True)

    def clear(self) -> None:
        self.area.buffer.set_document(Document(""), bypass_readonly=True)

    def scroll(self, lines: int) -> None:
        """Move the view by lines (negative: up). Reaching the last line follows new text again."""
        buffer = self.area.buffer
        if lines < 0:
            buffer.cursor_up(count=-lines)
            return
        buffer.cursor_down(count=lines)
        if buffer.document.cursor_position_row >= buffer.document.line_count - 1:
            buffer.cursor_position = len(buffer.text)

    def __pt_container__(self) -> TextArea:
        return self.area


class Dialog(Generic[T]):
    """A question with a scrollable text and a list of choices; the last choice is selected first.

    Enter on the choices answers with the selected choice; Esc and Ctrl-C answer `cancel`.
    `on_answer` is called once, in the interface thread.
    """

    def __init__(
        self,
        app: Application[Any],
        title: str,
        body: str,
        choices: list[tuple[T, str]],
        *,
        cancel: T,
        on_answer: Callable[[T], None],
    ):
        self.app = app
        self.choices = choices
        self.selected = len(choices) - 1
        self.cancel = cancel
        self._on_answer = on_answer
        self._answered = False
        self.body = TextArea(text=plain_text(body), read_only=True, scrollbar=True, wrap_lines=True)

        choice_keys = KeyBindings()
        choice_keys.add("up")(lambda event: self._move(-1))
        choice_keys.add("down")(lambda event: self._move(1))
        choice_keys.add("enter")(lambda event: self.answer(self.selected_value))
        self.choice_window = Window(
            FormattedTextControl(self._choice_fragments, focusable=True, key_bindings=choice_keys, show_cursor=False),
            height=Dimension.exact(len(choices)),
            dont_extend_height=True,
        )

        dialog_keys = KeyBindings()
        for key in ("escape", "c-c", "<sigint>"):
            dialog_keys.add(key)(lambda event: self.answer(self.cancel))
        for key in ("tab", "s-tab"):
            dialog_keys.add(key)(self._switch_focus)
        self.container = Frame(
            HSplit(
                [
                    self.body,
                    Window(height=1, char="─", style="class:dialog.separator"),
                    self.choice_window,
                    Window(FormattedTextControl(DIALOG_HINT), height=1, style="class:dialog.hint"),
                ],
                key_bindings=dialog_keys,
            ),
            title=title,
            style="class:dialog",
        )

    @property
    def body_text(self) -> str:
        return self.body.text

    @property
    def selected_value(self) -> T:
        return self.choices[self.selected][0]

    def has_focus(self) -> bool:
        """Whether the choices have the keyboard focus."""
        return self.app.layout.has_focus(self.choice_window)

    def answer(self, value: T) -> None:
        if not self._answered:
            self._answered = True
            self._on_answer(value)

    def _move(self, step: int) -> None:
        self.selected = min(max(self.selected + step, 0), len(self.choices) - 1)

    def _switch_focus(self, event: KeyPressEvent) -> None:
        target = self.body if self.has_focus() else self.choice_window
        event.app.layout.focus(target)

    def _choice_fragments(self) -> StyleAndTextTuples:
        fragments: StyleAndTextTuples = []
        for index, (_, label) in enumerate(self.choices):
            if index == self.selected:
                fragments.append(("class:choice.selected", f"> {label}"))
            else:
                fragments.append(("class:choice", f"  {label}"))
            if index < len(self.choices) - 1:
                fragments.append(("", "\n"))
        return fragments

    def __pt_container__(self) -> Frame:
        return self.container
