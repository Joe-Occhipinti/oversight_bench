"""Full-screen terminal interface for interactive sessions (`[tui] enabled = true`).

The interface runs in the calling thread; the session runs in a worker thread
and calls the same methods as meltzer.ui.TerminalUI. Prompts, approvals and
trust questions block the worker until the user answers in the interface.
"""

from __future__ import annotations

import asyncio
import ctypes
import io
import queue
import sys
import threading
from pathlib import Path
from typing import Any, Callable, TypeVar

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import ThreadedCompleter, merge_completers
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import Float, FloatContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

from meltzer import __version__
from meltzer.approval.policy import ApprovalChoice, ApprovalRequest
from meltzer.tools.request_user_input import Question
from meltzer.tui.file_picker import FileCompleter
from meltzer.tui.slash import SlashCompleter, help_text, local_command, status_report
from meltzer.tui.widgets import SCROLL_LINES, Dialog, Transcript, request_choices, request_text
from meltzer.ui import plain_text, tool_call_text

T = TypeVar("T")

# The choice that lets the user type their own answer to a question.
OTHER = object()

STYLE = Style.from_dict(
    {
        "separator": "fg:ansibrightblack",
        "status": "reverse",
        "dialog": "bg:ansiblack fg:ansiwhite",
        "dialog.hint": "fg:ansibrightblack",
        "choice.selected": "reverse bold",
    }
)


def _raise_in_thread(thread: threading.Thread, exc_type: type[BaseException]) -> None:
    """Raise exc_type in thread when it next runs Python code (as Ctrl-C does in the main thread)."""
    if thread.ident is not None:
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread.ident), ctypes.py_object(exc_type))


class _ErrorStream(io.TextIOBase):
    """Stands in for sys.stderr while the interface runs: lines go to the transcript and are kept."""

    def __init__(self, show_line: Callable[[str], None]):
        self._show_line = show_line
        self._parts: list[str] = []
        self._line = ""
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        with self._lock:
            self._parts.append(text)
            *lines, self._line = (self._line + text).split("\n")
        for line in lines:
            self._show_line(line)
        return len(text)

    def getvalue(self) -> str:
        with self._lock:
            return "".join(self._parts)


