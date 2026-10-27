"""A meltzer session: configuration, tools and the turn loop."""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Callable

from meltzer import __version__
from meltzer.agents_md import load_agents_md
from meltzer.approval.orchestrator import ApprovalOrchestrator, make_reviewer
from meltzer.approval.policy import ApprovalSettings, headless_conflict
from meltzer.approval.rules import RULES_DIR_NAME, RuleSet
from meltzer.client import ChatClient, ModelResponse
from meltzer.config.loader import ConfigError, meltzer_home
from meltzer.config.schema import Config
from meltzer.exec import ProcessRunner
from meltzer.history import History, Item, Kind, SessionRecorder, session_file
from meltzer.hooks import TRUSTED_HOOKS_FILE, HookRunner, TrustStore
from meltzer.monitor.review import MonitorReviewer
from meltzer.notify import Notifier
from meltzer.sandbox.policy import SandboxMode, SandboxPolicy, ensure_project_dir
from meltzer.tools.apply_patch import ApplyPatchTool
from meltzer.tools.compact import CompactTool
from meltzer.tools.plan import TaskPlan, UpdatePlanTool
from meltzer.tools.registry import ToolOutput, ToolRegistry
from meltzer.tools.shell import ShellTool
from meltzer.tools.spawn_agent import MAX_AGENT_DEPTH, SpawnAgentTool
from meltzer.ui import TerminalUI


def base_instructions() -> str:
    return resources.files("meltzer").joinpath("instructions.md").read_text(encoding="utf-8")


def environment_section(policy: SandboxPolicy, settings: ApprovalSettings) -> str:
    if policy.mode == SandboxMode.FULL_ACCESS:
        sandbox = "full-access: commands run without a sandbox."
    elif policy.mode == SandboxMode.READ_ONLY:
        sandbox = "read-only: commands can read files but not write them."
    else:
        sandbox = (
            f"workspace-write: commands can write in {policy.root} and in /tmp (private to this session); "
            ".meltzer and, when present, .git stay read-only."
        )
    network = "enabled" if policy.allows_network else "disabled"
    return (
        "# Environment\n"
        f"- Workspace root: {policy.root}\n"
        f"- Sandbox: {sandbox}\n"
        f"- Network access in the sandbox: {network}\n"
        f"- Approval policy: {settings.policy.value} (reviewer: {settings.reviewer.value})\n"
    )


def make_temp_dir(session_id: str) -> Path:
    """Create the directory that sandboxed commands of a session see as /tmp."""
    path = Path(tempfile.gettempdir()) / f"meltzer-{session_id}"
    try:
        path.mkdir(mode=0o700)
    except OSError as exc:
        raise ConfigError(f"cannot create the session's temporary directory {path}: {exc.strerror}") from exc
    return path


