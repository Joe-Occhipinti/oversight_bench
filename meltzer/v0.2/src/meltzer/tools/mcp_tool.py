"""Tools from MCP servers, one `mcp__<server>__<tool>` tool per server tool.

Whether a call is reviewed depends on the tool's approval mode:
  auto     reviewed if the server marks the tool destructive; runs without review
           if it marks it read-only, or neither destructive nor open-world;
           reviewed when the server gives no hints
  writes   reviewed unless the server marks the tool read-only
  prompt   always reviewed
  approve  runs without review
With approval_policy = never and no sandbox (full-access), calls run without review.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from meltzer.approval.orchestrator import ApprovalOrchestrator, McpCallRequest
from meltzer.approval.policy import ApprovalPolicy
from meltzer.mcp.client import McpError
from meltzer.mcp.config import ToolApprovalMode
from meltzer.mcp.manager import McpManager, McpToolInfo
from meltzer.tools.registry import Tool, ToolError, ToolSpec
from meltzer.utils.truncate import truncate_output


def _hint(annotations: dict[str, Any] | None, name: str) -> bool | None:
    value = (annotations or {}).get(name)
    return value if isinstance(value, bool) else None


def requires_approval(annotations: dict[str, Any] | None, mode: ToolApprovalMode) -> bool:
    """Whether a call to a tool with these annotations needs review in this approval mode."""
    if mode == ToolApprovalMode.PROMPT:
        return True
    if mode == ToolApprovalMode.APPROVE:
        return False
    read_only = _hint(annotations, "readOnlyHint") is True
    if mode == ToolApprovalMode.WRITES:
        return not read_only
    destructive = _hint(annotations, "destructiveHint")
    if destructive is True:
        return True
    if read_only:
        return False
    open_world = _hint(annotations, "openWorldHint")
    return destructive is not False or open_world is not False


def needs_review(info: McpToolInfo, orchestrator: ApprovalOrchestrator) -> bool:
    if info.approval_mode == ToolApprovalMode.APPROVE:
        return False
    if orchestrator.settings.policy == ApprovalPolicy.NEVER and not orchestrator.sandbox.is_sandboxed:
        return False
    return requires_approval(info.annotations, info.approval_mode)


def _content_text(block: Any) -> str:
    if not isinstance(block, dict):
        return ""
    kind = block.get("type")
    if kind == "text":
        return str(block.get("text", ""))
    if kind in ("image", "audio"):
        return f"[{kind} content: {block.get('mimeType', 'unknown type')}]"
    if kind == "resource":
        resource = block.get("resource") or {}
        if isinstance(resource.get("text"), str):
            return resource["text"]
        return f"[resource: {resource.get('uri', '')}]"
    if kind == "resource_link":
        return f"[resource: {block.get('uri', '')}]"
    return f"[{kind} content]"


def format_result(result: dict[str, Any]) -> str:
    """The text the model gets for a `tools/call` result.

    Structured content is returned as JSON; otherwise the content blocks are
    joined, one per line. A result marked `isError` raises ToolError.
    """
    content = result.get("content")
    blocks = content if isinstance(content, list) else []
    text = "\n".join(_content_text(block) for block in blocks)
    if result.get("isError") is True:
        raise ToolError(truncate_output(text or "the tool reported an error"))
    structured = result.get("structuredContent")
    if structured is not None:
        text = json.dumps(structured, ensure_ascii=False)
    return truncate_output(text)


class McpTool(Tool):
    def __init__(self, info: McpToolInfo, manager: McpManager, orchestrator: ApprovalOrchestrator, root: Path):
        self.info = info
        self.manager = manager
        self.orchestrator = orchestrator
        self.root = root
        self.spec = ToolSpec(name=info.model_name, description=info.description, parameters=info.input_schema)

    def run(self, arguments: dict[str, Any]) -> str:
        request = McpCallRequest(
            server=self.info.server,
            tool=self.info.name,
            arguments=arguments,
            cwd=self.root,
            description=self.info.description,
            annotations=self.info.annotations,
            needs_review=needs_review(self.info, self.orchestrator),
        )
        decision = self.orchestrator.decide_mcp_call(request)
        if not decision.run:
            raise ToolError(f"MCP tool call not run: {decision.reason}")
        try:
            result = self.manager.call_tool(self.info.server, self.info.name, arguments)
        except McpError as exc:
            raise ToolError(f"MCP tool call failed: {exc}") from None
        return format_result(result)


def mcp_tools(manager: McpManager, orchestrator: ApprovalOrchestrator, root: Path) -> list[McpTool]:
    """One tool per server tool, for a session's tool registry."""
    return [McpTool(info, manager, orchestrator, root) for info in manager.tools]
