"""Live views: the host's session table, with the device's pretend camera on the other end of a real WebSocket."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from node_harness import FIXTURES, NodeThread, eye_module, pair, start_hub, wait_for
from scripted_brain import ScriptedBrain
from test_node_link import link  # noqa: F401  (the fixture: a hub, a connected eye node, and a registry)

from jarvis.core import actions, context, loop, sessions
from jarvis.core.actions import Origin
from jarvis.core.apps import load_apps

CLI = Origin("cli")
START, LOOK, STOP = "eye__live_view_start", "eye__live_view_look", "eye__live_view_stop"


def rows(link):
    return [dict(r) for r in link.reg.conn.execute("SELECT * FROM sessions ORDER BY id")]


def start(link, **args):
    out = actions.run(link.reg, START, args, CLI)
    assert isinstance(out, actions.Ran), out
    return out


def test_tools_are_generated_for_each_session(link):
    names = [t.name for t in link.reg.actions() if t.app == "eye"]
    assert {START, LOOK, STOP} <= set(names)
    look = next(t for t in link.reg.actions() if t.name == LOOK)
    assert look.effect == "read" and look.untrusted_output and look.input_schema["required"] == ["session_id"]
    assert next(t for t in link.reg.actions() if t.name == START).input_schema["properties"]["name"]["default"] == "front"


def test_start_look_stop(link):
    eye = eye_module()
    out = start(link, name="desk")
    sid = out.result.data["session_id"]
    assert "Opened eye live_view #1" in out.result.text and "look 1 of desk" in out.result.text
    assert out.result.attachments[0].path.read_bytes() == b"frame-1"  # the first frame comes with the start
    actions.release(out)
    (row,) = rows(link)
    assert (row["app"], row["name"], row["stopped_at"]) == ("eye", "live_view", None) and row["node_id"]
    assert json.loads(row["state_json"])["name"] == "desk" and row["last_look_at"]

    again = actions.run(link.reg, LOOK, {"session_id": sid}, CLI)
    assert again.result.text == "look 2 of desk" and again.result.attachments[0].path.read_bytes() == b"frame-2"
    actions.release(again)
    assert len(eye.LIVE) == 1

    done = actions.run(link.reg, STOP, {"session_id": sid}, CLI)
    assert "Closed eye live_view #1" in done.result.text
    assert rows(link)[0]["stop_reason"] == "stopped" and eye.LIVE == {}
    gone = actions.run(link.reg, LOOK, {"session_id": sid}, CLI)
    assert isinstance(gone, actions.Failed) and "closed (stopped)" in gone.message
    assert list(link.tmp_host.iterdir()) == [] and list(link.node.config.tmp_dir.iterdir()) == []


def test_looks_and_starts_are_in_the_action_log(link):
    actions.release(start(link))
    log = actions.recent_log(link.reg)
    assert [(r["tool"], r["status"]) for r in log] == [(START, "ran")]


def test_only_one_view_of_a_kind_at_a_time(link):
    actions.release(start(link))
    out = actions.run(link.reg, START, {}, CLI)
    assert isinstance(out, actions.Failed) and "already open (#1)" in out.message


def test_unknown_session_and_wrong_id(link):
    out = actions.run(link.reg, LOOK, {"session_id": 9}, CLI)
    assert out.message == "There is no live_view session #9."


def test_idle_sessions_close(link):
    actions.release(start(link))
    soon = datetime.now(timezone.utc) + timedelta(minutes=4)
    assert sessions.sweep(link.reg, soon) == 0
    assert sessions.sweep(link.reg, datetime.now(timezone.utc) + timedelta(minutes=6)) == 1
    assert rows(link)[0]["stop_reason"] == "idle" and eye_module().LIVE == {}


def test_a_look_resets_the_idle_clock(link):
    out = start(link)
    sid = out.result.data["session_id"]
    actions.release(out)
    link.reg.conn.execute("UPDATE sessions SET started_at=?, last_look_at=?", (
        (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),) * 2)
    actions.release(actions.run(link.reg, LOOK, {"session_id": sid}, CLI))
    assert sessions.sweep(link.reg) == 0


def test_a_disconnecting_node_ends_its_sessions(link):
    actions.release(start(link))
    link.node.stop()
    wait_for(lambda: not link.hub.connected(), what="the disconnect")
    assert sessions.sweep(link.reg) == 1
    assert rows(link)[0]["stop_reason"] == "node disconnected"
    out = actions.run(link.reg, START, {}, CLI)
    assert out.message == "That needs eye, and no device with it is connected."


def test_a_look_after_the_node_left_closes_the_session(link):
    out = start(link)
    sid = out.result.data["session_id"]
    actions.release(out)
    link.node.stop()
    wait_for(lambda: not link.hub.connected(), what="the disconnect")
    gone = actions.run(link.reg, LOOK, {"session_id": sid}, CLI)
    assert "disconnected" in gone.message and rows(link)[0]["stop_reason"] == "node disconnected"


def test_a_reconnected_node_does_not_inherit_the_old_session(link, tmp_path):
    actions.release(start(link))
    link.node.stop()
    wait_for(lambda: not link.hub.connected(), what="the disconnect")
    new = NodeThread(tmp_path, link.hub.port, link.key).start()
    try:
        new.wait_connected()
        assert sessions.sweep(link.reg) == 1  # the old resource died with the old connection
        assert rows(link)[0]["stop_reason"] == "node disconnected"
    finally:
        new.stop()


def test_the_node_closes_live_resources_when_the_host_link_drops(link):
    actions.release(start(link))
    assert len(eye_module().LIVE) == 1
    link.hub.stop()
    wait_for(lambda: eye_module().LIVE == {}, what="the node to clean up its camera")


def test_stopping_the_host_stops_every_session(link):
    actions.release(start(link))
    assert sessions.stop_all(link.reg, "host stopped") == 1
    assert rows(link)[0]["stop_reason"] == "host stopped" and eye_module().LIVE == {}


def test_abandoned_rows_from_a_dead_host_are_closed(link):
    actions.release(start(link))
    assert sessions.abandon_open(link.reg, "host restarted") == 1
    assert rows(link)[0]["stop_reason"] == "host restarted"


def test_open_sessions_are_in_the_turn_context(link):
    actions.release(start(link))
    prompt = context.build_prompt(link.reg, "what about now?", "cli", 1)
    assert "Open live views:" in prompt
    assert "eye live_view #1" in prompt and "eye__live_view_look" in prompt and "last look just now" in prompt


def test_a_look_makes_the_rest_of_the_turn_outside_driven(link):
    out = start(link)
    sid = out.result.data["session_id"]
    actions.release(out)
    brain = ScriptedBrain([(LOOK, {"session_id": sid}), ("echo__send", {"text": "hi", "to": "Sam"})])
    reply = asyncio.run(loop.handle_message(link.reg, brain, "look and send", CLI))
    assert reply.new_asks  # the outbound send had to ask: the frame may carry someone else's words
    assert brain.replies[0].images and "Waiting on Elijah's answer" in brain.replies[1].text
    assert list(link.tmp_host.iterdir()) == []


def test_a_turn_sweeps_idle_sessions_first(link):
    actions.release(start(link))
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    link.reg.conn.execute("UPDATE sessions SET started_at=?, last_look_at=?", (old, old))
    brain = ScriptedBrain()
    asyncio.run(loop.handle_message(link.reg, brain, "hello", CLI))
    assert "Open live views" not in brain.prompts[0] and rows(link)[0]["stop_reason"] == "idle"


def test_a_failed_first_look_still_leaves_the_view_open(link):
    eye = eye_module()
    original = eye.sample_live
    eye.sample_live = lambda ctx, state: (_ for _ in ()).throw(eye.ActionError("lens cap on"))
    try:
        out = start(link)
    finally:
        eye.sample_live = original
    assert "first look failed (lens cap on)" in out.result.text and out.result.attachments == ()
    assert rows(link)[0]["stopped_at"] is None
