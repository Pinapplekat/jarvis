"""The scheduler, outbox and apps together inside a running host."""

import asyncio
import time
from datetime import datetime, timedelta, timezone

from fake_channel import FakeChannel
from scripted_brain import ScriptedBrain

from jarvis.core import outbox
from jarvis.core.apps import load_apps
from jarvis.core.channel import Incoming
from jarvis.host import Host


def remind_in(seconds, text="stretch"):
    due = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(microsecond=0)
    return due, ("reminders__remind_me", {"text": text, "at": due.astimezone().isoformat(timespec="seconds")})


async def wait_for(condition, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        await asyncio.sleep(0.02)
    return False


def test_a_job_message_reaches_the_channel_and_the_conversation(jobs_reg, settings):
    jobs_reg.apps["ticker"].module.CONTROL["beat"] = {"messages": ["good evening, sir"]}
    ch = FakeChannel()
    host = Host(jobs_reg, ScriptedBrain(), [ch])

    async def go():
        task = asyncio.create_task(host.run())
        assert await wait_for(lambda: ch.sent, 5)
        host.request_stop()
        await task

    asyncio.run(go())
    assert ch.sent[0] == ("good evening, sir", None, None)  # no reply_to: it opens the conversation
    reg = load_apps(settings)  # the host closed its registry; look at what it left
    try:
        assert [r["text"] for r in reg.conn.execute("SELECT text FROM chat_log WHERE role='assistant'")] == [
            "good evening, sir"]
        assert reg.conn.execute("SELECT sent_at FROM outbox").fetchone()["sent_at"] is not None
    finally:
        reg.close()


def test_a_reminder_arrives_on_time_from_a_wake_run(real_apps, settings):
    due, step = remind_in(3)
    ch = FakeChannel()
    host = Host(real_apps, ScriptedBrain([step], text="Done."), [ch])
    arrived = []

    async def go():
        task = asyncio.create_task(host.run())
        await ch.deliver(Incoming("discord", "remind me in 3 seconds to stretch", reply_to="dm"))
        assert await wait_for(lambda: any(s[0].startswith("⏰") for s in ch.sent), 10)
        arrived.append(datetime.now(timezone.utc))
        await asyncio.sleep(0.3)
        host.request_stop()
        await task

    asyncio.run(go())
    assert [s[0] for s in ch.sent if s[0].startswith("⏰")] == ["⏰ stretch"]  # on time: no late note, only once
    assert timedelta(0) <= arrived[0] - due < timedelta(seconds=5)
    reg = load_apps(settings)
    try:
        runs = [(r["name"], r["reason"]) for r in reg.conn.execute("SELECT name, reason FROM job_runs WHERE app='reminders'")]
        assert ("deliver", "wake") in runs
        said = [r["text"] for r in reg.conn.execute("SELECT text FROM chat_log WHERE role='assistant'")]
        assert "⏰ stretch" in said  # a reply like "done" has context
    finally:
        reg.close()


def test_a_reminder_set_before_a_restart_still_arrives_once(settings):
    due, step = remind_in(4, "call the bank")
    first_channel = FakeChannel()

    async def first():
        host = Host(load_apps(settings), ScriptedBrain([step], text="Okay."), [first_channel])
        task = asyncio.create_task(host.run())
        await first_channel.deliver(Incoming("discord", "remind me", reply_to="dm"))
        assert await wait_for(lambda: first_channel.sent, 5)
        host.request_stop()
        await task

    asyncio.run(first())
    assert not any(s[0].startswith("⏰") for s in first_channel.sent)
    second_channel = FakeChannel()

    async def second():
        host = Host(load_apps(settings), ScriptedBrain(), [second_channel])
        task = asyncio.create_task(host.run())
        assert await wait_for(lambda: any(s[0].startswith("⏰") for s in second_channel.sent), 10)
        await asyncio.sleep(1.0)
        host.request_stop()
        await task

    asyncio.run(second())
    assert [s[0] for s in second_channel.sent] == ["⏰ call the bank"]


def test_messages_wait_in_the_outbox_when_no_channel_is_up(real_apps):
    host = Host(real_apps, ScriptedBrain(), [])
    host.notify("while you were out", "test")
    assert [r["text"] for r in outbox.unsent(real_apps.conn)] == ["while you were out"]


def test_the_scheduler_stops_with_the_host_and_leaves_no_run_half_recorded(jobs_reg):
    host = Host(jobs_reg, ScriptedBrain(), [FakeChannel()])

    async def go():
        task = asyncio.create_task(host.run())
        await asyncio.sleep(0.3)
        host.request_stop()
        await asyncio.wait_for(task, 5)

    asyncio.run(go())
