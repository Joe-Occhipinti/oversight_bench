"""The `request_user_input` tool: ask the user one to three multiple-choice questions.

The session registers it only in the top-level session of an interactive run.
For each question the user picks one of the options or types an answer of
their own ("Other"). The answers are the tool result:
`{"answers": {"<question id>": {"answers": ["<answer>"]}}}`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from meltzer.tools.registry import Tool, ToolError, ToolSpec

MAX_QUESTIONS = 3
MIN_OPTIONS = 2
CANCELLED = "request_user_input was cancelled before receiving a response"


@dataclass(frozen=True)
class Option:
    label: str
    description: str


@dataclass(frozen=True)
class Question:
    id: str
    header: str
    question: str
    options: tuple[Option, ...]


def _string_schema(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}


REQUEST_USER_INPUT_SPEC = ToolSpec(
    name="request_user_input",
    description=(
        "Ask the user one to three short questions and wait for the answers. Each question offers "
        "a few options; the user can also type an answer of their own."
    ),
    parameters={
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "description": "Questions to show the user. Prefer 1 and do not exceed 3.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": _string_schema("Stable identifier for mapping answers (snake_case)."),
                        "header": _string_schema("Short header label shown in the UI (12 or fewer chars)."),
                        "question": _string_schema("Single-sentence prompt shown to the user."),
                        "options": {
                            "type": "array",
                            "description": (
                                "2-3 mutually exclusive choices. Do not include an \"Other\" option; "
                                "the user can always type their own answer."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": _string_schema("User-facing label (1-5 words)."),
                                    "description": _string_schema("One short sentence on the impact of this choice."),
                                },
                                "required": ["label", "description"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["id", "header", "question", "options"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["questions"],
        "additionalProperties": False,
    },
)


def _text(entry: dict[str, Any], key: str, where: str, *, allow_empty: bool = False) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ToolError(f"{where}: '{key}' must be a non-empty string")
    return value


def parse_questions(arguments: dict[str, Any]) -> list[Question]:
    """The questions of a call, validated."""
    entries = arguments.get("questions")
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_QUESTIONS:
        raise ToolError(f"'questions' must be a list of 1 to {MAX_QUESTIONS} questions")
    questions: list[Question] = []
    for index, entry in enumerate(entries):
        where = f"question {index + 1}"
        if not isinstance(entry, dict):
            raise ToolError(f"{where} must be an object")
        options = entry.get("options")
        if not isinstance(options, list) or len(options) < MIN_OPTIONS:
            raise ToolError(f"{where}: 'options' must be a list of at least {MIN_OPTIONS} options")
        parsed = []
        for number, option in enumerate(options):
            if not isinstance(option, dict):
                raise ToolError(f"{where}, option {number + 1} must be an object")
            label = _text(option, "label", f"{where}, option {number + 1}")
            parsed.append(Option(label, _text(option, "description", f"{where}, option {number + 1}", allow_empty=True)))
        question = Question(
            _text(entry, "id", where), _text(entry, "header", where), _text(entry, "question", where), tuple(parsed)
        )
        if any(earlier.id == question.id for earlier in questions):
            raise ToolError(f"{where}: duplicate id {question.id!r}")
        questions.append(question)
    return questions


class RequestUserInputTool(Tool):
    spec = REQUEST_USER_INPUT_SPEC

    def __init__(self, ask: Callable[[list[Question]], dict[str, list[str]] | None]):
        # The interface's ask_questions: answers by question id, or None if the user gave none.
        self.ask = ask

    def run(self, arguments: dict[str, Any]) -> str:
        questions = parse_questions(arguments)
        answers = self.ask(questions)
        if answers is None:
            raise ToolError(CANCELLED)
        result = {question.id: {"answers": answers.get(question.id, [])} for question in questions}
        return json.dumps({"answers": result}, ensure_ascii=False)
