"""ctx.notify from a node: on disk before it is sent, resent after a dropped link, acked, and sent to Elijah once."""

import json
import socket

import pytest
from node_harness import FIXTURES, NodeThread, fake_node, pair, start_hub, wait_for

from jarvis.core import actions, outbox
from jarvis.core.apps import load_apps
from jarvis.protocol import messages as m

CLI = actions.Origin("cli")


@pytest.fixture
def reg(settings):
    r = load_apps(settings, FIXTURES)
    yield r
    r.close()


def rows(reg):
    return reg.conn.execute("SELECT * FROM outbox ORDER BY id").fetchall()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_an_event_is_in_the_outbox_once_even_when_the_node_sends_it_twice(reg):
    hub = start_hub(reg)
    try:
        key = pair(reg)
        ws, welcome = fake_node(hub.port, key)
        assert isinstance(welcome, m.Welcome)
        event = m.Event("abc123", "eye", "the job is done")
        for _ in range(2):
            ws.send(event.to_json())
            assert m.parse(ws.recv(timeout=5)) == m.EventAck("abc123")
        (row,) = rows(reg)
        assert row["text"] == "the job is done" and row["source"] == "app:eye" and row["sent_at"] is None
        ws.close()
    finally:
        hub.stop()


def test_an_event_for_an_app_the_node_did_not_announce_is_dropped_but_acked(reg):
    hub = start_hub(reg)
    try:
        ws, _ = fake_node(hub.port, pair(reg))
        ws.send(m.Event("e1", "camera", "hi").to_json())
        assert m.parse(ws.recv(timeout=5)) == m.EventAck("e1")
        assert rows(reg) == []
        ws.close()
    finally:
        hub.stop()


def test_event_text_is_capped(reg):
    hub = start_hub(reg)
    try:
        ws, _ = fake_node(hub.port, pair(reg))
        ws.send(m.Event("e2", "eye", "x" * 9000).to_json())
        ws.recv(timeout=5)
        assert len(rows(reg)[0]["text"]) == m.MAX_EVENT_TEXT
        ws.close()
    finally:
        hub.stop()


def test_a_message_written_while_the_host_is_away_is_sent_when_it_is_back_and_then_deleted(reg, tmp_path):
    port = free_port()
    key = pair(reg)
    node = NodeThread(tmp_path, port, key).start()
    try:
        node.runtime.configure({"eye": {"label": "plain"}})
        node.runtime.notify("eye", "first")
        node.runtime.notify("eye", "second")
        files = sorted((node.config.node_dir / "outbox").glob("*.json"))
        assert len(files) == 2 and json.loads(files[0].read_text())["app"] == "eye"  # on disk, nothing sent yet
        hub = start_hub(reg, port=port)
        try:
            node.wait_connected(10)
            wait_for(lambda: len(rows(reg)) == 2, 10, "both events to reach the outbox")
            assert [r["text"] for r in rows(reg)] == ["first", "second"]  # oldest first
            wait_for(lambda: not list((node.config.node_dir / "outbox").glob("*.json")), what="the acks")
        finally:
            hub.stop()
    finally:
        node.stop()


def test_an_unacked_event_is_sent_again_after_the_link_drops_and_lands_once(reg, tmp_path):
    hub = start_hub(reg)
    try:
        node = NodeThread(tmp_path, hub.port, pair(reg)).start()
        node.wait_connected()
        wait_for(hub.connected, what="the hub to see the node")
        runtime = node.runtime
        acks = []
        real_ack = runtime.ack
        runtime.ack = lambda event_id: acks.append(event_id)  # the ack is lost on the way
        out = actions.run(reg, "eye__speak", {"text": "hello Elijah"}, CLI)
        assert isinstance(out, actions.Ran)
        wait_for(lambda: len(rows(reg)) == 1 and acks, what="the event and its (lost) ack")
        assert len(runtime.unacked()) == 1  # still on disk
        runtime.ack = real_ack
        for conn in list(hub._conns):  # drop the link from the host side
            hub._loop.call_soon_threadsafe(lambda c=conn: __import__("asyncio").ensure_future(c.ws.close()))
        wait_for(lambda: not runtime.unacked(), 10, "the resent event to be acked")
        assert len(rows(reg)) == 1  # the host got it twice and kept it once
        node.stop()
    finally:
        hub.stop()


def test_the_sender_delivers_the_event_to_elijah_once(reg):
    import asyncio

    from jarvis.core.outbox import Sender

    outbox.add(reg.conn, "done", "app:eye", dedupe_key="event:z")
    assert outbox.add(reg.conn, "done", "app:eye", dedupe_key="event:z") is None
    sent = []

    class Chan:
        name = "discord"

        async def send(self, text):
            sent.append(text)

    from datetime import datetime, timezone

    sender = Sender(reg.conn, lambda source, text: Chan(), lambda: datetime.now(timezone.utc), reg.settings.tz())
    asyncio.run(sender.flush())
    asyncio.run(sender.flush())
    assert sent == ["done"]


def test_a_host_app_notify_is_an_outbox_row_even_from_a_worker_thread(echo):
    import asyncio
    import threading

    from fake_channel import FakeChannel
    from scripted_brain import ScriptedBrain

    from jarvis.host import Host

    host = Host(echo, ScriptedBrain([]), [FakeChannel()])

    async def go():
        task = asyncio.create_task(host.run())
        await asyncio.sleep(0.05)
        ctx = echo.apps["echo"].ctx
        ctx.notify("from the loop")
        t = threading.Thread(target=ctx.notify, args=("from a thread",))
        t.start()
        t.join()
        await asyncio.sleep(0.05)
        found = [(r["text"], r["source"]) for r in rows(echo)]
        host.request_stop()
        await task
        return found

    assert asyncio.run(go()) == [("from the loop", "app:echo"), ("from a thread", "app:echo")]
