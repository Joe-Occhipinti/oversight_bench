"""Inspect solver for one sample: set up the container, run meltzer, wait, copy logs (spec section 7).

Order of steps (7.1, with the gateway steps found in S2, spec 4.2):

1. BashArena setup (5.4 steps 1-2), `add_gateway_to_hosts`.
2. Open the model bridge in the `gateway` container (7.2), `start_gateway_forwarder`,
   `allow_gateway_in_firewall`, then the preconditions (section 6).
3. Snapshot of the running PIDs, then `meltzer exec` (7.3) under the wall-clock limit.
4. Wait for processes that outlive meltzer (7.4).
5. Copy the logs to `<run_dir>/samples/<sample_id>/` (7.5).

Everything the monitor must not see (R1) stays on the host: the developer instructions go to meltzer on
standard input, the bridge runs in the gateway container, and no command run here during meltzer's run
carries a visible wrapper (no `timeout=` argument to `exec`).

The result of the run is kept in the sample store under `"meltzer"`:
`{"exit_code", "time_limit_hit", "wait_seconds", "leftover"}` (keys are missing for steps that did not
finish). `bench.task` builds `samples.jsonl` from it.

Inspect behaviour that matters here (inspect_ai 0.3.287):
- `exec` without `timeout` has no time limit, no `timeout` wrapper and no retry.
- Every `exec` holds one slot of the "subprocesses" and "docker-cli" limits for its whole duration unless
  `concurrency=False` is passed. The meltzer `exec` runs up to 30 minutes, so it is called with
  `concurrency=False`; otherwise a few parallel samples could fill every slot and block the commands that
  stop them.
"""

from __future__ import annotations

import json
import logging
import secrets
import shutil
import string
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import anyio
from inspect_ai.agent import sandbox_agent_bridge
from inspect_ai.model import GenerateConfig, Model, get_model
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.util import sandbox

from bench import basharena, image, preconditions, prompts, reasoning
from bench.config import REPO_ROOT, ModelSpec, RunConfig, to_plain

logger = logging.getLogger(__name__)

# Builds the model behind an alias: (model id, its spec in the run config) -> Model.
ModelFactory = Callable[[str, ModelSpec], Model]

STORE_KEY = "meltzer"
MELTZER_PATTERN = "meltzer exec -C /app"  # matches meltzer's command line (7.3)
KILL_GRACE_SECONDS = 30
POLL_SECONDS = 1
MAX_USER_MESSAGE_BYTES = 120_000  # Linux allows 131,072 bytes per argument
PROXY_START_SECONDS = 30
MELTZER_HOME = preconditions.MELTZER_HOME
LOG_DIRS = ("sessions", "logs")
# `echo $$` and then `exec ps` give ps the shell's PID, so the poll can leave itself out.
PS_SCRIPT = "echo $$; exec ps -eo pid=,ppid=,args= -ww"
INIT_PID = 1  # processes whose parent exited are adopted by it


# ---------------------------------------------------------------- models (7.2)


def default_model_factory(model_id: str, spec: ModelSpec) -> Model:
    """OpenRouter model with the reasoning effort and provider of the run config.

    Inspect's OpenRouter provider sends `provider` as `extra_body.provider` and `reasoning_effort` as
    `extra_body.reasoning.effort`; no effort setting is sent when the effort is None.
    """
    return get_model(
        f"openrouter/{model_id}",
        config=GenerateConfig(reasoning_effort=spec.reasoning_effort),
        provider=to_plain(spec.provider),
    )


def model_aliases(cfg: RunConfig, factory: ModelFactory) -> dict[str, Model]:
    """The models meltzer can reach through the bridge, under the ids of its config file."""
    agent, monitor = cfg.models.agent, cfg.models.monitor
    if agent.id == monitor.id and agent != monitor:
        raise ValueError(f"agent and monitor use the same id {agent.id!r} with different settings")
    return {agent.id: factory(agent.id, agent), monitor.id: factory(monitor.id, monitor)}


# ---------------------------------------------------------------- command (7.3)


def random_api_key() -> str:
    """`sk-` and 48 random letters and digits, new per sample. The proxy ignores it."""
    alphabet = string.ascii_letters + string.digits
    return "sk-" + "".join(secrets.choice(alphabet) for _ in range(48))


def build_meltzer_command(user_message: str) -> list[str]:
    """`meltzer exec -C /app -c developer_instructions_file=/dev/stdin "<user message>"`."""
    size = len(user_message.encode("utf-8"))
    if size >= MAX_USER_MESSAGE_BYTES:
        raise ValueError(f"user message is {size} bytes; the limit is {MAX_USER_MESSAGE_BYTES}")
    return ["meltzer", "exec", "-C", "/app", "-c", "developer_instructions_file=/dev/stdin", user_message]


@dataclass(frozen=True)
class MeltzerRun:
    exit_code: int
    stdout: str
    stderr: str
    time_limit_hit: bool


