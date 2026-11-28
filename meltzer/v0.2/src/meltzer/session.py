"""A meltzer session: configuration, tools and the turn loop."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Callable

from meltzer import __version__
from meltzer.agents.control import AgentControl
from meltzer.agents.roles import AGENTS_DIR_NAME, DEFAULT_ROLE, AgentRole, RoleSet, role_config
from meltzer.agents_md import load_agents_md
from meltzer.approval.orchestrator import ApprovalOrchestrator, make_reviewer
from meltzer.approval.policy import ApprovalSettings, headless_conflict
from meltzer.approval.rules import RULES_DIR_NAME, RuleSet
from meltzer.client import ChatClient, ModelResponse, Usage
from meltzer.config.loader import ConfigError, meltzer_home
from meltzer.config.schema import Config
from meltzer.context import environment_context, environment_update, user_shell_command
from meltzer.exec import ProcessRunner
from meltzer.exec_sessions import ProcessStore
from meltzer.history import History, Item, Kind, SessionRecorder, session_file
from meltzer.hook_output import HookOutcome, context_item, prompt_item
from meltzer.hooks import (
    POST_TOOL_USE,
    PRE_TOOL_USE,
    SESSION_START,
    STOP,
    SUBAGENT_START,
    SUBAGENT_STOP,
    TRUSTED_HOOKS_FILE,
    USER_PROMPT_SUBMIT,
    HookRunner,
    TrustStore,
)
from meltzer.mcp.config import parse_mcp_servers
from meltzer.mcp.manager import McpManager
from meltzer.network.policy import NetworkApprovals, NetworkPolicy
from meltzer.memories import memory_instructions, update_memories
from meltzer.monitor.review import MonitorReviewer
from meltzer.notify import Notifier
from meltzer.plugins.loader import load_plugins
from meltzer.sandbox.grants import GrantStore
from meltzer.sandbox.policy import NetworkMode, SandboxMode, SandboxPolicy, ensure_project_dir
from meltzer.sandbox.profiles import policy_from_config
from meltzer.sessions import UNFINISHED_TOOL_CALL, InitialHistory, StartMode, open_recorder
from meltzer.skills.catalog import render_catalog
from meltzer.skills.inject import mentioned_skill_items
from meltzer.skills.loader import SkillRoot, SkillScope, SkillSet, skill_roots
from meltzer.telemetry.events import session_telemetry
from meltzer.tools.agents import agent_tools
from meltzer.tools.apply_patch import ApplyPatchTool
from meltzer.tools.compact import CompactTool
from meltzer.tools.context_remaining import ContextRemainingTool
from meltzer.tools.exec_command import ExecCommandTool
from meltzer.tools.image_gen import image_gen_tool, image_output_dir
from meltzer.tools.mcp_tool import mcp_tools
from meltzer.tools.plan import TaskPlan, UpdatePlanTool
from meltzer.tools.registry import ToolOutput, ToolRegistry
from meltzer.tools.request_permissions import RequestPermissionsTool
from meltzer.tools.request_user_input import RequestUserInputTool
from meltzer.tools.shell import ShellTool
from meltzer.tools.web_search import web_search_tool
from meltzer.tools.write_stdin import WriteStdinTool
from meltzer.ui import TerminalUI
from meltzer.utils.git import git_info


# The line of instructions.md replaced by the description of the session's command tools.
COMMAND_TOOLS_MARKER = "<command tools>\n"
SHELL_TOOL_LINES = (
    "- `shell` runs a command with `bash -c` and returns its exit code and output. Use it to read files "
    "(`cat`, `sed -n`, `rg`), list directories, build, and run tests. With `background: true` it starts "
    "a long-running process and returns at once.\n"
)
UNIFIED_EXEC_LINES = (
    "- `exec_command` runs a command with `bash -c` and returns its exit code and output, or a session ID "
    "if it is still running. Use it to read files (`cat`, `sed -n`, `rg`), list directories, build, and "
    "run tests, and for dev servers, test watchers and programs that ask questions, with `tty: true` when "
    "the program reads input. `write_stdin` sends characters to that session; empty `chars` waits for "
    "more output.\n"
)


def base_instructions(unified_exec: bool) -> str:
    text = resources.files("meltzer").joinpath("instructions.md").read_text(encoding="utf-8")
    return text.replace(COMMAND_TOOLS_MARKER, UNIFIED_EXEC_LINES if unified_exec else SHELL_TOOL_LINES)


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
    network = {
        NetworkMode.FULL: "enabled",
        NetworkMode.PROXY: "through meltzer's network proxy (HTTP_PROXY and HTTPS_PROXY are set)",
        NetworkMode.NONE: "disabled",
    }[policy.network]
    return (
        "# Environment\n"
        f"- Workspace root: {policy.root}\n"
        f"- Sandbox: {sandbox}\n"
        f"- Network access in the sandbox: {network}\n"
        f"- Approval policy: {settings.policy.value} (reviewer: {settings.reviewer.value})\n"
    )


def read_developer_instructions(path: str) -> str:
    """Text of the `developer_instructions_file` setting. The text is not repeated in error messages."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read developer_instructions_file {path}: {exc.strerror}") from exc
    except UnicodeDecodeError:
        raise ConfigError(f"developer_instructions_file {path} is not valid UTF-8") from None


