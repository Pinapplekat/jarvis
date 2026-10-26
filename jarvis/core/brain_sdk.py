"""The Claude Agent SDK behind the Brain interface. The only module that imports claude_agent_sdk."""

from __future__ import annotations

import logging
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ResultMessage, SdkMcpTool, ToolUseBlock, create_sdk_mcp_server, query,
)

from jarvis.core.brain import BrainResult, Tool

log = logging.getLogger("jarvis.core.brain")
logging.getLogger("claude_agent_sdk").setLevel(logging.WARNING)  # its INFO lines would print on every turn

SERVER = "falak"
PREFIX = f"mcp__{SERVER}__"


def _sdk_tool(tool: Tool) -> SdkMcpTool:
    async def handler(args: dict) -> dict:
        reply = await tool.call(args)
        content = [{"type": "text", "text": reply.text}]
        content += [{"type": "image", "data": i.data, "mimeType": i.mime} for i in reply.images]
        return {"content": content, "is_error": reply.is_error}

    # The SDK only passes a schema through as JSON Schema when it has "properties"; otherwise it reads the
    # dict as name -> type pairs, so a no-argument action would demand "type" and "additionalProperties".
    schema = {"properties": {}, **tool.input_schema}
    return SdkMcpTool(name=tool.name, description=tool.description, input_schema=schema, handler=handler)


class SdkBrain:
    def __init__(self, model: str, max_turns: int, cwd: Path) -> None:
        self.model, self.max_turns, self.cwd = model, max_turns, cwd

    def _options(self, system: str, tools: list[Tool]) -> ClaudeAgentOptions:
        self.cwd.mkdir(parents=True, exist_ok=True)
        return ClaudeAgentOptions(
            tools=[],  # no built-in tools: Falak can only use its own
            setting_sources=[],  # none of Elijah's Claude Code settings, hooks or CLAUDE.md files
            strict_mcp_config=True,
            mcp_servers={SERVER: create_sdk_mcp_server(SERVER, tools=[_sdk_tool(t) for t in tools])},
            allowed_tools=[f"{PREFIX}*"],
            system_prompt=system,
            model=self.model,
            max_turns=self.max_turns,
            cwd=self.cwd,
            stderr=lambda line: log.debug("claude: %s", line.rstrip()),
        )

    async def run(self, system: str, prompt: str, tools: list[Tool]) -> BrainResult:
        result = BrainResult("")
        async for message in query(prompt=prompt, options=self._options(system, tools)):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        result.tool_calls.append(block.name.removeprefix(PREFIX))
            elif isinstance(message, ResultMessage):
                result.text = message.result or ""
                result.num_turns = message.num_turns
                result.cost_usd = message.total_cost_usd
                if message.is_error:
                    detail = "; ".join(message.errors or []) or message.result or message.subtype
                    result.error = f"{message.subtype}: {detail}"[:200]
        return result
