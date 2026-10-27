"""The conversation commands: chat, ask, turns, serve. Kept apart from cli.py so that one stays thin."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from jarvis.channels.terminal import TerminalChannel
from jarvis import node_cli
from jarvis.core import loop, sessions
from jarvis.core.apps import load_apps, node_app_settings
from jarvis.core.node_hub import NodeHub
from jarvis.core.brain import Brain
from jarvis.core.brain_sdk import SdkBrain
from jarvis.core.channel import Incoming
from jarvis.core.settings import Settings
from jarvis.host import Host, build_channels


FIXTURE_APPS = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "apps"


def _brain(settings: Settings) -> Brain:
    return SdkBrain(settings.model, settings.max_turns, settings.data_dir / "scratch")


def _dim(text: str) -> str:
    return f"\033[2m{text}\033[0m" if sys.stdout.isatty() else text


def _show_tools(incoming: Incoming, reply: loop.Reply) -> None:
    if reply.tools:
        print(_dim("  used: " + ", ".join(reply.tools)))


def ask(settings: Settings, message: str, brain: Brain | None = None) -> int:
    host = Host(load_apps(settings), brain or _brain(settings), [TerminalChannel()])
    try:
        reply = asyncio.run(host.turn(Incoming("cli", message)))
    finally:
        host.close()
    _show_tools(Incoming("cli", message), reply)
    return 1 if reply.error else 0


def chat(settings: Settings, brain: Brain | None = None) -> int:
    host = Host(load_apps(settings), brain or _brain(settings), [TerminalChannel()], on_reply=_show_tools)
    print(_dim(f"{settings.assistant_name} is listening. Ctrl-D or 'bye' to leave."))
    asyncio.run(host.run(handle_signals=True))
    return 0


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


def serve(settings: Settings, with_fixtures: bool = False, with_node: bool = False, brain: Brain | None = None) -> int:
    """Run Falak as a long-lived process hosting every enabled channel (Discord) until Ctrl-C."""
    extra = (FIXTURE_APPS,) if with_fixtures else ()
    if with_fixtures and not FIXTURE_APPS.is_dir():
        print(f"--with-fixtures needs {FIXTURE_APPS}, which is only in a source checkout", file=sys.stderr)
        return 2
    reg = load_apps(settings, extra_dirs=extra)
    sessions.abandon_open(reg, "host restarted")  # a host that died left its views open on paper only
    hub = NodeHub(settings, lambda: node_app_settings(reg))
    try:
        hub.start()
    except RuntimeError as e:
        reg.close()
        print(str(e), file=sys.stderr)
        return 1
    reg.nodes = hub
    host = Host(reg, brain or _brain(settings))
    for channel in build_channels(settings, host):
        host.add_channel(channel)
    if not host.channels:
        host.close()
        print("No channel is enabled, so there is nothing to serve. See docs/briefs/M4.md for the Discord setup.",
              file=sys.stderr)
        return 1
    child = node_cli.start_child(settings) if with_node else None
    try:
        asyncio.run(host.run(handle_signals=True))
    finally:
        if child is not None:
            node_cli.stop_child(child)
    return 0