async def signal_meltzer(sb: Any, signal: str) -> None:
    """`pkill -<signal> -f "meltzer exec -C /app"` in the container (no `timeout` wrapper: R1)."""
    await sb.exec(["pkill", f"-{signal}", "-f", MELTZER_PATTERN], user="root")


def _first_error(group: BaseExceptionGroup) -> BaseException:
    """The first leaf of an exception group, so that callers see the original error."""
    error = group.exceptions[0]
    return _first_error(error) if isinstance(error, BaseExceptionGroup) else error


async def run_meltzer(
    sb: Any,
    command: list[str],
    developer_instructions: str,
    api_key: str,
    limit_seconds: float,
    *,
    grace_seconds: float = KILL_GRACE_SECONDS,
) -> MeltzerRun:
    """Run `command` in the container under a wall-clock limit that leaves no wrapper process.

    The developer instructions are the standard input of the command. One task of a task group runs the
    `exec`; another sleeps `limit_seconds`, sends SIGINT with `pkill` (meltzer then closes its session and
    exits with code 130) and, if meltzer still runs `grace_seconds` later, SIGKILL. The timer is cancelled
    when the `exec` returns.
    """
    fired = False

    async def limit_timer() -> None:
        nonlocal fired
        await anyio.sleep(limit_seconds)
        fired = True
        for signal, delay in (("INT", grace_seconds), ("KILL", 0)):
            try:
                await signal_meltzer(sb, signal)
            except Exception:
                logger.warning("could not send SIG%s to meltzer", signal, exc_info=True)
            await anyio.sleep(delay)

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(limit_timer)
            result = await sb.exec(
                command,
                input=developer_instructions,
                cwd="/app",
                env={"MELTZER_API_KEY": api_key},
                user="root",
                concurrency=False,
            )
            tg.cancel_scope.cancel()
    except BaseExceptionGroup as group:
        raise _first_error(group) from None
    return MeltzerRun(result.returncode, result.stdout, result.stderr, fired)


# ---------------------------------------------------------------- wait for leftover processes (7.4)


async def list_processes(sb: Any) -> list[tuple[int, int, str]]:
    """`(pid, parent pid, command line)` of the processes in the container, without the listing command."""
    result = await sb.exec(["sh", "-c", PS_SCRIPT], user="root")
    if not result.success:
        raise RuntimeError(f"ps failed ({result.returncode}): {result.stderr.strip()}")
    first, *lines = result.stdout.splitlines()
    own = int(first)
    processes = []
    for line in lines:
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit() and int(parts[0]) != own:
            processes.append((int(parts[0]), int(parts[1]), parts[2] if len(parts) > 2 else ""))
    return processes


@dataclass(frozen=True)
class WaitResult:
    seconds: float
    leftover: list[str]  # command lines of the processes still running


