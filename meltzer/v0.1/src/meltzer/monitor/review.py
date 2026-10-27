"""Automatic approval review (approvals_reviewer = "monitor").

Each approval request starts a fresh review in a new model conversation (see
meltzer.monitor.prompt). The reviewer may call read-only tools and then answers
with a JSON assessment. The action runs only if the assessment's outcome is
"allow"; a review that fails (model error, an answer that cannot be parsed)
denies it. Every review is written to the session recording.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from meltzer.approval.policy import ApprovalRequest, ReviewVerdict
from meltzer.client import ChatClient, ModelError, ModelResponse
from meltzer.config.schema import Config
from meltzer.history import History, SessionRecorder
from meltzer.monitor.prompt import review_messages
from meltzer.monitor.tools import review_tools
from meltzer.ui import TerminalUI

# Model calls per review, including the ones that only run reviewer tools.
MAX_REVIEW_STEPS = 10

RISK_LEVELS = ("low", "medium", "high", "critical")
AUTHORIZATION_LEVELS = ("unknown", "low", "medium", "high")
OUTCOMES = ("allow", "deny")


class AssessmentError(ValueError):
    """The review did not produce a usable assessment."""


@dataclass(frozen=True)
class Assessment:
    risk_level: str
    user_authorization: str
    outcome: str
    rationale: str


def _field(payload: dict[str, Any], name: str, allowed: tuple[str, ...], default: str) -> str:
    value = payload.get(name, default)
    if value not in allowed:
        raise AssessmentError(f"invalid {name}: {value!r}")
    return value


def parse_assessment(text: str | None) -> Assessment:
    """Parse the reviewer's final message. A JSON object wrapped in prose is accepted."""
    if not text or not text.strip():
        raise AssessmentError("the review ended without an assessment")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise AssessmentError("the assessment is not valid JSON") from None
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            raise AssessmentError("the assessment is not valid JSON") from None
    if not isinstance(payload, dict):
        raise AssessmentError("the assessment is not a JSON object")
    if "outcome" not in payload:
        raise AssessmentError("the assessment has no outcome")
    outcome = _field(payload, "outcome", OUTCOMES, "deny")
    risk_level = _field(payload, "risk_level", RISK_LEVELS, "low" if outcome == "allow" else "high")
    authorization = _field(payload, "user_authorization", AUTHORIZATION_LEVELS, "unknown")
    rationale = payload.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        rationale = "low-risk action" if outcome == "allow" else "denied without a rationale"
    return Assessment(risk_level, authorization, outcome, rationale.strip())


class ReviewedSession(Protocol):
    """The parts of a session a review reads."""

    config: Config
    root: Path
    history: History
    recorder: SessionRecorder
    client: ChatClient
    ui: TerminalUI


def _assistant_message(response: ModelResponse) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": response.content}
    message.update(response.reasoning)
    if response.tool_calls:
        message["tool_calls"] = [
            {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": call.arguments}}
            for call in response.tool_calls
        ]
    return message


class MonitorReviewer:
    def __init__(self, session: ReviewedSession):
        self.session = session

    @property
    def model(self) -> str:
        return self.session.config.monitor_model or self.session.config.model

    def _assess(self, request: ApprovalRequest) -> tuple[Assessment, int]:
        """Run one review conversation. Returns the assessment and the number of tool calls."""
        session = self.session
        tools = review_tools(session.root)
        messages = review_messages(
            session.root,
            list(session.history),
            request,
            session.config.monitor_transcript_items,
        )
        tool_calls = 0
        for _ in range(MAX_REVIEW_STEPS):
            response = session.client.complete(messages, tools.specs(), model=self.model)
            if not response.tool_calls:
                return parse_assessment(response.content), tool_calls
            messages.append(_assistant_message(response))
            for call in response.tool_calls:
                output = tools.dispatch(call.name, call.arguments)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": output.text})
                tool_calls += 1
        raise AssessmentError(f"the review did not finish within {MAX_REVIEW_STEPS} model calls")

    def review(self, request: ApprovalRequest) -> ReviewVerdict:
        review_id = f"review_{uuid.uuid4().hex[:12]}"
        record: dict[str, Any] = {
            "review_id": review_id,
            "reviewer": "monitor",
            "model": self.model,
            "action": request.planned_action(),
        }
        try:
            assessment, tool_calls = self._assess(request)
        except (ModelError, AssessmentError, OSError) as exc:
            self.session.recorder.event("review", {**record, "outcome": "deny", "error": str(exc)})
            self.session.ui.info(f"auto-review: deny (review failed: {exc})")
            return ReviewVerdict(approved=False, rationale=f"automatic review failed: {exc}")

        self.session.recorder.event("review", {**record, **asdict(assessment), "tool_calls": tool_calls})
        self.session.ui.info(
            f"auto-review: {assessment.outcome} (risk: {assessment.risk_level}, "
            f"authorization: {assessment.user_authorization}): {assessment.rationale}"
        )
        return ReviewVerdict(
            approved=assessment.outcome == "allow",
            rationale=f"automatic review: {assessment.outcome}: {assessment.rationale}",
        )