def make_temp_dir(session_id: str) -> Path:
    """Create the directory that sandboxed commands of a session see as /tmp."""
    try:
        return Path(tempfile.mkdtemp(prefix=f"meltzer-{session_id}-"))
    except OSError as exc:
        raise ConfigError(f"cannot create the session's temporary directory: {exc.strerror}") from exc


class Session:
    def __init__(
        self,
        config: Config,
        root: Path,
        ui: TerminalUI,
        *,
        home: Path | None = None,
        parent: Session | None = None,
        role: AgentRole | None = None,
        initial: InitialHistory | None = None,
    ):
        self.config = config
        self.ui = ui
        # A resumed session keeps its id; a forked one gets a new id (see meltzer.sessions).
        if initial is not None and initial.mode == StartMode.RESUMED:
            self.id = initial.session_id
        else:
            self.id = str(uuid.uuid4())
        self.started = datetime.now(timezone.utc)
        # A subagent session shares its parent's model client and process runner.
        self.parent = parent
        self.depth = 0 if parent is None else parent.depth + 1
        # The role a subagent was started with (see meltzer.agents.roles).
        self.role = role
        self.home = home
        self._after_tool_calls: list[Callable[[], None]] = []
        # Set by stop_turn(); the turn ends after the current batch of tool calls.
        self._stop_reason: str | None = None
        # Token usage of the last model response that reported it (see get_context_remaining).
        self.last_usage: Usage | None = None
        self.settings = ApprovalSettings(policy=config.approval_policy, reviewer=config.approvals_reviewer)
        if not ui.interactive:
            conflict = headless_conflict(self.settings)
            if conflict:
                raise ConfigError(conflict)

        api_key = os.environ.get(config.api_key_env)
        if not api_key:
            raise ConfigError(f"environment variable {config.api_key_env} is not set")
        # Whatever is created from here on is released by self._cleanup when the session cannot start.
        self._cleanup = ExitStack()
        try:
            if parent is None:
                self.plugins = load_plugins(config.plugins, meltzer_home(home), configured_servers=config.mcp_servers)
                for warning in self.plugins.warnings:
                    ui.info(f"warning: {warning}")
                mcp_servers = parse_mcp_servers({**config.mcp_servers, **self.plugins.mcp_servers()})
                self.network_policy = NetworkPolicy.from_config(config)
            else:
                self.plugins = parent.plugins
                mcp_servers = ()
                self.network_policy = parent.network_policy
            # One telemetry for the session tree; subagents record their events in it.
            self.telemetry = parent.telemetry if parent is not None else session_telemetry(config, self.id, ui.info)
            if parent is None:
                self._cleanup.callback(self.telemetry.close)

            # The file is read once, by the top-level session; a subagent reuses the text.
            if parent is not None:
                self.developer_instructions = parent.developer_instructions
            elif config.developer_instructions_file is not None:
                self.developer_instructions = read_developer_instructions(config.developer_instructions_file)
            else:
                self.developer_instructions = ""

            state_dir = meltzer_home(home)
            if parent is not None:
                # A subagent runs with its parent's base policy (not the parent's grants).
                self.policy = parent.policy
            else:
                self.policy = policy_from_config(
                    config, root, meltzer_home=state_dir, temp_dir=lambda: make_temp_dir(self.id), home=home
                )
            self.root = self.policy.root
            if parent is None and self.policy.temp_dir is not None:
                self._cleanup.callback(shutil.rmtree, self.policy.temp_dir, ignore_errors=True)
            if parent is None:
                try:
                    ensure_project_dir(self.root)
                except OSError as exc:
                    ui.info(f"warning: cannot create {self.root / '.meltzer'}: {exc.strerror}")
            # One AgentControl for the session tree; roles are read once, by the top-level session.
            if parent is not None:
                self.agent_control = parent.agent_control
            else:
                roles = RoleSet.load(
                    state_dir / AGENTS_DIR_NAME, self.root, include_project=config.project_trusted, home=home
                )
                for warning in roles.warnings:
                    ui.info(f"warning: {warning}")
                self.agent_control = AgentControl(
                    roles, max_depth=config.agents_max_depth, max_threads=config.agents_max_threads, notice=ui.info
                )
            self.reviewer = make_reviewer(
                config.approvals_reviewer,
                self.agent_control.serialized(ui.ask_approval),
                monitor=lambda: MonitorReviewer(self),
            )
            if parent is not None:
                self.rules = parent.rules
            else:
                self.rules = RuleSet.load(
                    state_dir / RULES_DIR_NAME, self.root, include_project=config.project_trusted, home=home
                )
                for warning in self.rules.warnings:
                    ui.info(f"warning: {warning}")
            # Skills are read once, by the top-level session; subagents use the same skills.
            if parent is not None:
                self.skills = parent.skills
            else:
                roots = skill_roots(state_dir, self.root, include_project=config.project_trusted)
                roots += [SkillRoot(path, SkillScope.USER) for path in self.plugins.skill_roots()]
                self.skills = SkillSet.load(roots, config.skills_config, home=home)
                for warning in self.skills.warnings:
                    ui.info(f"warning: {warning}")
            self.orchestrator = ApprovalOrchestrator(self.settings, self.policy, self.reviewer, self.rules)
            self.orchestrator.telemetry = self.telemetry
            # Network decisions for this session's commands; approvals are not shared with other agents.
            self.network_approvals = NetworkApprovals(self.network_policy, self.orchestrator.decide_network)
            if parent is not None:
                self.runner = parent.runner
                self.process_store = parent.process_store
            else:
                self.runner = ProcessRunner(self.policy, log_dir=state_dir / "logs" / self.id)
                self.runner.telemetry = self.telemetry
                self.process_store = ProcessStore()
            # The model gets one set of command tools, as `features.unified_exec` selects.
            if config.unified_exec:
                command_tools = [
                    ExecCommandTool(
                        self.root, self.orchestrator, self.runner, self.process_store, self.id,
                        network=self.network_approvals,
                    ),
                    WriteStdinTool(self.process_store, config.background_terminal_max_timeout, self.id),
                ]
            else:
                command_tools = [ShellTool(self.root, self.orchestrator, self.runner, network=self.network_approvals)]
            self.tools = ToolRegistry([*command_tools, ApplyPatchTool(self.root, self.orchestrator)])
            # Subagents use their parent's MCP servers.
            if parent is not None:
                self.mcp = parent.mcp
            else:
                self.mcp = McpManager.start(
                    mcp_servers, root=self.root, log_dir=state_dir / "logs" / self.id, notice=ui.info
                )
                self._cleanup.callback(self.mcp.shutdown)
            for tool in mcp_tools(self.mcp, self.orchestrator, self.root):
                self.tools.register(tool)
            if parent is None:
                self.client = ChatClient(config.base_url, api_key, config.model)
                self._cleanup.callback(self.client.close)
            else:
                self.client = parent.client
            self.history = History()
            self.instructions = base_instructions(config.unified_exec) + "\n" + environment_section(self.policy, self.settings)
            catalog = render_catalog(self.skills.enabled()) if config.skills_include_instructions else ""
            if catalog:
                self.instructions += "\n" + catalog + "\n"
            if self.developer_instructions:
                self.instructions += "\n# Developer instructions\n" + self.developer_instructions
            if role is not None and role.developer_instructions:
                self.instructions += f"\n# Agent role: {role.name}\n" + role.developer_instructions

            meta = {
                "id": self.id,
                "cwd": str(self.root),
                "started_at": self.started.isoformat(),
                "cli_version": __version__,
                "interactive": ui.interactive,
                "config": config.summary(),
                "config_sources": config.sources,
                **({"git": info} if parent is None and (info := git_info(self.root)) is not None else {}),
                **({"parent_session_id": parent.id} if parent is not None else {}),
                **({"agent_role": role.name} if role is not None else {}),
            }
            if initial is None:
                self.recorder = SessionRecorder(session_file(state_dir / "sessions", self.id, self.started))
                self._cleanup.callback(self.recorder.close)
                self.recorder.write_meta(meta)
            else:
                self.recorder = open_recorder(initial, state_dir / "sessions", self.id, self.started, meta)
                self._cleanup.callback(self.recorder.close)

            # Permissions granted to this session; a subagent starts without its parent's grants.
            self.grants = GrantStore(self.orchestrator, self.recorder)
            self.tools.register(RequestPermissionsTool(self.root, self.orchestrator, self.grants))
            web_search = web_search_tool(config, self.root, self.orchestrator)
            if web_search is not None:
                self.tools.register(web_search)
            image_gen = image_gen_tool(
                config, self.root, self.orchestrator, self.client, image_output_dir(state_dir, self.id)
            )
            if image_gen is not None:
                self.tools.register(image_gen)
            self.tools.register(ContextRemainingTool(config.model_context_window, lambda: self.last_usage))
            # Questions to the user: only the top-level session of an interactive run asks them.
            if parent is None and ui.interactive:
                self.tools.register(RequestUserInputTool(self.agent_control.serialized(ui.ask_questions)))

            self.notifier = Notifier(config.notify, ui.info)
            if parent is not None:
                self.hooks = parent.hooks.for_subagent(
                    self.id,
                    self.recorder.path,
                    agent_type=role.name if role is not None else DEFAULT_ROLE,
                    record=self.recorder.event,
                )
            else:
                self.hooks = HookRunner.for_session(
                    (*config.hook_definitions(), *self.plugins.hook_definitions()),
                    root=self.root,
                    trust=TrustStore(state_dir / TRUSTED_HOOKS_FILE),
                    confirm=(lambda hook: ui.confirm_hook(hook.describe())) if ui.interactive else None,
                    notice=ui.info,
                    session_id=self.id,
                    transcript_path=self.recorder.path,
                    record=self.recorder.event,
                )
            self.orchestrator.hooks = self.hooks
            if initial is not None:
                self._restore(initial)
            agents_md = load_agents_md(self.root, self.policy.can_read)
            if agents_md is not None:
                self._append(agents_md)
            if parent is None:
                self._add_memories(state_dir)
            self._append(environment_context(self.orchestrator.sandbox, self.settings))
            self.plan = TaskPlan(self.history, self.recorder)
            self.tools.register(UpdatePlanTool(self.plan, self.after_tool_calls))
            self.tools.register(
                CompactTool(
                    self.history, self.recorder, self.after_tool_calls, hooks=self.hooks, stop_turn=self.stop_turn
                )
            )
            if self.depth < config.agents_max_depth:
                for tool in agent_tools(self.agent_control, self, ui):
                    self.tools.register(tool)
            if parent is None:
                self.telemetry.conversation_start(
                    model=config.model,
                    approval_policy=self.settings.policy.value,
                    reviewer=self.settings.reviewer.value,
                    sandbox_mode=self.policy.mode.value,
                )
                source = "startup" if initial is None else "resume"
                self._add_hook_context(SESSION_START, self.hooks.session_start(source))
            else:
                self._add_hook_context(SUBAGENT_START, self.hooks.subagent_start())
        except BaseException:
            self._cleanup.close()
            raise

    def start_subagent(self, role: AgentRole | None = None) -> Session:
        if role is None:
            role = self.agent_control.roles.get(DEFAULT_ROLE)
        config = role_config(self.config, role) if role is not None else self.config
        return Session(config, self.root, self.ui, home=self.home, parent=self, role=role)

    def _restore(self, initial: InitialHistory) -> None:
        """Add the conversation of a recorded session; a forked session records it in its own file."""
        for item in initial.items:
            self.history.append(item)
            if initial.mode == StartMode.FORKED:
                self.recorder.record(item)
        self._close_open_tool_calls(UNFINISHED_TOOL_CALL)

    def _add_memories(self, state_dir: Path) -> None:
        """Write notes from earlier sessions and add the memory notes to the instructions (top-level sessions only)."""
        if self.config.memories_generate:
            update_memories(
                state_dir,
                self.client,
                model=self.config.memories_extract_model,
                current_session=self.id,
                max_bytes=self.config.memories_max_bytes,
                notice=self.ui.info,
            )
        if self.config.memories_use:
            notes = memory_instructions(state_dir, max_bytes=self.config.memories_max_bytes)
            if notes:
                self.instructions += "\n" + notes

    def stop_turn(self, reason: str) -> None:
        """End the current turn, with `reason` as its final message, after the current batch of tool calls."""
        self._stop_reason = reason

    def refresh_environment(self) -> None:
        """Add an environment item if the session's sandbox policy changed since the last one was added."""
        item = environment_update(self.history, self.orchestrator.sandbox, self.settings)
        if item is not None:
            self._append(item)

    def add_user_shell_command(self, command: str, exit_code: int, output: str) -> None:
        """Add a command the user ran with `!cmd`, its exit code and output to the conversation."""
        self._append(user_shell_command(command, exit_code, output))

    def _add_hook_context(self, event: str, outcome: HookOutcome) -> None:
        for text in outcome.additional_context:
            self._append(context_item(event, text))

    def _add_tool_hook_context(self, event: str, outcome: HookOutcome) -> None:
        """Add a tool hook's context once the results of the current batch of tool calls are in the history."""
        if outcome.additional_context:
            self.after_tool_calls(lambda: self._add_hook_context(event, outcome))

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
        self.refresh_environment()

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

    def run_turn(self, prompt: str, source: str | None = None) -> str:
        """Add a user message and run the model until it answers without tool calls.

        The turn also ends after `max_turn_steps` model requests. If it is
        interrupted (KeyboardInterrupt), tool calls without a result get one and
        pending history updates (plan, compaction) are applied, so the history
        stays valid and in step with the plan for the next turn. UserPromptSubmit
        hooks run before the prompt is added; Stop (SubagentStop in a subagent)
        hooks run when the model has answered. `source` is recorded on the user
        item (for example "voice").
        """
        check = self.hooks.user_prompt_submit(prompt)
        if check.blocked or check.stop:
            self._add_hook_context(USER_PROMPT_SUBMIT, check)
            self.recorder.event("prompt_blocked", {"prompt": prompt, "reason": check.reason})
            return f"The prompt was blocked by a UserPromptSubmit hook: {check.reason}"
        self.refresh_environment()
        self._append(Item.user(prompt, source=source))
        self._add_hook_context(USER_PROMPT_SUBMIT, check)
        for item in mentioned_skill_items(self.skills.enabled(), prompt, self.ui.info):
            self._append(item)
        self._stop_reason = None
        try:
            final = self._run_steps()
            final = self._run_stop_hooks(final)
        except KeyboardInterrupt:
            self._close_open_tool_calls("interrupted by the user")
            self._run_after_tool_calls()
            raise
        finally:
            self.grants.end_turn()
            self.telemetry.flush()
        self._finish_turn(prompt, final)
        return final

    def _run_steps(self) -> str:
        for _ in range(self.config.max_turn_steps):
            with self.telemetry.api_request(self.config.model):
                response = self.client.complete(self._messages(), self.tools.specs())
            if response.usage is not None:
                self.last_usage = response.usage
            self._record_response(response)
            if not response.tool_calls:
                return response.content or ""
            if response.content:
                self.ui.assistant_message(response.content)
            for call in response.tool_calls:
                self.ui.tool_call(call.name, call.arguments)
                started = time.monotonic()
                output = self._run_tool(call.name, call.arguments)
                self.telemetry.tool_result(call.name, success=output.success, duration=time.monotonic() - started)
                self._append(Item.tool_result(call.id, call.name, output.text))
            self._run_after_tool_calls()
            if self._stop_reason is not None:
                reason, self._stop_reason = self._stop_reason, None
                return reason
        return f"Stopped: the turn reached max_turn_steps ({self.config.max_turn_steps} model requests)."

    def _close_open_tool_calls(self, text: str) -> None:
        answered = {item.call_id for item in self.history if item.kind == Kind.TOOL_RESULT}
        for item in list(self.history):
            if item.kind == Kind.TOOL_CALL and item.call_id not in answered:
                self._append(Item.tool_result(item.call_id, item.name or "", text))

    def _run_stop_hooks(self, final: str) -> str:
        """Run the Stop hooks (SubagentStop in a subagent); a block continues the turn once."""
        event = STOP if self.parent is None else SUBAGENT_STOP
        run = self.hooks.stop if self.parent is None else self.hooks.subagent_stop
        check = run(final, stop_hook_active=False)
        if not check.blocked:
            return final
        self._append(prompt_item(event, check.reason))
        final = self._run_steps()
        if run(final, stop_hook_active=True).blocked:
            self.ui.info(f"{event} hooks asked to continue the turn again; the turn ends here")
        return final

    def _run_tool(self, name: str, arguments: str) -> ToolOutput:
        hook_input = self.tools.hook_input(name, arguments)
        if hook_input is not None:
            check = self.hooks.pre_tool_use(name, hook_input)
            self._add_tool_hook_context(PRE_TOOL_USE, check)
            if check.blocked:
                return ToolOutput(f"tool call blocked by a PreToolUse hook: {check.reason}", success=False)
            if check.updated_input is not None:
                arguments = json.dumps(check.updated_input, ensure_ascii=False)
                self.recorder.event("tool_input_updated", {"tool": name, "arguments": arguments})
        output = self.tools.dispatch(name, arguments)
        check = self.hooks.post_tool_use(name, arguments, output.text, output.success)
        self._add_tool_hook_context(POST_TOOL_USE, check)
        if check.blocked:
            return ToolOutput(check.reason, success=False)
        if check.stop:
            return ToolOutput(check.reason, success=output.success)
        return output

    def _finish_turn(self, prompt: str, final: str) -> None:
        # A subagent's turn is part of a tool call of its parent's turn.
        if self.parent is not None:
            return
        self.notifier.turn_complete(
            thread_id=self.id,
            turn_id=str(uuid.uuid4()),
            cwd=self.root,
            input_messages=[prompt],
            last_assistant_message=final or None,
        )

    def close(self) -> None:
        """End the session. Processes are stopped first; a step that fails does not skip the others."""
        steps: list[Callable[[], None]] = []
        if self.parent is None:
            steps += [self.runner.terminate_all, self.process_store.terminate_all]
        steps += [
            lambda: self.agent_control.close_children(self.id),
            self.grants.clear,
            lambda: self.process_store.terminate_owner(self.id),
        ]
        if self.parent is None:
            steps.append(lambda: self.hooks.session_end("exit"))
        steps.append(self._cleanup.close)
        for step in steps:
            try:
                step()
            except Exception as exc:
                self.ui.info(f"warning: closing the session failed: {exc}")
