"""Registry actions as brain tools, plus the core tools that are how the loop itself works."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field, replace

from jarvis.core import actions
from jarvis.core.actions import Origin
from jarvis.core.apps import SEP, Registry
from jarvis.core.brain import Image, Tool, ToolReply

CORE = "falak"  # core tools are falak__<name>
OWN_CHANNELS = frozenset({"cli"})  # channels only Elijah can type into; M4 adds his others


@dataclass
class TurnState:
    origin: Origin  # becomes outside_content=True once an untrusted_output action has run
    last_ask_id: int  # asks with a higher id were created during this turn
    tools_used: list[str] = field(default_factory=list)


def latest_ask_id(reg: Registry) -> int:
    return reg.conn.execute("SELECT COALESCE(MAX(id), 0) FROM asks").fetchone()[0]


def _reply(outcome: actions.Ran | actions.Asked | actions.Failed | actions.Declined) -> ToolReply:
    if isinstance(outcome, actions.Ran):
        try:
            images = tuple(
                Image(a.mime, base64.b64encode(a.path.read_bytes()).decode())
                for a in outcome.result.attachments
                if a.kind == "image"
            )
        finally:
            actions.release(outcome)
        return ToolReply(outcome.result.text, images)
    if isinstance(outcome, actions.Asked):
        return ToolReply(f"Waiting on Elijah's answer (ask #{outcome.ask_id}): {outcome.question}")
    if isinstance(outcome, actions.Declined):
        return ToolReply(f"Declined ask #{outcome.ask_id}.")
    return ToolReply(outcome.message, is_error=True)


def _noted(reg: Registry, turn: TurnState, tool_name: str, outcome: object) -> None:
    """Once an action whose output carries someone else's words has run, the rest of the turn is outside-driven."""
    found = reg.find(tool_name)
    if isinstance(outcome, actions.Ran) and found and found[1].untrusted_output:
        turn.origin = replace(turn.origin, outside_content=True)


def _app_tool(reg: Registry, turn: TurnState, spec) -> Tool:
    async def call(args: dict) -> ToolReply:
        turn.tools_used.append(spec.name)
        outcome = actions.run(reg, spec.name, args, turn.origin)
        _noted(reg, turn, spec.name, outcome)
        return _reply(outcome)

    return Tool(spec.name, spec.description, spec.input_schema, call)


def _refusal(turn: TurnState, ask_id: int, exists: bool) -> str | None:
    """Why this turn may not answer asks for Elijah, or None. All three guards must pass."""
    if turn.origin.channel not in OWN_CHANNELS:
        return "This message didn't come through one of Elijah's own channels, so it can't approve anything."
    if turn.origin.outside_content:
        return "This turn has read outside content (a photo, page or message), so it can't approve anything."
    if exists and ask_id > turn.last_ask_id:
        return "That ask was created during this turn, so this turn can't approve it."
    return None


def _answer_ask_tool(reg: Registry, turn: TurnState) -> Tool:
    async def call(args: dict) -> ToolReply:
        turn.tools_used.append(f"{CORE}{SEP}answer_ask")
        ask_id, yes = args.get("ask_id"), args.get("yes")
        if not isinstance(ask_id, int) or isinstance(ask_id, bool) or not isinstance(yes, bool):
            return ToolReply("answer_ask needs an integer ask_id and a true/false yes.", is_error=True)
        row = reg.conn.execute("SELECT tool FROM asks WHERE id=?", (ask_id,)).fetchone()
        why = _refusal(turn, ask_id, exists=row is not None)
        if why:
            return ToolReply(f"{why} Ask Elijah directly and wait for his reply.", is_error=True)
        outcome = actions.answer_ask(reg, ask_id, yes)
        if row:
            _noted(reg, turn, row["tool"], outcome)
        return _reply(outcome)

    schema = {
        "type": "object",
        "required": ["ask_id", "yes"],
        "additionalProperties": False,
        "properties": {
            "ask_id": {"type": "integer", "description": "The number of the pending ask."},
            "yes": {"type": "boolean", "description": "true to approve it, false to decline it."},
        },
    }
    desc = ("Approve or decline a pending ask, on Elijah's say-so. Only call it when his message in this "
            "turn clearly answers that ask (\"yeah, send it\" is yes; \"no, don't\" is no).")
    return Tool(f"{CORE}{SEP}answer_ask", desc, schema, call)


def _undo_tool(reg: Registry, turn: TurnState) -> Tool:
    async def call(args: dict) -> ToolReply:
        turn.tools_used.append(f"{CORE}{SEP}undo")
        log_id = args.get("log_id")
        if log_id is not None and (not isinstance(log_id, int) or isinstance(log_id, bool)):
            return ToolReply("log_id must be an integer.", is_error=True)
        return _reply(actions.undo(reg, log_id))

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"log_id": {"type": "integer", "description": "A recent action's number; leave out for the latest."}},
    }
    desc = "Undo the most recent undoable action (or the one with the given log number)."
    return Tool(f"{CORE}{SEP}undo", desc, schema, call)


def build_tools(reg: Registry, turn: TurnState) -> list[Tool]:
    tools = [_app_tool(reg, turn, spec) for spec in reg.actions()]
    return [*tools, _answer_ask_tool(reg, turn), _undo_tool(reg, turn)]
