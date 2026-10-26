"""One message in, one reply out: record, build the prompt, run the brain with the tools, record the turn."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from jarvis.core import context, conversation
from jarvis.core.actions import Origin
from jarvis.core.apps import Registry
from jarvis.core.brain import Brain, BrainResult
from jarvis.core.tools import TurnState, build_tools, latest_ask_id

log = logging.getLogger("jarvis.core.loop")


@dataclass(frozen=True)
class Reply:
    text: str
    tools: tuple[str, ...]
    error: str | None
    turn_id: int


def _fallback(error: str, tools: list[str]) -> str:
    text = f"I couldn't think just now, sir ({error})."
    if tools:
        text += f" Before that I ran: {', '.join(tools)}."
    return text


async def _think(brain: Brain, reg: Registry, turn: TurnState, system: str, prompt: str) -> BrainResult:
    limit = reg.settings.turn_timeout_seconds
    try:
        return await asyncio.wait_for(brain.run(system, prompt, build_tools(reg, turn)), limit)
    except asyncio.TimeoutError:
        return BrainResult("", error=f"timed out after {limit}s")
    except Exception as e:
        log.exception("the brain failed")
        return BrainResult("", error=f"{type(e).__name__}: {e}"[:200])


def _record_turn(reg: Registry, channel: str, ms: int, result: BrainResult, tools: list[str], error: str | None) -> int:
    return reg.conn.execute(
        "INSERT INTO turns (at, channel, ms, model, num_turns, tools_json, cost_usd, error) VALUES (?,?,?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), channel, ms, reg.settings.model,
         result.num_turns, json.dumps(tools), result.cost_usd, error),
    ).lastrowid


async def handle_message(reg: Registry, brain: Brain, text: str, origin: Origin, now: datetime | None = None) -> Reply:
    started = time.monotonic()
    message_id = conversation.add(reg.conn, "user", text, now)
    turn = TurnState(origin, latest_ask_id(reg))
    prompt = context.build_prompt(reg, text, origin.channel, message_id, now)
    result = await _think(brain, reg, turn, context.system_prompt(reg.settings.assistant_name), prompt)
    error = result.error or (None if result.text.strip() else "the model said nothing")
    reply = _fallback(error, turn.tools_used) if error else result.text.strip()
    conversation.add(reg.conn, "assistant", reply, now)
    turn_id = _record_turn(reg, origin.channel, int((time.monotonic() - started) * 1000), result, turn.tools_used, error)
    return Reply(reply, tuple(turn.tools_used), error, turn_id)
