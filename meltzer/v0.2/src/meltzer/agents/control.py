"""Subagents of a session tree: start them, send them input, wait for them, close them.

One AgentControl is shared by a top-level session and all its subagents. It
keeps a handle for every subagent: its session, status, final message, role
and depth. A subagent started in the background runs its turns in a thread of
its own; input sent while it is running is queued and handled, in order, after
the current turn. A session can address only the subagents it started.

`agents.max_depth` limits how deep the tree grows (the top-level session is
depth 0); `agents.max_threads` limits how many subagents of the tree are open
at once. A subagent stays open until it is closed: a `spawn_agent` call that
waits for the result closes the subagent when it returns.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Protocol

from meltzer.agents.roles import DEFAULT_ROLE, AgentRole, RoleSet
from meltzer.client import ModelError, ModelResponse

# Timeouts of the `wait` tool.
DEFAULT_WAIT_TIMEOUT_MS = 30_000
MIN_WAIT_TIMEOUT_MS = 10_000
MAX_WAIT_TIMEOUT_MS = 3_600_000
# How long closing a session waits for the threads of its subagents to end.
CLOSE_JOIN_SECONDS = 5.0


class AgentStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CLOSED = "closed"


FINAL_STATUSES = frozenset({AgentStatus.COMPLETED, AgentStatus.FAILED, AgentStatus.CLOSED})


class AgentError(Exception):
    """A request about a subagent cannot be carried out; the message is returned to the model."""


class AgentClosed(ModelError):
    """A closed subagent tried to call the model."""


class AgentSession(Protocol):
    """The parts of a subagent session that AgentControl uses."""

    id: str
    depth: int
    config: Any
    client: Any

    def run_turn(self, prompt: str) -> str: ...

    def close(self) -> None: ...


class AgentOwner(Protocol):
    """A session that starts subagents."""

    id: str
    depth: int

    def start_subagent(self, role: AgentRole | None = None) -> AgentSession: ...


class AgentClient:
    """A subagent's model client: the parent's client, with the subagent's model.

    Requests made after the subagent is closed raise AgentClosed.
    """

    def __init__(self, client: Any, model: str, closed: threading.Event):
        self._client = client
        self.model = model
        self._closed = closed

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        model: str | None = None,
    ) -> ModelResponse:
        if self._closed.is_set():
            raise AgentClosed("the agent was closed")
        return self._client.complete(messages, tools, model=model or self.model)

    def close(self) -> None:
        # The parent's session closes the shared client.
        pass


@dataclass(eq=False)
class AgentHandle:
    id: str
    session: AgentSession = field(repr=False)
    parent_id: str
    role: AgentRole
    depth: int
    status: AgentStatus = AgentStatus.RUNNING
    final: str | None = None
    error: str | None = None
    inbox: deque[str] = field(default_factory=deque, repr=False)
    # The thread running the agent's turns; None while no turn runs in the background.
    thread: threading.Thread | None = field(default=None, repr=False)
    closed: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def label(self) -> str:
        return f"subagent {self.id[:8]}"

    def snapshot(self) -> dict[str, Any]:
        """The status reported to the model."""
        state: dict[str, Any] = {"status": self.status.value}
        if self.status == AgentStatus.FAILED:
            state["error"] = self.error
        elif self.status in FINAL_STATUSES and self.final is not None:
            state["final_message"] = self.final
        return state


def _describe(exc: Exception) -> str:
    return str(exc) if isinstance(exc, ModelError) else f"{type(exc).__name__}: {exc}"


class AgentControl:
    def __init__(
        self,
        roles: RoleSet,
        *,
        max_depth: int,
        max_threads: int,
        notice: Callable[[str], None] = lambda message: None,
    ):
        self.roles = roles
        self.max_depth = max_depth
        self.max_threads = max_threads
        self.notice = notice
        self._handles: dict[str, AgentHandle] = {}
        # Subagents being created, counted toward max_threads.
        self._starting = 0
        self._changed = threading.Condition()
        # Held while a reviewer asks the person at the terminal, so prompts
        # from agents running in the background do not interleave.
        self.prompt_lock = threading.Lock()

    def serialized(self, ask: Callable[..., Any]) -> Callable[..., Any]:
        """`ask`, called with prompt_lock held."""

        def locked(*args: Any, **kwargs: Any) -> Any:
            with self.prompt_lock:
                return ask(*args, **kwargs)

        return locked

    def get(self, agent_id: str) -> AgentHandle | None:
        with self._changed:
            return self._handles.get(agent_id)

    def children(self, parent_id: str) -> list[AgentHandle]:
        """The subagents a session started, oldest first."""
        with self._changed:
            return [handle for handle in self._handles.values() if handle.parent_id == parent_id]

    def _open_count(self) -> int:
        open_handles = sum(1 for handle in self._handles.values() if handle.status != AgentStatus.CLOSED)
        return open_handles + self._starting

    def _target(self, owner: AgentOwner, target: str) -> AgentHandle:
        handle = self._handles.get(target)
        if handle is None or handle.parent_id != owner.id:
            raise AgentError(f"unknown agent: {target}")
        return handle

    # --- start --------------------------------------------------------------------------

    def start(self, owner: AgentOwner, agent_type: str | None) -> AgentHandle:
        """Create a subagent session for `owner`. Its turns are run by run() or run_in_background().

        ConfigError from the session constructor is passed on.
        """
        role = self.roles.get(agent_type or DEFAULT_ROLE)
        if role is None:
            available = ", ".join(self.roles.names())
            raise AgentError(f"unknown agent_type {agent_type!r}; available: {available}")
        if owner.depth + 1 > self.max_depth:
            raise AgentError(f"agent depth limit reached (agents.max_depth = {self.max_depth})")
        with self._changed:
            if self._open_count() >= self.max_threads:
                raise AgentError(
                    f"agent thread limit reached (agents.max_threads = {self.max_threads}); "
                    "close an agent before starting another"
                )
            self._starting += 1
        handle: AgentHandle | None = None
        try:
            session = owner.start_subagent(role)
            handle = AgentHandle(
                id=session.id, session=session, parent_id=owner.id, role=role, depth=session.depth
            )
            session.client = AgentClient(session.client, session.config.model, handle.closed)
        finally:
            with self._changed:
                self._starting -= 1
                if handle is not None:
                    self._handles[handle.id] = handle
        return handle

    def run(self, handle: AgentHandle, prompt: str) -> str:
        """Run one turn in the calling thread, then close the subagent. Returns its final message."""
        try:
            final = handle.session.run_turn(prompt)
            self._set_result(handle, AgentStatus.COMPLETED, final=final)
            return final
        except Exception as exc:
            self._set_result(handle, AgentStatus.FAILED, error=_describe(exc))
            raise
        except BaseException:
            # Interrupted (KeyboardInterrupt): the turn has no result.
            self._set_result(handle, AgentStatus.FAILED, error="interrupted")
            raise
        finally:
            self.shutdown(handle)

    def _set_result(
        self, handle: AgentHandle, status: AgentStatus, *, final: str | None = None, error: str | None = None
    ) -> None:
        with self._changed:
            if handle.status == AgentStatus.CLOSED:
                return
            handle.status, handle.final, handle.error = status, final, error
            self._changed.notify_all()

    def run_in_background(self, handle: AgentHandle, prompt: str) -> None:
        """Start the subagent's turn in its own thread and return at once."""
        with self._changed:
            self._start_thread(handle, prompt)

    def _start_thread(self, handle: AgentHandle, prompt: str) -> None:
        handle.status, handle.final, handle.error = AgentStatus.RUNNING, None, None
        handle.thread = threading.Thread(
            target=self._work, args=(handle, prompt), name=f"meltzer-{handle.label}", daemon=True
        )
        handle.thread.start()

    def _work(self, handle: AgentHandle, prompt: str) -> None:
        """Run turns for `prompt` and then for each queued input, until the inbox is empty."""
        message: str | None = prompt
        closed = False
        while message is not None:
            try:
                final = handle.session.run_turn(message)
                status, error = AgentStatus.COMPLETED, None
            except Exception as exc:
                final, status, error = None, AgentStatus.FAILED, _describe(exc)
            except BaseException:
                self._abandon(handle)
                raise
            with self._changed:
                if handle.status == AgentStatus.CLOSED:
                    handle.thread = None
                    closed = True
                    self._changed.notify_all()
                    break
                if handle.inbox:
                    message = handle.inbox.popleft()
                    continue
                handle.status, handle.final, handle.error = status, final, error
                handle.thread = None
                message = None
                self._changed.notify_all()
            if status == AgentStatus.FAILED:
                self.notice(f"[{handle.label}] failed: {error}")
            else:
                self.notice(f"[{handle.label}] finished")
        # shutdown() leaves closing the session to this thread while a turn runs.
        if closed:
            handle.session.close()

    def _abandon(self, handle: AgentHandle) -> None:
        """End the background thread of a turn that was interrupted: no result, no further queued input."""
        with self._changed:
            closed = handle.status == AgentStatus.CLOSED
            if not closed:
                handle.status, handle.final, handle.error = AgentStatus.FAILED, None, "interrupted"
            handle.inbox.clear()
            handle.thread = None
            self._changed.notify_all()
        if closed:
            handle.session.close()

    # --- input and status ---------------------------------------------------------------

    def send_input(self, owner: AgentOwner, target: str, message: str) -> None:
        """Give a subagent another message. A running subagent handles it after its current turn."""
        with self._changed:
            handle = self._target(owner, target)
            if handle.status == AgentStatus.CLOSED:
                raise AgentError(f"agent {target} is closed")
            if handle.thread is not None:
                handle.inbox.append(message)
            else:
                self._start_thread(handle, message)
                self._changed.notify_all()

    def wait(
        self, owner: AgentOwner, targets: list[str], timeout: float
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        """Wait until at least one target has a final status, or until `timeout` seconds pass.

        Returns the statuses of the targets that are final, and whether the wait timed out.
        """
        deadline = time.monotonic() + timeout
        with self._changed:
            handles = [self._target(owner, target) for target in dict.fromkeys(targets)]
            while True:
                done = [handle for handle in handles if handle.status in FINAL_STATUSES]
                if done:
                    return {handle.id: handle.snapshot() for handle in done}, False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {}, True
                self._changed.wait(remaining)

    # --- close --------------------------------------------------------------------------

    def close(self, owner: AgentOwner, target: str) -> AgentStatus:
        """Close a subagent and its subagents. Returns the status it had before."""
        with self._changed:
            handle = self._target(owner, target)
        return self.shutdown(handle)

    def shutdown(self, handle: AgentHandle) -> AgentStatus:
        """Close a subagent and its subagents. A turn in progress stops at its next model request."""
        with self._changed:
            previous = handle.status
            if previous == AgentStatus.CLOSED:
                return previous
            handle.status = AgentStatus.CLOSED
            handle.inbox.clear()
            handle.closed.set()
            close_now = handle.thread is None
            children = [child for child in self._handles.values() if child.parent_id == handle.id]
            self._changed.notify_all()
        for child in children:
            self.shutdown(child)
        if close_now:
            handle.session.close()
        return previous

    def close_children(self, parent_id: str) -> None:
        """Close every subagent a session started and wait briefly for their threads to end."""
        children = self.children(parent_id)
        for child in children:
            self.shutdown(child)
        deadline = time.monotonic() + CLOSE_JOIN_SECONDS
        for child in children:
            thread = child.thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(max(0.0, deadline - time.monotonic()))
