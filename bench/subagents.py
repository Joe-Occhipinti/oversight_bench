"""Subagents do not get the developer instructions (spec 7.2).

meltzer gives a subagent the same system message as its parent, which ends with the developer instructions
(`meltzer.session.Session.__init__`: `parent.developer_instructions`, appended after `# Developer instructions`).
Here those are the attack prompt and the exploit cards, so a subagent would be a second red-team agent. The
subagent is meant to know only meltzer's own system instructions and the prompt its parent wrote for it.

`without_developer_instructions` is a filter for the model bridge: it sees each request before the model does and
cuts the developer instructions out of the system message of a subagent's request. meltzer and the source in
`/opt/meltzer` stay as they are. The filter tells a subagent from the main session by its tools: meltzer
registers `spawn_agent` only for the main session (`MAX_AGENT_DEPTH` is 1). Requests without the developer
instructions in the system message, such as the monitor's, pass unchanged.
"""

from __future__ import annotations

from inspect_ai.model import ChatMessage, GenerateConfig, GenerateFilter, GenerateInput, Model
from inspect_ai.tool import ToolChoice, ToolInfo

SPAWN_TOOL = "spawn_agent"
HEADING = "\n# Developer instructions\n"  # as in `Session.__init__`: `self.instructions += HEADING + text`


def without_developer_instructions(developer_instructions: str) -> GenerateFilter:
    """The filter for `sandbox_agent_bridge(filter=...)`; `developer_instructions` is the text given to meltzer."""
    section = HEADING + developer_instructions

    async def cut(
        model: Model,
        messages: list[ChatMessage],
        tools: list[ToolInfo],
        tool_choice: ToolChoice | None,
        config: GenerateConfig,
    ) -> GenerateInput | None:
        if any(tool.name == SPAWN_TOOL for tool in tools):
            return None  # the main session
        if not messages or messages[0].role != "system" or section not in messages[0].text:
            return None
        system = messages[0].model_copy(update={"content": messages[0].text.replace(section, "", 1)})
        return GenerateInput([system, *messages[1:]], tools, tool_choice, config)

    return cut
