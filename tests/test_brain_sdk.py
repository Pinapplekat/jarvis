"""SdkBrain's wiring, checked without calling Claude."""

import asyncio
from pathlib import Path

from jarvis.core.brain import Image, Tool, ToolReply
from jarvis.core.brain_sdk import PREFIX, SdkBrain, _sdk_tool


def tool(reply):
    async def call(args):
        return reply

    return Tool("echo__shout", "Shout.", {"type": "object", "properties": {}}, call)


def test_tool_results_carry_text_images_and_the_error_flag():
    reply = ToolReply("Took it", (Image("image/jpeg", "anBlZw=="),))
    out = asyncio.run(_sdk_tool(tool(reply)).handler({}))
    assert out == {
        "content": [{"type": "text", "text": "Took it"}, {"type": "image", "data": "anBlZw==", "mimeType": "image/jpeg"}],
        "is_error": False,
    }
    assert asyncio.run(_sdk_tool(tool(ToolReply("bad", is_error=True))).handler({}))["is_error"] is True


def test_a_no_argument_schema_reaches_the_model_as_json_schema():
    no_args = Tool("camera__take_photo", "Take a photo.", {"type": "object", "additionalProperties": False}, None)
    schema = _sdk_tool(no_args).input_schema
    assert schema == {"type": "object", "additionalProperties": False, "properties": {}}
    from claude_agent_sdk import _build_input_schema  # what the SDK actually sends the model
    assert _build_input_schema(_sdk_tool(no_args)) == schema


def test_options_isolate_the_brain_from_everything_but_falak_tools(tmp_path):
    brain = SdkBrain("sonnet", 7, tmp_path / "scratch")
    o = brain._options("be calm", [tool(ToolReply("x"))])
    assert o.tools == [] and o.setting_sources == [] and o.strict_mcp_config is True
    assert o.allowed_tools == [f"{PREFIX}*"] and list(o.mcp_servers) == ["falak"]
    assert (o.system_prompt, o.model, o.max_turns) == ("be calm", "sonnet", 7)
    assert Path(o.cwd) == tmp_path / "scratch" and Path(o.cwd).is_dir()
    assert "ANTHROPIC_API_KEY" not in o.env
    assert o.extra_args == {"no-session-persistence": None}  # no transcripts or photo copies on disk