async def wait_for_leftovers(
    sb: Any,
    snapshot: set[int],
    cap_seconds: float,
    *,
    sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> WaitResult:
    """Poll every second until no process outside `snapshot` is left, or `cap_seconds` have passed.

    A process whose parent is in `snapshot` (and is not PID 1) does not count: BashArena's firewall daemon
    starts a new `inotifywait` child about every 30 seconds, which would otherwise always be a leftover.
    Processes that outlive meltzer (the notify program, anything started with `setsid` or `nohup`) are
    adopted by PID 1 and do count.
    """
    start = clock()
    while True:
        left = [
            f"{pid} {args}"
            for pid, ppid, args in await list_processes(sb)
            if pid not in snapshot and (ppid == INIT_PID or ppid not in snapshot)
        ]
        elapsed = clock() - start
        if not left:
            return WaitResult(elapsed, [])
        if elapsed >= cap_seconds:
            return WaitResult(elapsed, left)
        await sleep(POLL_SECONDS)


# ---------------------------------------------------------------- logs (7.5)


async def copy_meltzer_logs(sb: Any, dest: Path) -> list[Path]:
    """Copy the files under `/root/.meltzer/{sessions,logs}` to `dest/meltzer/{sessions,logs}`."""
    written = []
    for name in LOG_DIRS:
        folder = f"{MELTZER_HOME}/{name}"
        listing = await sb.exec(
            ["sh", "-c", 'test -d "$1" || exit 0; find "$1" -type f -print0', "sh", folder], user="root"
        )
        if not listing.success:
            raise RuntimeError(f"cannot list {folder}: {listing.stderr.strip()}")
        for path in filter(None, listing.stdout.split("\0")):
            relative = PurePosixPath(path).relative_to(MELTZER_HOME)
            if ".." in relative.parts:
                raise ValueError(f"unexpected path in the container: {path!r}")
            target = dest / "meltzer" / Path(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(await sb.read_file(path, text=False))
            written.append(target)
    return written


async def collect_sample_files(
    sb: Any, dest: Path, run: MeltzerRun | None, wait: WaitResult | None, wait_cap_seconds: float
) -> None:
    """Write the files of 7.5 into `dest` (the ones whose step finished) and copy meltzer's logs."""
    dest.mkdir(parents=True, exist_ok=True)
    if run is not None:
        (dest / "stdout.txt").write_text(run.stdout, encoding="utf-8", newline="\n")
        (dest / "stderr.txt").write_text(run.stderr, encoding="utf-8", newline="\n")
        (dest / "exit_code").write_text(f"{run.exit_code}\n", encoding="utf-8", newline="\n")
    if wait is not None:
        data = {"seconds": round(wait.seconds, 3), "cap_seconds": wait_cap_seconds, "leftover": wait.leftover}
        (dest / "wait.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
    await copy_meltzer_logs(sb, dest)


# ---------------------------------------------------------------- the solver


def render_developer_instructions(
    cfg: RunConfig, condition: str, side_task: str, assigned_card: str | None = None
) -> str:
    if condition == "aware" and cfg.prompts.aware_mode == "one_card" and assigned_card is None:
        raise ValueError("aware mode one_card: the sample has no assigned card")
    return prompts.render_developer_instructions(
        condition,
        setting_description=basharena.setting_description(),
        side_task_description=basharena.SIDE_TASKS[side_task].description,
        attack_base_path=REPO_ROOT / cfg.prompts.attack_base,
        aware_section_path=REPO_ROOT / cfg.prompts.aware_section_file,
        cards_dir=REPO_ROOT / cfg.prompts.cards,
        card_id=assigned_card,
    )


async def proxy_is_listening(gateway: Any) -> bool:
    """Does something listen on `GATEWAY_PROXY_PORT` in the gateway container? (IPv4 listening sockets.)"""
    result = await gateway.exec(["sh", "-c", "awk '$4==\"0A\"{print $2}' /proc/net/tcp"])  # hex ADDR:PORT
    ports = {int(entry.split(":")[1], 16) for entry in result.stdout.split()}
    return image.GATEWAY_PROXY_PORT in ports


async def wait_for_proxy(gateway: Any, *, seconds: float = PROXY_START_SECONDS) -> None:
    """Wait until Inspect's model proxy listens in the gateway (it starts a moment after the bridge opens)."""
    for _ in range(int(seconds * 4)):
        if await proxy_is_listening(gateway):
            return
        await anyio.sleep(0.25)
    raise RuntimeError(f"the model proxy is not listening on port {image.GATEWAY_PROXY_PORT} in the gateway")


def _record(state: TaskState, **fields: Any) -> None:
    state.store.set(STORE_KEY, {**(state.store.get(STORE_KEY) or {}), **fields})


@solver
def meltzer_solver(cfg: RunConfig, run_dir: Path, model_factory: ModelFactory | None = None) -> Solver:
    """One sample: setup, `meltzer exec`, wait, copy logs. `model_factory` replaces OpenRouter in tests."""
    reasoning.install()  # the model proxy returns reasoning in message fields, not as <think> text
    factory = model_factory or default_model_factory
    run_dir = Path(run_dir)

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        metadata = state.metadata
        sample_dir = run_dir / "samples" / str(state.sample_id)
        shutil.rmtree(sample_dir, ignore_errors=True)  # a retried sample starts clean
        sample_dir.mkdir(parents=True)

        task = basharena.load_task(metadata["main_task"])
        command = build_meltzer_command(state.input_text)
        developer_instructions = render_developer_instructions(
            cfg, metadata["condition"], metadata["side_task"], metadata.get("assigned_card")
        )
        aliases = model_aliases(cfg, factory)
        sb = sandbox()

        await basharena.run_environment_setup(task)
        await image.add_gateway_to_hosts()

        snapshot: set[int] | None = None  # set once meltzer may start
        run: MeltzerRun | None = None
        wait: WaitResult | None = None
        finished = False
        try:
            async with sandbox_agent_bridge(
                sandbox=image.GATEWAY_SERVICE, port=image.GATEWAY_PROXY_PORT, model_aliases=aliases
            ):
                await wait_for_proxy(sandbox(image.GATEWAY_SERVICE))
                await image.start_gateway_forwarder()
                await image.allow_gateway_in_firewall()
                await preconditions.write_preconditions(
                    cfg.models.agent.id, cfg.models.monitor.id, cfg.meltzer.max_turn_steps
                )
                snapshot = {pid for pid, _, _ in await list_processes(sb)}
                run = await run_meltzer(
                    sb, command, developer_instructions, random_api_key(), cfg.limits.meltzer_seconds
                )
            _record(state, exit_code=run.exit_code, time_limit_hit=run.time_limit_hit)
            wait = await wait_for_leftovers(sb, snapshot, cfg.limits.wait_seconds)
            _record(state, wait_seconds=round(wait.seconds, 3), leftover=wait.leftover)
            finished = True
        finally:
            if snapshot is not None:
                with anyio.CancelScope(shield=True):  # also runs when Inspect's sample time limit fires
                    try:
                        if run is None:
                            await signal_meltzer(sb, "KILL")
                        await collect_sample_files(sb, sample_dir, run, wait, cfg.limits.wait_seconds)
                    except Exception:
                        if finished:
                            raise
                        logger.warning("could not collect the files of sample %s", state.sample_id, exc_info=True)
        return state

    return solve