class Session:
    def __init__(
        self,
        config: Config,
        root: Path,
        ui: TerminalUI,
        *,
        home: Path | None = None,
        parent: Session | None = None,
    ):
        self.config = config
        self.ui = ui
        self.id = str(uuid.uuid4())
        self.started = datetime.now(timezone.utc)
        # A subagent session shares its parent's model client and process runner.
        self.parent = parent
        self.depth = 0 if parent is None else parent.depth + 1
        self.home = home
        self._after_tool_calls: list[Callable[[], None]] = []
        self.settings = ApprovalSettings(policy=config.approval_policy, reviewer=config.approvals_reviewer)
        if not ui.interactive:
            conflict = headless_conflict(self.settings)
            if conflict:
                raise ConfigError(conflict)

        api_key = os.environ.get(config.api_key_env)
        if not api_key:
            raise ConfigError(f"environment variable {config.api_key_env} is not set")

        state_dir = meltzer_home(home)
        if parent is not None:
            temp_dir = parent.policy.temp_dir
        elif config.sandbox_mode != SandboxMode.FULL_ACCESS:
            temp_dir = make_temp_dir(self.id)
        else:
            temp_dir = None
        self.policy = SandboxPolicy.for_session(
            config.sandbox_mode, root, meltzer_home=state_dir, temp_dir=temp_dir
        )
        self.root = self.policy.root
        if parent is None:
            try:
                ensure_project_dir(self.root)
            except OSError as exc:
                ui.info(f"warning: cannot create {self.root / '.meltzer'}: {exc.strerror}")
        self.reviewer = make_reviewer(config.approvals_reviewer, ui.ask_approval, monitor=lambda: MonitorReviewer(self))
        if parent is not None:
            self.rules = parent.rules
        else:
            self.rules = RuleSet.load(
                state_dir / RULES_DIR_NAME, self.root, include_project=config.project_trusted, home=home
            )
            for warning in self.rules.warnings:
                ui.info(f"warning: {warning}")
        self.orchestrator = ApprovalOrchestrator(self.settings, self.policy, self.reviewer, self.rules)
        if parent is not None:
            self.runner = parent.runner
        else:
            self.runner = ProcessRunner(self.policy, log_dir=state_dir / "logs" / self.id)
        self.tools = ToolRegistry(
            [
                ShellTool(self.root, self.orchestrator, self.runner),
                ApplyPatchTool(self.root, self.orchestrator),
            ]
        )
        self.client = parent.client if parent is not None else ChatClient(config.base_url, api_key, config.model)
        self.history = History()
        self.instructions = base_instructions() + "\n" + environment_section(self.policy, self.settings)

        self.recorder = SessionRecorder(session_file(state_dir / "sessions", self.id, self.started))
        self.recorder.write_meta(
            {
                "id": self.id,
                "cwd": str(self.root),
                "started_at": self.started.isoformat(),
                "cli_version": __version__,
                "interactive": ui.interactive,
                "config": config.summary(),
                "config_sources": config.sources,
                **({"parent_session_id": parent.id} if parent is not None else {}),
            }
        )

        self.notifier = Notifier(config.notify, ui.info)
        if parent is not None:
            self.hooks = parent.hooks.for_subagent(self.id, self.recorder.path)
        else:
            self.hooks = HookRunner.for_session(
                config.hook_definitions(),
                root=self.root,
                trust=TrustStore(state_dir / TRUSTED_HOOKS_FILE),
                confirm=(lambda hook: ui.confirm_hook(hook.describe())) if ui.interactive else None,
                notice=ui.info,
                session_id=self.id,
                transcript_path=self.recorder.path,
            )
        agents_md = load_agents_md(self.root)
        if agents_md is not None:
            self._append(agents_md)
        self.plan = TaskPlan(self.history, self.recorder)
        self.tools.register(UpdatePlanTool(self.plan, self.after_tool_calls))
        self.tools.register(CompactTool(self.history, self.recorder, self.after_tool_calls))
        if self.depth < MAX_AGENT_DEPTH:
            self.tools.register(SpawnAgentTool(self._start_subagent, ui))
        if parent is None:
            self.hooks.session_start()

    def _start_subagent(self) -> Session:
        return Session(self.config, self.root, self.ui, home=self.home, parent=self)

    def after_tool_calls(self, action: Callable[[], None]) -> None:
        """Run `action` once the results of the current batch of tool calls are in the history.

        Tools that add or replace history items use this, so a tool call is always
        followed directly by its result in the messages sent to the model.
        """
        self._after_tool_calls.append(action)

    def _run_after_tool_calls(self) -> None:
        actions, self._after_tool_calls = self._after_tool_calls, []
        for action in actions:
            action()

    def _append(self, item: Item) -> None:
        self.history.append(item)
        self.recorder.record(item)

    def _messages(self) -> list[dict]:
        return [{"role": "system", "content": self.instructions}, *self.history.to_messages()]

    def _record_response(self, response: ModelResponse) -> None:
        response_id = f"resp_{uuid.uuid4().hex}"
        if response.reasoning:
            self._append(Item.reasoning_fields(response.reasoning, response_id))
        if response.content:
            self._append(Item.assistant(response.content, response_id))
        for call in response.tool_calls:
            self._append(Item.tool_call(call.id, call.name, call.arguments, response_id))

    def run_turn(self, prompt: str) -> str:
        """Add a user message and run the model until it answers without tool calls.

        The turn also ends after `max_turn_steps` model requests. If it is
        interrupted (KeyboardInterrupt), tool calls without a result get one and
        pending history updates (plan, compaction) are applied, so the history
        stays valid and in step with the plan for the next turn.
        """
        self._append(Item.user(prompt))
        try:
            final = self._run_steps()
        except KeyboardInterrupt:
            self._close_open_tool_calls("interrupted by the user")
            self._run_after_tool_calls()
            raise
        self._finish_turn(prompt, final)
        return final

    def _run_steps(self) -> str:
        for _ in range(self.config.max_turn_steps):
            response = self.client.complete(self._messages(), self.tools.specs())
            self._record_response(response)
            if not response.tool_calls:
                return response.content or ""
            if response.content:
                self.ui.assistant_message(response.content)
            for call in response.tool_calls:
                self.ui.tool_call(call.name, call.arguments)
                output = self._run_tool(call.name, call.arguments)
                self._append(Item.tool_result(call.id, call.name, output.text))
            self._run_after_tool_calls()
        return f"Stopped: the turn reached max_turn_steps ({self.config.max_turn_steps} model requests)."

    def _close_open_tool_calls(self, text: str) -> None:
        answered = {item.call_id for item in self.history if item.kind == Kind.TOOL_RESULT}
        for item in list(self.history):
            if item.kind == Kind.TOOL_CALL and item.call_id not in answered:
                self._append(Item.tool_result(item.call_id, item.name or "", text))

    def _run_tool(self, name: str, arguments: str) -> ToolOutput:
        check = self.hooks.pre_tool_use(name, arguments)
        if check.blocked:
            return ToolOutput(f"tool call blocked by a PreToolUse hook: {check.reason}", success=False)
        output = self.tools.dispatch(name, arguments)
        self.hooks.post_tool_use(name, arguments, output.text, output.success)
        return output

    def _finish_turn(self, prompt: str, final: str) -> None:
        # A subagent's turn is part of a tool call of its parent's turn.
        if self.parent is not None:
            return
        self.hooks.stop(final)
        self.notifier.turn_complete(
            thread_id=self.id,
            turn_id=str(uuid.uuid4()),
            cwd=self.root,
            input_messages=[prompt],
            last_assistant_message=final or None,
        )

    def close(self) -> None:
        if self.parent is None:
            self.runner.terminate_all()
            self.client.close()
            if self.policy.temp_dir is not None:
                shutil.rmtree(self.policy.temp_dir, ignore_errors=True)
        self.recorder.close()
