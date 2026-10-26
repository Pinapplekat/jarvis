from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from jarvis import rightnow
from jarvis.chat import build_prompt
from jarvis.memory import Memory
from jarvis.orders import DEFAULT_ORDERS, standing_orders

TZ = ZoneInfo("America/Los_Angeles")


def test_standing_orders_are_pinned_once_and_preferences_leave_the_facts_list():
    m = Memory(":memory:")
    m.remember("preference", "reply_style", "Short and formal.")
    m.remember("personal", "fiancee", "Emily")
    orders = standing_orders(m)
    assert orders.count("Short and formal.") == 1 and DEFAULT_ORDERS[0] in orders
    prompt = build_prompt(m, "hi", datetime.now(TZ), "")
    assert prompt.index("<standing_orders") < prompt.index("<context>")
    context = prompt[prompt.index("<context>"):prompt.index("</context>")]
    assert "Short and formal." not in context and "Emily" in context


def test_right_now_says_nothing_is_running_when_nothing_is():
    m = Memory(":memory:")
    text = rightnow.for_prompt(m, datetime.now(TZ), idle=lambda: 5)
    assert "nothing (no task" in text and "at the Mac" in text


def test_right_now_lists_running_tasks_and_what_he_was_told():
    m = Memory(":memory:")
    now = datetime.now(timezone.utc)
    m.db.execute("INSERT INTO tasks (goal, status, created_at, updated_at) VALUES (?, 'running', ?, ?)",
                 ("fix the tests", now.isoformat(), now.isoformat()))
    m.log("heartbeat", "sent (normal): Sir, Twilio wants a reply about your account.")
    m.add_chat("user", "I know, don't worry")
    text = rightnow.for_prompt(m, now, idle=lambda: 900)
    assert "fix the tests" in text and "Twilio wants a reply" in text
    assert "brushed off" in text and "away from the Mac" in text


def test_mid_conversation():
    m = Memory(":memory:")
    m.add_chat("user", "hey")
    m.add_chat("assistant", "Sir.")
    m.add_chat("user", "what's next")
    assert "mid-conversation" in rightnow.presence(m, datetime.now(timezone.utc) + timedelta(seconds=5), None)
