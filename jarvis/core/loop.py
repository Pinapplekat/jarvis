"""One message in, one reply out: record, build the prompt, run the brain with the tools, record the turn."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, time as clock_time, timedelta, timezone

from jarvis.core import context, conversation, sessions
from jarvis.core.actions import Origin
from jarvis.contract.api import Attachment
from jarvis.core.apps import Registry
from jarvis.core.brain import Brain, BrainResult, Image
from jarvis.core.timeutil import stamp
from jarvis.core.tools import TurnState, build_tools, latest_ask_id, new_pending_asks, shared_images

log = logging.getLogger("jarvis.core.loop")


@dataclass(frozen=True)
class Reply:
    text: str
    tools: tuple[str, ...]
    error: str | None
    turn_id: int
    ms: int = 0
    new_asks: tuple[int, ...] = ()  # asks this turn created that are still pending
    images: tuple[Image, ...] = ()  # pictures the model chose to send with the reply (falak__send_image)


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


def thinking_runs_today(reg: Registry, now: datetime | None = None) -> int:
    """Thinking turns (channel job:...) so far this local day. Chat turns are never counted."""
    local = (now or datetime.now(timezone.utc)).astimezone(reg.settings.tz())
    start = datetime.combine(local.date(), clock_time(0), local.tzinfo)
    end = datetime.combine(local.date() + timedelta(days=1), clock_time(0), local.tzinfo)
    return reg.conn.execute("SELECT COUNT(*) FROM turns WHERE channel LIKE 'job:%' AND at >= ? AND at < ?",
                            (stamp(start), stamp(end))).fetchone()[0]


async def _run_turn(reg: Registry, brain: Brain, turn: TurnState, system: str,
                    prompt: str) -> tuple[BrainResult, str | None]:
    result = await _think(brain, reg, turn, system, prompt)
    return result, result.error or (None if result.text.strip() else "the model said nothing")


async def handle_message(reg: Registry, brain: Brain, text: str, origin: Origin, now: datetime | None = None, *,
                         own_channels: frozenset[str] = frozenset({"cli"}),
                         attachments: tuple[Attachment, ...] = ()) -> Reply:
    """Run one turn. The files in `attachments` are deleted when it ends, whether or not they were viewed."""
    started = time.monotonic()
    try:
        await asyncio.to_thread(sessions.sweep, reg, now)  # a view whose device left or sat idle closes before this turn
        message_id = conversation.add(reg.conn, "user", text, now)
        turn = TurnState(origin, latest_ask_id(reg), own_channels, attachments)
        images = sum(a.kind == "image" for a in attachments)
        prompt = context.build_prompt(reg, text, origin.channel, message_id, now, images)
        result, error = await _run_turn(reg, brain, turn, context.system_prompt(reg.settings.assistant_name), prompt)
        reply = _fallback(error, turn.tools_used) if error else result.text.strip()
        conversation.add(reg.conn, "assistant", reply, now)
        ms = int((time.monotonic() - started) * 1000)
        turn_id = _record_turn(reg, origin.channel, ms, result, turn.tools_used, error)
        return Reply(reply, tuple(turn.tools_used), error, turn_id, ms, new_pending_asks(reg, turn),
                     shared_images(turn))
    finally:
        for att in attachments:
            att.path.unlink(missing_ok=True)


async def handle_job(reg: Registry, brain: Brain, task: str, origin: Origin, now: datetime | None = None, *,
                     own_channels: frozenset[str] = frozenset({"cli"})) -> Reply:
    """A thinking turn for a job: the same loop, tools and policy, but nobody spoke. The conversation log is left
    alone (the host adds the reply once it is actually sent). Reply.text is empty when the turn failed."""
    started = time.monotonic()
    await asyncio.to_thread(sessions.sweep, reg, now)
    turn = TurnState(origin, latest_ask_id(reg), own_channels)
    prompt = context.build_prompt(reg, task, origin.channel, None, now, task=True)
    result, error = await _run_turn(reg, brain, turn, context.thinking_prompt(reg.settings.assistant_name), prompt)
    ms = int((time.monotonic() - started) * 1000)
    turn_id = _record_turn(reg, origin.channel, ms, result, turn.tools_used, error)
    return Reply("" if error else result.text.strip(), tuple(turn.tools_used), error, turn_id, ms,
                 new_pending_asks(reg, turn))
