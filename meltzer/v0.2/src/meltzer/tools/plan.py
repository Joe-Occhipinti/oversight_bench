"""The task plan: a checklist shared by the model (`update_plan`) and the user (`/plan`)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable

from meltzer.history import History, Item, Kind, Role, SessionRecorder
from meltzer.tools.registry import ToolError, ToolSpec


class StepStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


@dataclass
class PlanStep:
    step: str
    status: StepStatus = StepStatus.PENDING


class PlanError(ValueError):
    """An edit to the plan is invalid."""


def parse_status(value: Any) -> StepStatus:
    try:
        return StepStatus(value)
    except ValueError:
        allowed = ", ".join(s.value for s in StepStatus)
        raise PlanError(f"status must be one of: {allowed}; got {value!r}") from None


class TaskPlan:
    def __init__(self, history: History, recorder: SessionRecorder):
        self.history = history
        self.recorder = recorder
        self.steps: list[PlanStep] = []

    def render(self) -> str:
        lines = ["# Plan"]
        for number, step in enumerate(self.steps, start=1):
            lines.append(f"{number}. [{step.status.value}] {step.step}")
        return "\n".join(lines)

    def _check(self, steps: list[PlanStep]) -> None:
        if sum(1 for s in steps if s.status == StepStatus.IN_PROGRESS) > 1:
            raise PlanError("at most one step can be in_progress")

    def _step(self, number: int) -> PlanStep:
        if not 1 <= number <= len(self.steps):
            raise PlanError(f"no step {number}; the plan has {len(self.steps)} steps")
        return self.steps[number - 1]

    def replace(self, steps: list[PlanStep]) -> None:
        self._check(steps)
        self.steps = list(steps)

    def add(self, text: str) -> None:
        if not text.strip():
            raise PlanError("a step needs a description")
        self.steps.append(PlanStep(text.strip()))

    def edit(self, number: int, text: str) -> None:
        if not text.strip():
            raise PlanError("a step needs a description")
        self._step(number).step = text.strip()

    def set_status(self, number: int, status: StepStatus) -> None:
        step = self._step(number)
        updated = [PlanStep(s.step, status if s is step else s.status) for s in self.steps]
        self._check(updated)
        step.status = status

    def remove(self, number: int) -> None:
        self.steps.remove(self._step(number))

    def clear(self) -> None:
        self.steps = []

    def sync(self) -> None:
        """Bring the plan item in the history up to date with the steps."""
        item = self.history.last_of_kind(Kind.PLAN)
        if not self.steps:
            if item is not None:
                index = self.history.remove(item)
                self.recorder.event("item_removed", {"index": index})
            return
        if item is None:
            item = Item(Role.USER, Kind.PLAN, content=self.render())
            self.history.append(item)
            self.recorder.record(item)
            return
        item.content = self.render()
        self.recorder.event("item_updated", {"index": self.history.index_of(item), "item": item.to_record()})


PLAN_COMMAND = "/plan"
PLAN_COMMAND_HELP = """\
/plan                      show the plan
/plan add <step>           add a pending step
/plan edit <n> <step>      change the text of step n
/plan status <n> <status>  set step n to pending, in_progress or completed
/plan remove <n>           remove step n
/plan clear                remove all steps"""


def _step_number(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise PlanError(f"expected a step number, got {text!r}") from None


def run_plan_command(plan: TaskPlan, arguments: str) -> str:
    """Apply a `/plan` command typed by the user and return what to show them."""
    action, _, rest = arguments.strip().partition(" ")
    rest = rest.strip()
    if action:
        try:
            if action == "add":
                plan.add(rest)
            elif action in ("edit", "status"):
                number_text, _, value = rest.partition(" ")
                number = _step_number(number_text)
                if action == "edit":
                    plan.edit(number, value)
                else:
                    plan.set_status(number, parse_status(value.strip()))
            elif action == "remove":
                plan.remove(_step_number(rest))
            elif action == "clear":
                plan.clear()
            else:
                return PLAN_COMMAND_HELP
        except PlanError as exc:
            return f"plan not changed: {exc}"
        plan.sync()
    return plan.render() if plan.steps else "The plan is empty."


UPDATE_PLAN_SPEC = ToolSpec(
    name="update_plan",
    description=(
        "Create or replace the task plan: a short checklist of steps, each with a status "
        "(pending, in_progress, completed). Send the full list every time; it replaces the "
        "previous plan. At most one step can be in_progress. The user can see and edit the plan."
    ),
    parameters={
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "The steps of the plan, in order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "step": {"type": "string", "description": "What the step does."},
                        "status": {"type": "string", "enum": [s.value for s in StepStatus]},
                    },
                    "required": ["step", "status"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["items"],
        "additionalProperties": False,
    },
)


class UpdatePlanTool:
    spec = UPDATE_PLAN_SPEC

    def __init__(self, plan: TaskPlan, after_tool_calls: Callable[[Callable[[], None]], None]):
        self.plan = plan
        self.after_tool_calls = after_tool_calls

    def run(self, arguments: dict[str, Any]) -> str:
        raw_items = arguments.get("items")
        if not isinstance(raw_items, list):
            raise ToolError("'items' must be a list of steps")
        steps = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise ToolError("each item must be an object with 'step' and 'status'")
            text = raw.get("step")
            if not isinstance(text, str) or not text.strip():
                raise ToolError("each item needs a non-empty 'step'")
            try:
                steps.append(PlanStep(text.strip(), parse_status(raw.get("status"))))
            except PlanError as exc:
                raise ToolError(str(exc)) from exc
        try:
            self.plan.replace(steps)
        except PlanError as exc:
            raise ToolError(str(exc)) from exc
        self.after_tool_calls(self.plan.sync)
        return "Plan updated."
