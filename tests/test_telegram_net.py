import httpx
import pytest

from jarvis.cli import Outage
from jarvis.telegram import TelegramClient, TelegramError


class FakeHTTP:
    def __init__(self, errors):
        self.errors, self.calls = list(errors), 0

    def post(self, url, json=None, **kw):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}},
                              request=httpx.Request("POST", url))


def client(errors):
    tg = TelegramClient(token="T", chat_id="1")
    tg.http, tg._sleep = FakeHTTP(errors), lambda s: None
    return tg


def test_send_retries_a_connection_that_never_opened():
    tg = client([httpx.ConnectError("down"), httpx.ConnectError("down")])
    assert tg.send("hi") == 7 and tg.http.calls == 3


def test_send_never_retries_a_dropped_connection():  # it may have been delivered: no double message
    tg = client([httpx.ReadError("dropped")])
    with pytest.raises(TelegramError, match="sendMessage: ReadError"):
        tg.send("hi")
    assert tg.http.calls == 1


def test_idempotent_calls_retry_a_dropped_connection():
    tg = client([httpx.ReadError("dropped")])
    tg.typing()
    assert tg.http.calls == 2


def test_gives_up_after_retries_without_leaking_the_token():
    tg = client([httpx.ConnectError("x")] * 5)
    with pytest.raises(TelegramError) as e:
        tg.send("hi")
    assert "T" not in str(e.value).replace("TelegramError", "") or "bot" not in str(e.value)
    assert tg.http.calls == 3


def test_outage_backs_off_and_logs_once():
    t = [100.0]
    o = Outage(first=5, most=60, clock=lambda: t[0])
    waits = [o.failed("e") for _ in range(6)]
    assert [w for w, _ in waits] == [5, 10, 20, 40, 60, 60]
    assert [log for _, log in waits] == [True, False, False, False, False, False]
    t[0] = 190.0
    assert o.ok() == "Telegram back after 6 failed polls over 90s"
    assert o.ok() is None and o.failed("e")[0] == 5