class FullScreenUI:
    """Transcript, input box and status line; dialogs for approvals and trust questions."""

    interactive = True

    def __init__(self, *, input: Input | None = None, output: Output | None = None):
        self.transcript = Transcript()
        self.dialog: Dialog[Any] | None = None
        # Set once run() has started the worker.
        self.started = False
        self._session: Any | None = None
        self._root = Path.cwd()
        self._prompts: queue.Queue[str | None] = queue.Queue()
        # While a question waits for a typed answer, the next input goes to this queue instead of _prompts.
        self._answers: queue.Queue[str | None] = queue.Queue()
        self._answering = False
        self._waiting = False
        self._state_lock = threading.Lock()
        # One dialog at a time; the answer callback and value of the open one, for closing.
        self._dialog_lock = threading.Lock()
        self._pending: tuple[Callable[[Any], None], Any] | None = None
        self._closed = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._worker: threading.Thread | None = None
        self._errors = _ErrorStream(lambda line: self._post(lambda: self.transcript.append(line)))

        no_dialog = Condition(lambda: self.dialog is None)
        completer = merge_completers([SlashCompleter(), FileCompleter(lambda: self._root)])
        self.input = TextArea(
            prompt="> ",
            multiline=False,
            completer=ThreadedCompleter(completer),
            complete_while_typing=True,
            focusable=no_dialog,
            accept_handler=self._accept,
        )
        body = HSplit(
            [
                self.transcript,
                Window(height=1, char="─", style="class:separator"),
                self.input,
                Window(FormattedTextControl(self._status_fragments), height=1, style="class:status"),
            ]
        )
        self._container = FloatContainer(
            body, floats=[Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=10, scroll_offset=1))]
        )
        self.app: Application[None] = Application(
            layout=Layout(self._container, focused_element=self.input),
            key_bindings=self._key_bindings(no_dialog),
            style=STYLE,
            full_screen=True,
            mouse_support=False,
            input=input,
            output=output,
        )

    # --- running -------------------------------------------------------------------

    def run(self, worker: Callable[[], int]) -> int:
        """Show the interface while worker runs in its own thread; return its result.

        The interface closes when worker returns. An exception from worker is
        raised again here, after the interface has closed. Text written to
        sys.stderr meanwhile is shown in the transcript and printed after closing.
        """
        outcome: dict[str, Any] = {}

        def target() -> None:
            try:
                outcome["result"] = worker()
            except BaseException as exc:
                outcome["error"] = exc
            finally:
                self._post(self._exit)

        self._worker = threading.Thread(target=target, name="meltzer-session", daemon=True)

        def pre_run() -> None:
            self._loop = asyncio.get_running_loop()
            self.started = True
            self._worker.start()

        stderr = sys.stderr
        sys.stderr = self._errors
        finished = False
        try:
            self.app.run(pre_run=pre_run)
            finished = True
        finally:
            self._close()
            if self.started:
                if not finished and self._worker.is_alive():
                    _raise_in_thread(self._worker, KeyboardInterrupt)
                self._worker.join()
            sys.stderr = stderr
            text = self._errors.getvalue()
            if text:
                stderr.write(plain_text(text))
                stderr.flush()
        if "error" in outcome:
            raise outcome["error"]
        return outcome.get("result", 0)

    def _exit(self) -> None:
        if self.app.is_running and not self.app.future.done():
            self.app.exit()

    def _close(self) -> None:
        with self._state_lock:
            self._closed.set()
            self._loop = None
            pending, self._pending = self._pending, None
        self._prompts.put(None)
        self._answers.put(None)
        if pending is not None:
            answer, value = pending
            answer(value)

    def _post(self, callback: Callable[[], None]) -> bool:
        """Run callback in the interface thread. False when the interface is closed."""
        loop = self._loop
        if loop is None or self._closed.is_set():
            return False
        try:
            loop.call_soon_threadsafe(callback)
        except RuntimeError:
            return False
        return True

    def attach_session(self, session: Any) -> Any:
        """Show this session's model, sandbox mode and cwd; complete files under its root."""
        self._session = session
        self._root = session.root
        self._post(self.app.invalidate)
        return session

    # --- methods the session and cli call (worker thread) -----------------------------

    def info(self, message: str) -> None:
        self._show(message)

    def assistant_message(self, text: str) -> None:
        self._show(text)

    def tool_call(self, name: str, arguments: str) -> None:
        self._show(tool_call_text(name, arguments))

    def _show(self, text: str) -> None:
        if not self._post(lambda: self.transcript.append(text)):
            print(plain_text(text), file=sys.stderr, flush=True)

    @property
    def waiting_for_prompt(self) -> bool:
        return self._waiting

    def read_prompt(self) -> str | None:
        """Next prompt typed in the input box, or None when the user ends the session."""
        if self._closed.is_set():
            return None
        self._set_waiting(True)
        try:
            while True:
                text = self._prompts.get()
                if text is None or text.strip():
                    return text
        finally:
            self._set_waiting(False)

    def _read_answer(self) -> str | None:
        """The next input typed in the input box, as the answer to a question; None when the interface closes."""
        with self._state_lock:
            if self._closed.is_set():
                return None
            self._answering = True
        try:
            return self._answers.get()
        finally:
            self._answering = False

    def _set_waiting(self, waiting: bool) -> None:
        with self._state_lock:
            self._waiting = waiting
        self._post(self.app.invalidate)

    def ask_approval(self, request: ApprovalRequest) -> ApprovalChoice:
        return self._choose("Approval needed", request_text(request), request_choices(request), ApprovalChoice.DENY)

    def confirm_feedback(self, question: str) -> bool:
        """Ask whether to send a feedback report."""
        return self._choose("Feedback", question, [(True, "Yes, send"), (False, "No")], False)

    def confirm_hook(self, description: str) -> bool:
        """Ask whether to trust a hook that has not been approved before."""
        body = f"A hook in your configuration has not been trusted yet:\n{description}"
        return self._choose("Hook trust", body, [(True, "Yes, trust and run this hook"), (False, "No")], False)

    def confirm_project_trust(self, project: Path, cwd: Path) -> bool | None:
        """Ask whether to trust the files in a project. None if the dialog is closed without an answer."""
        lines = [f"Do you trust the files in {project}?"]
        if project != cwd:
            lines.append(f"{cwd} is inside a Git repository; the answer applies to the repository root.")
        lines.append("Trusting it lets meltzer load the project config and rules from its .meltzer directories.")
        choices: list[tuple[bool | None, str]] = [(True, "Yes, trust this directory"), (False, "No")]
        return self._choose("Project trust", "\n".join(lines), choices, None)

    def ask_questions(self, questions: list[Question]) -> dict[str, list[str]] | None:
        """Ask request_user_input questions, one dialog each; "Other" reads the answer from the input box.

        None if a dialog is refused or the input ends before every question was answered.
        """
        answers: dict[str, list[str]] = {}
        for question in questions:
            body = "\n".join([question.question, "", *(f"{o.label}: {o.description}" for o in question.options)])
            choices = [(option.label, option.label) for option in question.options]
            answer = self._choose(question.header, body, [*choices, (OTHER, "Other (type your own answer)")], None)
            if answer is OTHER:
                self._show(f"Type your answer to \"{question.header}\" and press Enter.")
                answer = self._read_answer()
            if answer is None:
                return None
            answers[question.id] = [answer]
        return answers

    def _choose(self, title: str, body: str, choices: list[tuple[T, str]], cancel: T) -> T:
        """Open a dialog and wait for its answer; `cancel` if the interface closes first."""
        with self._dialog_lock:
            answered = threading.Event()
            answers: list[T] = []

            def answer(value: T) -> None:
                if not answered.is_set():
                    answers.append(value)
                    answered.set()

            with self._state_lock:
                if self._closed.is_set():
                    return cancel
                self._pending = (answer, cancel)
            if not self._post(lambda: self._open_dialog(title, body, choices, cancel, answer)):
                answer(cancel)
            answered.wait()
            with self._state_lock:
                self._pending = None
            return answers[0]

    # --- interface thread ------------------------------------------------------------

    def _open_dialog(
        self, title: str, body: str, choices: list[tuple[T, str]], cancel: T, answer: Callable[[T], None]
    ) -> None:
        def on_answer(value: T) -> None:
            self._container.floats.remove(dialog_float)
            self.app.layout.update_parents_relations()
            self.dialog = None
            self.app.layout.focus(self.input)
            answer(value)

        dialog = Dialog(self.app, title, body, choices, cancel=cancel, on_answer=on_answer)
        dialog_float = Float(content=dialog, left=2, right=2)
        self._container.floats.append(dialog_float)
        # Key bindings are looked up through the parents of the focused window;
        # the dialog's parents are known only after this (or the next redraw).
        self.app.layout.update_parents_relations()
        self.dialog = dialog
        self.app.layout.focus(dialog.choice_window)
        self.app.invalidate()

    def _accept(self, buffer: Buffer) -> bool:
        text = buffer.text
        if not text.strip():
            return False
        self.transcript.append(f"> {text}")
        command = local_command(text)
        if self._answering:
            self._answers.put(text)
        elif command == "/clear":
            self.transcript.clear()
        elif command == "/help":
            self.transcript.append(help_text())
        elif command == "/status":
            self.transcript.append(status_report(self._session))
        else:
            self._prompts.put(text)
        return False

    def _ctrl_c(self) -> None:
        if self.input.text:
            self.input.text = ""
            return
        with self._state_lock:
            if self._answering:
                self._answers.put(None)
                return
            if self._waiting:
                self._prompts.put(None)
                return
            worker = self._worker
            # A dialog that is about to open answers Ctrl-C itself.
            if worker is None or not worker.is_alive() or self._pending is not None:
                return
            _raise_in_thread(worker, KeyboardInterrupt)
        self.transcript.append("stopping the turn")

    def _key_bindings(self, no_dialog: Condition) -> KeyBindings:
        keys = KeyBindings()

        @keys.add("c-c", filter=no_dialog)
        @keys.add("<sigint>", filter=no_dialog)
        def _(event: KeyPressEvent) -> None:
            self._ctrl_c()

        @keys.add("c-d", filter=no_dialog)
        def _(event: KeyPressEvent) -> None:
            if not self.input.text:
                self._prompts.put(None)

        @keys.add("pageup", filter=no_dialog)
        def _(event: KeyPressEvent) -> None:
            self.transcript.scroll(-SCROLL_LINES)

        @keys.add("pagedown", filter=no_dialog)
        def _(event: KeyPressEvent) -> None:
            self.transcript.scroll(SCROLL_LINES)

        return keys

    def status_text(self) -> str:
        session = self._session
        if session is None:
            parts = [f"meltzer {__version__}"]
        else:
            parts = [
                f"model: {session.config.model}",
                f"sandbox: {session.policy.mode.value}",
                f"cwd: {session.root}",
            ]
        parts.append("ready" if self._waiting else "working")
        return plain_text("  ".join(parts)).replace("\n", " ")

    def _status_fragments(self) -> StyleAndTextTuples:
        return [("class:status", f" {self.status_text()}")]
