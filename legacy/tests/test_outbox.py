from datetime import datetime, timedelta, timezone

from jarvis import outbox, rightnow
from jarvis.memory import Memory
from jarvis.telegram import TelegramError
from jarvis.voice import Announcer


class Flaky:
    def __init__(self):
        self.down, self.sent = True, []

    def send(self, text, buttons=None):
        if self.down:
            raise TelegramError("sendMessage: ConnectError")
        self.sent.append(text)
        return len(self.sent)


def test_offline_messages_wait_and_go_out_late_but_in_order():
    m, tg = Memory(":memory:"), Flaky()
    a = Announcer(tg, m, presence=lambda now: False)
    a.send("⏰ Econ time, sir.")
    a.send("The tests finished.")
    assert outbox.waiting(m) == 2
    assert outbox.flush(m, tg.send) == 0  # still down: nothing lost
    tg.down = False
    later = datetime.now(timezone.utc) + timedelta(minutes=10)
    assert outbox.flush(m, tg.send, now=later, tz=timezone.utc) == 2
    assert tg.sent[0].startswith("⏰ Econ time, sir.\n(late:") and outbox.waiting(m) == 0


def test_questions_with_buttons_still_raise_so_their_senders_retry():
    import pytest
    m, tg = Memory(":memory:"), Flaky()
    with pytest.raises(TelegramError):
        Announcer(tg, m, presence=lambda now: False).send("Approve?", buttons=[("Yes", "y:1")])
    assert outbox.waiting(m) == 0


def test_driving_from_two_fresh_points():
    m = Memory(":memory:")
    now = datetime.now(timezone.utc)
    for lat, at in ((47.60, now - timedelta(seconds=90)), (47.61, now - timedelta(seconds=30))):
        m.db.execute("INSERT INTO location_points (at, last_seen, lat, lon, source) VALUES (?, ?, ?, ?, 'live')",
                     (at.isoformat(), at.isoformat(), lat, -122.3))
    assert "probably driving" in rightnow.moving(m, now)  # ~1.1 km in a minute
    assert rightnow.moving(m, now + timedelta(minutes=10)) == ""  # stale: don't assume
