import json
import time
from datetime import datetime, timezone

from jarvis import inbox
from jarvis.approvals import _handle, notify_pending
from jarvis.memory import Memory
from jarvis.telegram import Update
from tests.test_approvals import FakeTelegram, _draft


def messages():
    return inbox.read()["messages"]


def test_everything_sent_to_telegram_is_mirrored_with_its_buttons():
    tg = inbox.Mirror(FakeTelegram())
    tg.send("Hello, sir.")
    mid = tg.send("Run it?", buttons=[("Run ▶️", "cmd_run:4"), ("Reject ✖️", "cmd_reject:4")])
    assert [m["text"] for m in messages()] == ["Hello, sir.", "Run it?"]
    assert messages()[1]["buttons"] == [["Run ▶️", "cmd_run:4"], ["Reject ✖️", "cmd_reject:4"]]
    assert inbox.read()["waiting"] == 1
    tg.edit(mid, "Run it?\n\n— Ran OK")  # pressed in Telegram: the Mac's copy follows
    assert messages()[1]["buttons"] == [] and messages()[1]["text"].endswith("Ran OK") and inbox.read()["waiting"] == 0


def test_only_real_open_buttons_can_be_pressed():
    lid = inbox.record("jarvis", "Build it?", [("Yes, build it ✅", "plan_yes:3"), ("Not now", "plan_no:3")])
    assert inbox.open_button(lid, "plan_yes:3")["text"] == "Build it?"
    assert inbox.open_button(lid, "se_deploy:3") is None  # not a button on that message
    inbox.resolve(lid, note="Building it")
    assert inbox.open_button(lid, "plan_yes:3") is None and messages()[0]["note"] == "Building it"


def test_approving_from_the_mac_goes_through_the_same_code_and_updates_telegram():
    m, real = Memory(":memory:"), FakeTelegram()
    tg = inbox.Mirror(real)
    aid = _draft(m)
    notify_pending(m, tg)
    [ask] = messages()
    mac = inbox.MacChat(tg, local_id=ask["id"])
    u = Update(update_id=0, sender_id=0, sent_at=datetime.now(timezone.utc), callback_id="inbox",
               callback_data=f"approve:{aid}", message_id=ask["tg_id"], message_text=ask["text"])
    assert _handle(m, mac, u)
    assert m.get_action(aid)["status"] == "approved"
    assert real.edits and real.edits[-1][0] == ask["tg_id"]  # Telegram's copy loses its buttons too
    [after] = messages()
    assert after["buttons"] == [] and after["note"].startswith("Approved the email")


def test_replies_to_what_he_types_on_the_mac_stay_on_the_mac_but_questions_reach_telegram():
    real = FakeTelegram()
    mac = inbox.MacChat(inbox.Mirror(real))
    mac.send("Here you are, sir.")
    mac.send("Run it?", buttons=[("Run ▶️", "cmd_run:1")])
    assert [t for t, _ in real.sent] == ["Run it?"]
    assert [(m["text"], m["via"]) for m in messages()] == [("Here you are, sir.", "Mac"), ("Run it?", "")]


def test_requests_are_taken_once_and_old_ones_dropped():
    inbox.REQUESTS.mkdir(parents=True)
    (inbox.REQUESTS / "1.json").write_text(json.dumps({"type": "text", "text": "hi", "at": time.time()}))
    (inbox.REQUESTS / "2.json").write_text(json.dumps({"type": "text", "text": "old", "at": time.time() - 900}))
    (inbox.REQUESTS / "3.json").write_text("not json")
    assert [r["text"] for r in inbox.take_requests()] == ["hi"] and inbox.take_requests() == []


def test_seed_starts_with_the_recent_conversation_without_prefixes():
    m = Memory(":memory:")
    m.add_chat("user", "(typed in the HUD on his Mac) what's due?")
    m.add_chat("assistant", "Two things, sir.")
    assert inbox.seed(m) == 2 and inbox.seed(m) == 0
    assert [(x["who"], x["text"], x["via"]) for x in messages()] == [("you", "what's due?", "HUD"), ("jarvis", "Two things, sir.", "")]


def test_unanswered_buttons_lapse():
    lid = inbox.record("jarvis", "Deploy it?", [("Deploy ✅", "se_deploy:1")])
    with inbox._editing() as data:
        data["messages"][0]["at"] -= inbox.STALE_AFTER + 1
    assert inbox.open_button(lid, "se_deploy:1") is None and inbox.read()["waiting"] == 0
