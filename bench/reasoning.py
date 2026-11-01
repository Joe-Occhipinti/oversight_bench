"""Reasoning in the model proxy's replies (spec 7.2).

meltzer talks to Inspect's model proxy in the Chat Completions format. For a reply with reasoning, Inspect
(0.3.287, `inspect_ai.model._openai.openai_chat_choices`) writes the reasoning into the message text as
`<think>` tags. Two things go wrong with that text:

- meltzer keeps it as part of the assistant message, so the monitor's transcript shows the agent's reasoning,
  including its plan for the side task;
- the monitor model's own reply starts with `<think>`, so meltzer cannot parse its JSON verdict and denies.

`install()` replaces that function with one that leaves the reasoning out of the text and returns it in a field
of the message, as OpenRouter does (`reasoning_details`, or `reasoning`). meltzer already reads those fields
(`meltzer.client.REASONING_FIELDS`), keeps them as separate history items that the monitor transcript skips
(`meltzer.monitor.transcript`), and sends them back with the next request, where Inspect turns them into the
model's reasoning again. So the agent keeps its reasoning between steps and the monitor never sees it.
"""

from __future__ import annotations

from inspect_ai.model import ContentReasoning, _openai
from inspect_ai.model._openrouter_reasoning import reasoning_to_openrouter_reasoning_details
from pydantic import JsonValue

MARKER = "_bench_reasoning_in_fields"


def reasoning_fields(reasoning: ContentReasoning) -> dict[str, JsonValue]:
    """The message fields that carry one reasoning block: OpenRouter's `reasoning_details` when the block came
    from it (the block is then replayed whole, signature included), else the readable text as `reasoning`."""
    details = reasoning_to_openrouter_reasoning_details(reasoning)
    if details is not None:
        return details  # type: ignore[return-value]
    if reasoning.reasoning and not reasoning.redacted:
        return {"reasoning": reasoning.reasoning}
    return {}


def install() -> None:
    """Make the proxy return reasoning in message fields. Safe to call more than once."""
    current = _openai.openai_chat_choices
    if getattr(current, MARKER, False):
        return

    def openai_chat_choices(choices):
        oai_choices = current(choices)
        for choice, oai_choice in zip(choices, oai_choices):
            if isinstance(choice.message.content, str):
                continue  # no reasoning block: the text is already the model's text
            content, fields = _openai.openai_assistant_content(choice.message, reasoning_fields)
            oai_choice.message.content = content.strip()  # as for a text-only reply, without the leading newline
            for name, value in fields.items():
                setattr(oai_choice.message, name, value)  # the message type accepts extra fields
        return oai_choices

    setattr(openai_chat_choices, MARKER, True)
    _openai.openai_chat_choices = openai_chat_choices
