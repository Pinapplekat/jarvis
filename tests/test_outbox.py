import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fake_channel import FakeChannel
from scripted_brain import ScriptedBrain

from jarvis.core import outbox
from jarvis.core.outbox import Sender
from jarvis.host import Host

LA = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 10, 26, 15, 5, tzinfo=LA).astimezone(timezone.utc)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


class Down(FakeChannel):
    """A channel that can't be reached until `up` is set."""

    up = False

    async def send(self, text, **kw):
        if not self.up:
            raise ConnectionError("Discord is down")
        await super().send(text, **kw)


def sender(reg, channel, clock):
    return Sender(reg.conn, lambda: channel, clock, LA)


def texts(ch):
    return [s[0] for s in ch.sent]


def add(reg, text, clock, **kw):
    return outbox.add(reg.conn, text, "test", clock(), **kw)


def test_rows_go_out_oldest_first_to_the_channel_with_no_reply_to(real_apps):
    clock, ch = Clock(), FakeChannel()
    for t in ("one", "two", "three"):
        add(real_apps, t, clock)
    assert asyncio.run(sender(real_apps, ch, clock).flush()) == 3
    assert ch.sent == [("one", None, None), ("two", None, None), ("three", None, None)]
    assert outbox.unsent(real_apps.conn) == []
    assert asyncio.run(sender(real_apps, ch, clock).flush()) == 0  # nothing twice


def test_a_failing_channel_keeps_the_row_and_retries_with_backoff(real_apps):
    clock, ch = Clock(), Down()
    s = sender(real_apps, ch, clock)
    add(real_apps, "first", clock)
    add(real_apps, "second", clock)
    assert asyncio.run(s.flush()) == 0
    row = outbox.unsent(real_apps.conn)[0]
    assert (row["attempts"], row["last_error"]) == (1, "ConnectionError: Discord is down")
    ch.up = True
    assert asyncio.run(s.flush()) == 0  # backing off: not before 5 seconds
    clock.now += timedelta(seconds=6)
    assert asyncio.run(s.flush()) == 2
    assert texts(ch)[0].startswith("first") and texts(ch)[1].startswith("second")  # still in order


def test_retry_delays_are_5s_15s_1min_then_every_2_minutes(real_apps):
    clock, ch = Clock(), Down()
    s = sender(real_apps, ch, clock)
    add(real_apps, "x", clock)
    seen = []
    for _ in range(5):
        asyncio.run(s.flush())
        seen.append(s._retry_at[1] - clock.now)
        clock.now = s._retry_at[1]
    assert [d.total_seconds() for d in seen] == [5, 15, 60, 120, 120]


def test_a_message_that_went_out_late_says_so(real_apps):
    clock, ch = Clock(), Down()
    s = sender(real_apps, ch, clock)
    add(real_apps, "⏰ stretch", clock)
    asyncio.run(s.flush())
    ch.up = True
    clock.now += timedelta(minutes=3)
    asyncio.run(s.flush())
    assert texts(ch) == ["⏰ stretch\n(held: Fake was unreachable at 3:05 PM)".replace("Fake", "Discord")]


def test_a_quick_retry_adds_no_note_and_a_first_try_never_does(real_apps):
    clock, ch = Clock(), Down()
    s = sender(real_apps, ch, clock)
    add(real_apps, "quick", clock)
    asyncio.run(s.flush())
    ch.up = True
    clock.now += timedelta(seconds=10)
    asyncio.run(s.flush())
    add(real_apps, "plain", clock)
    clock.now += timedelta(hours=1)  # created long ago but never failed: no note
    asyncio.run(s.flush())
    assert texts(ch) == ["quick", "plain"]


def test_not_before_holds_a_row_without_blocking_the_others_or_adding_a_note(real_apps):
    clock, ch = Clock(), FakeChannel()
    s = sender(real_apps, ch, clock)
    add(real_apps, "later", clock, not_before=T0 + timedelta(hours=8))
    add(real_apps, "now", clock)
    asyncio.run(s.flush())
    assert texts(ch) == ["now"]
    clock.now += timedelta(hours=8, seconds=1)
    asyncio.run(s.flush())
    assert texts(ch) == ["now", "later"]  # held on purpose, not "unreachable"


def test_every_sent_message_joins_the_conversation(real_apps):
    clock, ch = Clock(), FakeChannel()
    add(real_apps, "⏰ stretch", clock)
    asyncio.run(sender(real_apps, ch, clock).flush())
    row = real_apps.conn.execute("SELECT role, text FROM chat_log").fetchone()
    assert (row["role"], row["text"]) == ("assistant", "⏰ stretch")


def test_nothing_is_dropped_without_a_channel(real_apps):
    clock = Clock()
    add(real_apps, "waiting", clock)
    nobody = Sender(real_apps.conn, lambda: None, clock, LA)
    assert asyncio.run(nobody.flush()) == 0
    assert [r["text"] for r in outbox.unsent(real_apps.conn)] == ["waiting"]
    ch = FakeChannel()
    assert asyncio.run(sender(real_apps, ch, clock).flush()) == 1 and texts(ch) == ["waiting"]


def test_sent_rows_are_pruned_after_two_weeks(real_apps):
    clock = Clock()
    old = add(real_apps, "old", clock)
    real_apps.conn.execute("UPDATE outbox SET sent_at=? WHERE id=?",
                           ((T0 - timedelta(days=20)).isoformat(timespec="seconds"), old))
    add(real_apps, "unsent", clock)
    outbox.prune(real_apps.conn, T0)
    assert [r["text"] for r in outbox.recent(real_apps.conn)] == ["unsent"]


def test_the_host_notifies_through_the_notify_channel_or_the_first_running_one(real_apps):
    async def go(setting, channels):
        host = Host(real_apps, ScriptedBrain(), channels)
        task = asyncio.create_task(host.run())
        await asyncio.sleep(0)
        host.notify("hello sir", "test")
        for _ in range(200):
            if any(c.sent for c in channels):
                break
            await asyncio.sleep(0.01)
        host.request_stop()
        await task

    discord, other = FakeChannel("discord"), FakeChannel("telegram")
    asyncio.run(go("discord", [other, discord]))
    assert texts(discord) == ["hello sir"] and other.sent == []

    real_apps_reg = real_apps
    only = FakeChannel("cli")  # no channel is called discord: the first running one takes it
    host = Host.__new__(Host)
    host.reg, host.channels = real_apps_reg, {"cli": only}
    assert host._notify_channel() is only
    host.channels = {}
    assert host._notify_channel() is None
