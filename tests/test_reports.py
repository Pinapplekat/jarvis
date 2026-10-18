from jarvis import reports
from jarvis.memory import Memory


def test_one_message_for_everything_waiting_and_progress_is_never_sent():
    m = Memory(":memory:")
    reports.add(m, "task", 1, "progress", "site tests", "10 steps in")
    reports.add(m, "task", 1, "done", "site tests", "All green, sir.")
    reports.add(m, "job", 3, "failed", "the background job `npm run build`", "exit 1")
    sent, seen = [], []

    def compose(items):
        seen.append([r["kind"] for r in items])
        return "Site tests are green, sir; the build failed, though."
    assert reports.deliver(m, sent.append, compose) == 2
    assert sent == ["Site tests are green, sir; the build failed, though."] and seen == [["done", "failed"]]
    assert m.chat_history(1)[0]["text"] == sent[0]  # so a follow-up question makes sense
    assert reports.deliver(m, sent.append, compose) == 0  # each report goes out once


def test_a_broken_composer_still_gets_the_news_through():
    m = Memory(":memory:")
    reports.add(m, "task", 1, "done", "site tests", "All green, sir.")
    sent = []

    def broken(items):
        raise RuntimeError("Claude CLI failed")
    reports.deliver(m, sent.append, broken)
    assert sent == ["Done: site tests. All green, sir."]


def test_chat_sees_what_was_reported_and_what_was_passed_on():
    m = Memory(":memory:")
    reports.add(m, "task", 1, "progress", "site tests", "10 steps in; now: Running pytest")
    reports.add(m, "task", 1, "done", "site tests", "All green.")
    text = reports.for_prompt(m)
    assert "progress (for you only): site tests: 10 steps in" in text and "done (not passed on)" in text
    reports.deliver(m, lambda t: None, lambda items: "ok")
    assert "done (told him)" in reports.for_prompt(m)
