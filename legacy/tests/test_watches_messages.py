import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from jarvis import config, messagesread, watches
from jarvis.memory import Memory

TZ = ZoneInfo("America/Los_Angeles")
SECRET = "the gate code is 4471"


@pytest.fixture
def wm(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MESSAGES_WATCH", True)
    monkeypatch.setattr(config, "WATCHES_MAX_ACTIVE", 20)
    monkeypatch.setattr(config, "MESSAGES_DB", str(tmp_path / "chat.db"))
    return Memory(":memory:")


class ChatDB:
    """A tiny chat.db with the tables and columns the reader touches."""

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, display_name TEXT);
            CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, attributedBody BLOB, handle_id INTEGER,
                                  is_from_me INTEGER);
            CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        """)
        self.chats: dict[str, int] = {}

    def add(self, chat: str, sender: str, text: str | None, mine: bool = False, blob: bytes | None = None) -> int:
        if chat not in self.chats:
            self.chats[chat] = self.db.execute("INSERT INTO chat (display_name) VALUES (?)", (chat,)).lastrowid
        h = self.db.execute("SELECT ROWID FROM handle WHERE id = ?", (sender,)).fetchone()
        hid = h[0] if h else self.db.execute("INSERT INTO handle (id) VALUES (?)", (sender,)).lastrowid
        rowid = self.db.execute("INSERT INTO message (text, attributedBody, handle_id, is_from_me) VALUES (?, ?, ?, ?)",
                                (text, blob, hid, int(mine))).lastrowid
        self.db.execute("INSERT INTO chat_message_join VALUES (?, ?)", (self.chats[chat], rowid))
        self.db.commit()
        return rowid


def now_local():
    return datetime.now(TZ)


def alerts(m):
    return [r["message"] for r in m.db.execute("SELECT message FROM alerts ORDER BY id")]


def logged(m):
    return " ".join(r["content"] for r in m.db.execute("SELECT content FROM events"))


def chat_text(m):
    return " ".join(r["text"] for r in m.db.execute("SELECT text FROM chat_log"))


def test_one_alert_per_new_message_and_old_ones_skipped(wm):
    chat = ChatDB(config.MESSAGES_DB)
    chat.add("Arboreal", "+12065550100", "old news from before the watch")
    t = now_local()
    wid = watches.create(wm, "Arboreal", "messages", {"from": ["Arboreal"]}, t)
    assert watches.get(wm, wid)["recurring"]
    assert watches.run_due(wm, t) == "checked 1, triggered 0"  # first look records where it is
    chat.add("Arboreal", "+12065550100", "Shift moved to 3pm")
    chat.add("Arboreal", "+12065550111", "Can you bring gloves?")
    chat.add("Arboreal", "+12065550100", "my own reply", mine=True)
    assert "triggered 1" in watches.run_due(wm, t + timedelta(minutes=5))
    assert alerts(wm) == ["👁 Arboreal: new text from Arboreal: Shift moved to 3pm",
                          "👁 Arboreal: new text from Arboreal: Can you bring gloves?"]
    assert watches.run_due(wm, t + timedelta(minutes=10)) == "checked 1, triggered 0"  # each only once
    assert len(alerts(wm)) == 2
    assert watches.get(wm, wid)["status"] == "active"


def test_ignores_unlisted_senders_and_chats(wm):
    chat = ChatDB(config.MESSAGES_DB)
    t = now_local()
    watches.create(wm, "Emily", "messages", {"from": ["(206) 555-0100"]}, t)
    watches.run_due(wm, t)
    chat.add("", "+12065559999", "someone else entirely")
    chat.add("Arboreal Fans", "+12065550111", "a near-miss chat name")
    chat.add("", "+120655501001", "a longer number that only starts the same")
    assert watches.run_due(wm, t + timedelta(minutes=5)) == "checked 1, triggered 0"
    chat.add("", "+12065550100", "hi from Emily")
    watches.run_due(wm, t + timedelta(minutes=10))
    assert alerts(wm) == ["👁 Emily: new text from (206) 555-0100: hi from Emily"]


def test_reader_never_queries_without_a_target(monkeypatch):
    def boom(path):
        raise AssertionError("opened chat.db with nothing to watch")
    monkeypatch.setattr(messagesread, "_connect", boom)
    assert messagesread.read_new([], 5) == ([], 5)
    assert messagesread.read_new(["  "], None) == ([], 0)


def test_needs_a_target_and_his_own_ask(wm):
    with pytest.raises(ValueError, match="exact sender"):
        watches.create(wm, "x", "messages", {}, now_local())
    with pytest.raises(ValueError, match="asks for it himself"):
        watches.create(wm, "x", "messages", {"from": ["Arboreal"]}, now_local(), trusted=False)
    with pytest.raises(ValueError, match="only alerts him"):
        watches.create(wm, "x", "messages", {"from": ["Arboreal"]}, now_local(),
                       follow_up={"to": "Arboreal", "body": "ok"})
    assert not hasattr(messagesread, "send") and not any("send" in n for n in dir(messagesread))


def test_message_text_stays_out_of_logs_and_chat(wm):
    chat = ChatDB(config.MESSAGES_DB)
    t = now_local()
    watches.create(wm, "Arboreal", "messages", {"from": ["Arboreal"]}, t)
    watches.run_due(wm, t)
    chat.add("Arboreal", "+12065550100", SECRET)
    watches.run_due(wm, t + timedelta(minutes=5))
    assert SECRET in alerts(wm)[0]  # only the alert to him carries it
    assert "4471" not in logged(wm) and "4471" not in chat_text(wm)
    assert "'Arboreal' triggered: 1 new text" in logged(wm)


def test_attributed_body_is_read(wm):
    chat = ChatDB(config.MESSAGES_DB)
    t = now_local()
    watches.create(wm, "Arboreal", "messages", {"from": ["Arboreal"]}, t)
    watches.run_due(wm, t)
    body = "hello from the new format".encode()
    chat.add("Arboreal", "+12065550100", None, blob=b"streamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString"
             b"\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+" + bytes([len(body)]) + body)
    watches.run_due(wm, t + timedelta(minutes=5))
    assert alerts(wm) == ["👁 Arboreal: new text from Arboreal: hello from the new format"]


def test_missing_permission_says_so_once(wm, monkeypatch):
    monkeypatch.setattr(config, "MESSAGES_DB", "/nonexistent/Messages/chat.db")
    t = now_local()
    watches.create(wm, "Arboreal", "messages", {"from": ["Arboreal"]}, t)
    for i in range(3):
        assert "checked 1" in watches.run_due(wm, t + timedelta(minutes=5 * i))
    assert logged(wm).count("Messages isn't readable") == 1
    assert len(alerts(wm)) == 1 and "Full Disk Access" in alerts(wm)[0]


def test_describe(wm):
    wid = watches.create(wm, "Arboreal", "messages", {"from": ["Arboreal", "Arboreal"]}, now_local())
    assert watches.describe(watches.get(wm, wid)) == "new texts from Arboreal"
