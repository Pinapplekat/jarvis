import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from jarvis import config
from jarvis.chat import apply_actions, build_prompt, respond
from jarvis.memory import Memory
from jarvis.web import Page, find_urls

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 15, 15, 0, tzinfo=TZ)


@pytest.fixture(autouse=True)
def mail_account(monkeypatch):
    monkeypatch.setattr(config, "MAIL_ACCOUNT", "me@school.edu")


def fake_runner(reply, actions=()):
    calls = []

    def run(system, prompt):
        calls.append(prompt)
        return {"reply": reply, "actions": list(actions)}
    run.calls = calls
    return run


def test_reply_is_returned_and_history_kept():
    m = Memory(":memory:")
    run = fake_runner("Hi Elijah.")
    assert respond(m, "hello", NOW, "", runner=run) == "Hi Elijah."
    respond(m, "again", NOW, "", runner=fake_runner("Hello again."))
    assert [(h["role"], h["text"]) for h in m.chat_history(10)] == [
        ("user", "hello"), ("assistant", "Hi Elijah."), ("user", "again"), ("assistant", "Hello again."),
    ]


def test_prompt_includes_context_history_and_deadlines():
    m = Memory(":memory:")
    m.remember("money", "rent_due", "1st of the month")
    m.add_chat("user", "earlier question")
    m.queue_action("send_email", {"from": "me@school.edu", "to": ["prof@x.edu"], "subject": "Ext", "body": "."})
    prompt = build_prompt(m, "what's up?", NOW, "Sun Oct 18: ECON 100 Module 3")
    for expected in ("rent_due", "earlier question", "ECON 100 Module 3", "#1 to prof@x.edu", "Thursday October 15"):
        assert expected in prompt


def test_actions_change_memory_and_queue_drafts_only():
    m = Memory(":memory:")
    problems = apply_actions(m, [
        {"type": "remember", "category": "money", "key": "rent_due", "value": "the 1st"},
        {"type": "project", "name": "Shop", "kind": "business", "summary": "Etsy store"},
        {"type": "note", "content": "Shipped 3 orders", "project": "Shop"},
        {"type": "add_deadline", "course": "ECON 100", "title": "Module 4", "due": "2026-10-25"},
        {"type": "draft_email", "to": ["prof@x.edu"], "subject": "Extension", "body": "Hi Prof,"},
    ], TZ)
    assert problems == []
    assert m.facts("money")[0]["value"] == "the 1st"
    assert m.projects()[0]["summary"] == "Etsy store"
    assert m.recent_events()[0]["content"] == "Shipped 3 orders"
    assert m.deadlines()[0]["title"] == "Module 4"
    (draft,) = m.pending_actions(kind="send_email")
    assert json.loads(draft["payload"])["from"] == "me@school.edu"
    assert m.pending_actions(status="approved") == []  # nothing is approved, let alone sent


def test_bad_actions_are_reported_not_crashing():
    m = Memory(":memory:")
    problems = apply_actions(m, [
        {"type": "remember", "category": "secrets", "key": "k", "value": "v"},
        {"type": "project", "name": "X", "kind": "hobby"},
        {"type": "draft_email", "to": ["not-an-address"], "subject": "s", "body": "b"},
        {"type": "send_email_now", "to": ["a@b.c"]},
        {"type": "deadline_done", "key": "nope"},
        {"type": "remember"},
    ], TZ)
    assert len(problems) == 6
    assert m.facts() == [] and m.projects() == [] and m.pending_actions() == []


def test_claude_failure_still_answers():
    m = Memory(":memory:")

    def broken(system, prompt):
        raise RuntimeError("usage limit reached")
    reply = respond(m, "hi", NOW, "", runner=broken)
    assert "couldn't think" in reply and "usage limit" in reply


def fake_fetcher(opened):
    def fetch(url):
        opened.append(url)
        return Page(url=url, title="Agenda", text=f"Week 3: read chapter 5 (from {url})")
    return fetch


def test_links_in_message_are_opened_before_answering():
    m, opened = Memory(":memory:"), []
    run = fake_runner("Ch 5 this week.")
    reply = respond(m, "what's on https://agenda.test/week3 ?", NOW, "", runner=run, fetcher=fake_fetcher(opened))
    assert opened == ["https://agenda.test/week3"]
    assert "Week 3: read chapter 5" in run.calls[0] and "Untrusted page content" in run.calls[0]
    assert reply == "Ch 5 this week."


def test_claude_can_ask_to_open_pages_then_answers():
    m, opened, prompts = Memory(":memory:"), [], []
    m.remember("school", "engl_agenda_url", "https://agenda.test/engl")

    def run(system, prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return {"reply": "checking", "actions": [{"type": "open_url", "url": "https://agenda.test/engl"}]}
        return {"reply": "Read ch 5.", "actions": [
            {"type": "add_deadline", "course": "ENGL&101", "title": "Ch 5", "due": "2026-10-19"}]}

    reply = respond(m, "what's on the engl agenda", NOW, "", runner=run, fetcher=fake_fetcher(opened))
    assert reply == "Read ch 5." and opened == ["https://agenda.test/engl"]
    assert "Week 3" in prompts[1] and m.deadlines()[0]["title"] == "Ch 5"


def test_page_requests_are_capped():
    m, opened = Memory(":memory:"), []
    pages = iter(f"https://site.test/{i}" for i in range(100))

    def greedy(system, prompt):
        return {"reply": "still looking", "actions": [{"type": "open_url", "url": next(pages)} for _ in range(5)]}

    reply = respond(m, "find everything", NOW, "", runner=greedy, fetcher=fake_fetcher(opened))
    assert len(opened) <= 6 and reply == "still looking"
    assert m.pending_actions() == [] and m.facts() == []


def test_find_urls():
    assert find_urls("see https://a.test/x, and (http://b.test/y). https://a.test/x") == [
        "https://a.test/x", "http://b.test/y"]


def test_skipped_actions_are_mentioned_in_reply():
    m = Memory(":memory:")
    run = fake_runner("Done!", [{"type": "remember", "category": "nope", "key": "a", "value": "b"}])
    assert "couldn't do" in respond(m, "remember a", NOW, "", runner=run)
