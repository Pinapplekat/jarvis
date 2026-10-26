"""The conversation commands: chat, ask, turns. Kept apart from cli.py so that one stays thin."""

from __future__ import annotations

import asyncio
import json
import sys

from jarvis.core import loop
from jarvis.core.actions import Origin
from jarvis.core.apps import load_apps
from jarvis.core.brain import Brain
from jarvis.core.brain_sdk import SdkBrain
from jarvis.core.settings import Settings

CLI = Origin(channel="cli")


def _brain(settings: Settings) -> Brain:
    return SdkBrain(settings.model, settings.max_turns, settings.data_dir / "scratch")


def _dim(text: str) -> str:
    return f"\033[2m{text}\033[0m" if sys.stdout.isatty() else text


def _say(reply: loop.Reply, show_tools: bool) -> None:
    print(reply.text)
    if show_tools and reply.tools:
        print(_dim("  used: " + ", ".join(reply.tools)))


def ask(settings: Settings, message: str, brain: Brain | None = None) -> int:
    reg = load_apps(settings)
    try:
        reply = asyncio.run(loop.handle_message(reg, brain or _brain(settings), message, CLI))
    finally:
        reg.close()
    _say(reply, show_tools=True)
    return 1 if reply.error else 0


def chat(settings: Settings, brain: Brain | None = None) -> int:
    reg = load_apps(settings)
    brain = brain or _brain(settings)
    print(_dim(f"{settings.assistant_name} is listening. Ctrl-D or 'bye' to leave."))
    try:
        while True:
            try:
                message = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if message.lower() in ("bye", "exit", "quit"):
                return 0
            if message:
                _say(asyncio.run(loop.handle_message(reg, brain, message, CLI)), show_tools=True)
    finally:
        reg.close()


def turns(settings: Settings, n: int) -> int:
    reg = load_apps(settings)
    try:
        rows = reg.conn.execute("SELECT * FROM turns ORDER BY id DESC LIMIT ?", (n,)).fetchall()[::-1]
    finally:
        reg.close()
    for r in rows:
        cost = f" ${r['cost_usd']:.4f}" if r["cost_usd"] is not None else ""
        used = ", ".join(json.loads(r["tools_json"])) or "no tools"
        err = f"  ERROR: {r['error']}" if r["error"] else ""
        print(f"#{r['id']} {r['at']} {r['channel']} {r['ms'] / 1000:.1f}s {r['model']} "
              f"({r['num_turns']} steps{cost}) {used}{err}")
    return 0
